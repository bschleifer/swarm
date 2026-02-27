"""REST + WebSocket API server for the swarm daemon."""

from __future__ import annotations

import asyncio
import hmac
import json
import os
import secrets
import time
from collections import defaultdict, deque
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from aiohttp import web

from swarm.logging import get_logger
from swarm.pty.process import ProcessError
from swarm.server.daemon import SwarmOperationError, TaskOperationError, WorkerNotFoundError
from swarm.server.helpers import (
    get_daemon,
    json_error,
    parse_limit,
    read_file_field,
    require_message,
    validate_worker_name,
)
from swarm.tasks.task import (
    TaskPriority,
    TaskType,
    validate_priority,
    validate_task_type,
)

if TYPE_CHECKING:
    from swarm.server.daemon import SwarmDaemon

_log = get_logger("server.api")

_RATE_LIMIT_REQUESTS = 60  # per minute
_RATE_LIMIT_WINDOW = 60  # seconds
_RATE_LIMIT_CLEANUP_INTERVAL = 300  # evict stale IPs every 5 minutes
_RATE_LIMIT_MAX_IPS = 10_000  # absolute cap on tracked IPs
_rate_limit_last_cleanup: float = 0.0

# Paths that require authentication for mutating methods
_CONFIG_AUTH_PREFIX = "/api/config"

# Auto-generated token for sessions where no api_password is configured.
# Generated once per process; logged on startup so the operator can see it.
_auto_token: str = secrets.token_urlsafe(32)


def _get_client_ip(request: web.Request) -> str:
    """Get client IP, respecting X-Forwarded-For only when trust_proxy is enabled.

    When trust_proxy is True, uses the rightmost-minus-one entry from X-Forwarded-For
    (the last hop before our trusted proxy). When False, always uses request.remote
    to prevent header spoofing from bypassing rate limits.
    """
    daemon = get_daemon(request)
    if daemon.config.trust_proxy:
        forwarded = request.headers.get("X-Forwarded-For", "")
        if forwarded:
            parts = [p.strip() for p in forwarded.split(",") if p.strip()]
            if len(parts) >= 2:
                return parts[-2]
            if parts:
                return parts[0]
    return request.remote or "unknown"


def _get_api_password(daemon: SwarmDaemon) -> str:
    """Get API password from config, environment, or auto-generated token.

    Always returns a non-empty string.  When the operator hasn't set an
    explicit password, the module-level ``_auto_token`` is used so that
    all endpoints are authenticated by default.
    """
    return os.environ.get("SWARM_API_PASSWORD") or daemon.config.api_password or _auto_token


@web.middleware
async def _config_auth_middleware(
    request: web.Request, handler: Callable[[web.Request], Awaitable[web.StreamResponse]]
) -> web.StreamResponse:
    """Require Bearer token for mutating config endpoints."""
    if request.path.startswith(_CONFIG_AUTH_PREFIX) and request.method in ("PUT", "POST", "DELETE"):
        daemon = get_daemon(request)
        password = _get_api_password(daemon)
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer ") or not hmac.compare_digest(auth[7:], password):
            return json_error("Unauthorized", 401)
    return await handler(request)


def _is_same_origin(request: web.Request, origin: str) -> bool:
    """Check if the Origin header matches the request host or the tunnel URL."""
    if not origin:
        return True  # No origin header = same-origin (non-browser client)
    req_host = request.host.split(":")[0] if request.host else ""
    parsed = urlparse(origin)
    origin_host = parsed.hostname or ""
    if origin_host in ("localhost", "127.0.0.1") or origin_host == req_host:
        return True
    # Allow tunnel origin when tunnel is running
    daemon = get_daemon(request)
    tunnel_url = daemon.tunnel.url
    if tunnel_url:
        tunnel_parsed = urlparse(tunnel_url)
        if origin_host == (tunnel_parsed.hostname or ""):
            return True
    return False


@web.middleware
async def _csrf_middleware(
    request: web.Request, handler: Callable[[web.Request], Awaitable[web.StreamResponse]]
) -> web.StreamResponse:
    """Reject cross-origin mutating requests.

    Checks both Origin header and requires X-Requested-With on API calls.
    Web form POSTs (non-API) are exempt from the header check.
    """
    if request.method in ("POST", "PUT", "DELETE"):
        origin = request.headers.get("Origin", "")
        if origin and not _is_same_origin(request, origin):
            return web.Response(status=403, text="CSRF rejected")
        # Require X-Requested-With for API and action endpoints
        if (
            request.path.startswith("/api/") or request.path.startswith("/action/")
        ) and not request.headers.get("X-Requested-With"):
            return web.Response(status=403, text="Missing X-Requested-With header")
    return await handler(request)


@web.middleware
async def _security_headers_middleware(
    request: web.Request, handler: Callable[[web.Request], Awaitable[web.StreamResponse]]
) -> web.StreamResponse:
    """Add security headers to all responses."""
    response = await handler(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    # CSP nonces are incompatible with inline event-handler attributes
    # (onclick/oninput/etc.) — browsers ignore 'unsafe-inline' when a nonce
    # is present, which blocks all inline handlers.  Until handlers are
    # migrated to addEventListener(), we must rely on 'unsafe-inline'.
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' https://unpkg.com https://cdn.jsdelivr.net; "
        "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
        "img-src 'self' data:; "
        "font-src 'self' data:; "
        "connect-src 'self' ws: wss:; "
        "frame-ancestors 'self'",
    )
    if request.secure:
        response.headers.setdefault(
            "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
        )
    return response


def create_app(daemon: SwarmDaemon, enable_web: bool = True) -> web.Application:
    """Create the aiohttp application with all routes."""
    app = web.Application(
        client_max_size=20 * 1024 * 1024,  # 20 MB for file uploads
        middlewares=[
            _security_headers_middleware,
            _csrf_middleware,
            _rate_limit_middleware,
            _config_auth_middleware,
        ],
    )
    app["daemon"] = daemon
    app["rate_limits"] = defaultdict(deque)  # ip -> deque of timestamps

    # Web dashboard routes (before API to allow / to serve dashboard)
    if enable_web:
        from swarm.web.app import setup_web_routes

        setup_web_routes(app)

    app.router.add_get("/api/health", handle_health)
    app.router.add_get("/api/workers", handle_workers)

    # Literal worker routes BEFORE {name} to avoid ambiguity
    app.router.add_post("/api/workers/launch", handle_workers_launch)
    app.router.add_post("/api/workers/spawn", handle_workers_spawn)
    app.router.add_post("/api/workers/continue-all", handle_workers_continue_all)
    app.router.add_post("/api/workers/send-all", handle_workers_send_all)
    app.router.add_post("/api/workers/discover", handle_workers_discover)

    app.router.add_get("/api/workers/{name}", handle_worker_detail)
    app.router.add_post("/api/workers/{name}/send", handle_worker_send)
    app.router.add_post("/api/workers/{name}/continue", handle_worker_continue)
    app.router.add_post("/api/workers/{name}/kill", handle_worker_kill)
    app.router.add_post("/api/workers/{name}/escape", handle_worker_escape)
    app.router.add_post("/api/workers/{name}/interrupt", handle_worker_interrupt)
    app.router.add_post("/api/workers/{name}/revive", handle_worker_revive)
    app.router.add_post("/api/workers/{name}/analyze", handle_worker_analyze)
    app.router.add_post("/api/workers/{name}/merge", handle_worker_merge)

    # Conflicts
    app.router.add_get("/api/conflicts", handle_conflicts)

    # Drones
    app.router.add_get("/api/drones/log", handle_drone_log)
    app.router.add_get("/api/drones/status", handle_drone_status)
    app.router.add_post("/api/drones/toggle", handle_drone_toggle)
    app.router.add_post("/api/drones/poll", handle_drones_poll)
    app.router.add_get("/api/drones/tuning", handle_tuning_suggestions)
    app.router.add_get("/api/notifications", handle_notification_history)
    app.router.add_get("/api/queen/oversight", handle_oversight_status)
    app.router.add_get("/api/coordination/ownership", handle_ownership_status)
    app.router.add_get("/api/coordination/sync", handle_sync_status)

    # Groups
    app.router.add_post("/api/groups/{name}/send", handle_group_send)

    # Queen
    app.router.add_post("/api/queen/coordinate", handle_queen_coordinate)
    app.router.add_get("/api/queen/queue", handle_queen_queue)

    # Usage
    app.router.add_get("/api/usage", handle_usage)

    # Session
    app.router.add_post("/api/session/kill", handle_session_kill)

    # Tunnel
    app.router.add_post("/api/tunnel/start", handle_tunnel_start)
    app.router.add_post("/api/tunnel/stop", handle_tunnel_stop)
    app.router.add_get("/api/tunnel/status", handle_tunnel_status)

    # Server
    app.router.add_post("/api/server/stop", handle_server_stop)
    app.router.add_post("/api/server/restart", handle_server_restart)

    # Uploads (standalone)
    app.router.add_post("/api/uploads", handle_upload)

    # Tasks
    app.router.add_get("/api/tasks", handle_tasks)
    app.router.add_post("/api/tasks", handle_create_task)
    app.router.add_post("/api/tasks/{task_id}/assign", handle_assign_task)
    app.router.add_post("/api/tasks/{task_id}/complete", handle_complete_task)
    app.router.add_post("/api/tasks/{task_id}/fail", handle_fail_task)
    app.router.add_post("/api/tasks/{task_id}/unassign", handle_unassign_task)
    app.router.add_post("/api/tasks/{task_id}/reopen", handle_reopen_task)
    app.router.add_delete("/api/tasks/{task_id}", handle_remove_task)
    app.router.add_patch("/api/tasks/{task_id}", handle_edit_task)
    app.router.add_post("/api/tasks/from-email", handle_create_task_from_email)
    app.router.add_post("/api/tasks/{task_id}/attachments", handle_upload_attachment)
    app.router.add_post("/api/tasks/{task_id}/retry-draft", handle_retry_draft)
    app.router.add_get("/api/tasks/{task_id}/history", handle_task_history)

    # Proposals
    app.router.add_get("/api/proposals", handle_proposals)
    app.router.add_post("/api/proposals/{proposal_id}/approve", handle_approve_proposal)
    app.router.add_post("/api/proposals/{proposal_id}/reject", handle_reject_proposal)
    app.router.add_post("/api/proposals/reject-all", handle_reject_all_proposals)

    # Decisions (proposal history)
    app.router.add_get("/api/decisions", handle_decisions)

    # Serve uploaded files
    uploads_dir = Path.home() / ".swarm" / "uploads"
    uploads_dir.mkdir(parents=True, exist_ok=True)
    app.router.add_static("/uploads/", uploads_dir)

    app.router.add_get("/ws", handle_websocket)

    from swarm.pty.bridge import handle_terminal_ws

    app.router.add_get("/ws/terminal", handle_terminal_ws)

    # Config endpoints
    app.router.add_get("/api/config", handle_get_config)
    app.router.add_put("/api/config", handle_update_config)
    app.router.add_post("/api/config/workers", handle_add_config_worker)
    app.router.add_post("/api/config/workers/{name}/save", handle_save_worker_to_config)
    app.router.add_post("/api/config/workers/{name}/add-to-group", handle_add_worker_to_group)
    app.router.add_delete("/api/config/workers/{name}", handle_remove_config_worker)
    app.router.add_post("/api/config/groups", handle_add_config_group)
    app.router.add_put("/api/config/groups/{name}", handle_update_config_group)
    app.router.add_delete("/api/config/groups/{name}", handle_remove_config_group)
    app.router.add_get("/api/config/projects", handle_list_projects)

    return app


@web.middleware
async def _rate_limit_middleware(
    request: web.Request, handler: Callable[[web.Request], Awaitable[web.StreamResponse]]
) -> web.StreamResponse:
    """Simple in-memory rate limiter: N requests/minute per client IP."""
    # Exempt read-only routes: pages, partials, static assets, WebSockets
    if request.method == "GET" or request.path in ("/ws", "/ws/terminal"):
        return await handler(request)

    ip = _get_client_ip(request)
    now = time.time()
    rate_limits: dict[str, deque[float]] = request.app["rate_limits"]
    timestamps = rate_limits[ip]
    # Prune old entries from the front of the sorted deque — O(k) for k expired
    cutoff = now - _RATE_LIMIT_WINDOW
    while timestamps and timestamps[0] <= cutoff:
        timestamps.popleft()

    if len(timestamps) >= _RATE_LIMIT_REQUESTS:
        return json_error("Rate limit exceeded. Try again later.", 429)

    timestamps.append(now)

    # Periodic cleanup: evict IPs with no recent activity to prevent unbounded growth
    global _rate_limit_last_cleanup
    if now - _rate_limit_last_cleanup > _RATE_LIMIT_CLEANUP_INTERVAL:
        _rate_limit_last_cleanup = now
        stale = [k for k, v in rate_limits.items() if not v or v[-1] < cutoff]
        for k in stale:
            del rate_limits[k]
        # Hard cap: if still too many IPs, drop the least recently active
        if len(rate_limits) > _RATE_LIMIT_MAX_IPS:

            def _last_ts(k: str) -> float:
                return rate_limits[k][-1] if rate_limits[k] else 0

            by_recency = sorted(rate_limits, key=_last_ts)
            excess = len(rate_limits) - _RATE_LIMIT_MAX_IPS
            for k in by_recency[:excess]:
                del rate_limits[k]

    return await handler(request)


def _validate_priority(raw: str) -> TaskPriority:
    """Parse and validate a priority string. Raises SwarmOperationError on invalid."""
    try:
        return validate_priority(raw)
    except ValueError as e:
        raise SwarmOperationError(str(e)) from e


def _validate_task_type(raw: str) -> TaskType:
    """Parse and validate a task_type string. Raises SwarmOperationError on invalid."""
    try:
        return validate_task_type(raw)
    except ValueError as e:
        raise SwarmOperationError(str(e)) from e


def _handle_errors(
    handler: Callable[[web.Request], Awaitable[web.Response]],
) -> Callable[[web.Request], Awaitable[web.Response]]:
    """Decorator that maps common exceptions to HTTP error responses.

    - WorkerNotFoundError → 404
    - TaskOperationError → 404 (not found) or 409 (wrong state)
    - SwarmOperationError → 400
    - ValueError → 400 (validation errors from config parsing)
    - Exception → 500 (broad catch for HTTP error boundary)
    """

    async def wrapper(request: web.Request) -> web.Response:
        try:
            return await handler(request)
        except json.JSONDecodeError:
            return json_error("Invalid JSON in request body")
        except WorkerNotFoundError as e:
            return json_error(str(e), 404)
        except TaskOperationError as e:
            return json_error(str(e), e.status_code)
        except (SwarmOperationError, ValueError) as e:
            return json_error(str(e))
        except Exception:
            _log.exception("unhandled error in %s", handler.__name__)
            return json_error("Internal server error", 500)

    wrapper.__name__ = handler.__name__
    wrapper.__doc__ = handler.__doc__
    return wrapper


async def _worker_action(
    request: web.Request,
    action: Callable[[SwarmDaemon, str], Awaitable[None]],
    success_status: str,
) -> web.Response:
    """Common handler for worker-targeted actions with WorkerNotFoundError→404."""
    d = get_daemon(request)
    name = request.match_info["name"]
    try:
        await action(d, name)
    except WorkerNotFoundError:
        return json_error(f"Worker '{name}' not found", 404)
    except SwarmOperationError as e:
        return json_error(str(e))
    return web.json_response({"status": success_status, "worker": name})


# --- Health ---


async def handle_health(request: web.Request) -> web.Response:
    from swarm.update import _get_installed_version, build_sha

    d = get_daemon(request)
    pilot_info: dict[str, object] = {}
    if d.pilot:
        pilot_info = d.pilot.get_diagnostics()
    return web.json_response(
        {
            "status": "ok",
            "workers": len(d.workers),
            "drones_enabled": d.pilot.enabled if d.pilot else False,
            "uptime": time.time() - d.start_time,
            "pilot": pilot_info,
            "version": _get_installed_version(),
            "build_sha": build_sha(),
        }
    )


# --- Workers ---


async def handle_workers(request: web.Request) -> web.Response:
    d = get_daemon(request)
    workers = []
    for w in d.workers:
        wd = w.to_api_dict()
        wd["in_config"] = d.config.get_worker(w.name) is not None
        workers.append(wd)
    return web.json_response({"workers": workers})


async def handle_worker_detail(request: web.Request) -> web.Response:
    d = get_daemon(request)
    name = request.match_info["name"]
    worker = d.get_worker(name)
    if not worker:
        return json_error(f"Worker '{name}' not found", 404)

    try:
        content = await d.capture_worker_output(name)
    except (ProcessError, OSError):
        content = "(output unavailable)"

    result = worker.to_api_dict()
    result["worker_output"] = content
    return web.json_response(result)


@_handle_errors
async def handle_worker_send(request: web.Request) -> web.Response:
    d = get_daemon(request)
    name = request.match_info["name"]

    body = await request.json()
    result = require_message(body)
    if isinstance(result, web.Response):
        return result
    message = result

    await d.send_to_worker(name, message)
    return web.json_response({"status": "sent", "worker": name})


async def handle_worker_continue(request: web.Request) -> web.Response:
    return await _worker_action(request, lambda d, n: d.continue_worker(n), "continued")


async def handle_worker_kill(request: web.Request) -> web.Response:
    return await _worker_action(request, lambda d, n: d.kill_worker(n), "killed")


# --- Drones ---


async def handle_drone_log(request: web.Request) -> web.Response:
    d = get_daemon(request)
    limit = parse_limit(request)

    # If query params request SQLite-backed queries, use the store
    worker = request.query.get("worker")
    action = request.query.get("action")
    category = request.query.get("category")
    since_str = request.query.get("since")
    overridden_str = request.query.get("overridden")

    use_store = any([worker, action, category, since_str, overridden_str])

    if use_store and d.drone_log.store is not None:
        since = float(since_str) if since_str else None
        overridden = None
        if overridden_str is not None:
            overridden = overridden_str.lower() in ("true", "1", "yes")
        rows = d.drone_log.query(
            worker_name=worker,
            action=action.upper() if action else None,
            category=category,
            since=since,
            overridden=overridden,
            limit=limit,
        )
        return web.json_response({"entries": rows})

    # Default: in-memory entries (backward compatible)
    entries = d.drone_log.entries[-limit:]
    return web.json_response(
        {
            "entries": [
                {
                    "time": e.formatted_time,
                    "action": e.action.value.lower(),
                    "worker": e.worker_name,
                    "detail": e.detail,
                }
                for e in entries
            ]
        }
    )


async def handle_tuning_suggestions(request: web.Request) -> web.Response:
    """Return auto-tuning suggestions based on override patterns."""
    from swarm.drones.tuning import analyze_overrides

    d = get_daemon(request)
    store = d.drone_log.store
    if store is None:
        return web.json_response({"suggestions": []})
    days = int(request.query.get("days", "7"))
    suggestions = analyze_overrides(store, days=days)
    return web.json_response(
        {
            "suggestions": [
                {
                    "id": s.id,
                    "description": s.description,
                    "config_path": s.config_path,
                    "current_value": s.current_value,
                    "suggested_value": s.suggested_value,
                    "reason": s.reason,
                    "override_count": s.override_count,
                    "total_decisions": s.total_decisions,
                    "override_rate": round(s.override_rate, 2),
                }
                for s in suggestions
            ]
        }
    )


async def handle_notification_history(request: web.Request) -> web.Response:
    """Return recent notification history."""
    d = get_daemon(request)
    limit = min(int(request.query.get("limit", "50")), 50)
    history = d._notification_history[-limit:]
    return web.json_response({"notifications": list(reversed(history))})


async def handle_oversight_status(request: web.Request) -> web.Response:
    """Return Queen oversight monitor status."""
    d = get_daemon(request)
    monitor = getattr(d, "_oversight_monitor", None)
    if monitor is None:
        return web.json_response({"enabled": False})
    return web.json_response(monitor.get_status())


async def handle_ownership_status(request: web.Request) -> web.Response:
    """Return file ownership map status."""
    d = get_daemon(request)
    ownership = getattr(d, "file_ownership", None)
    if ownership is None:
        return web.json_response({"mode": "off"})
    return web.json_response(ownership.to_dict())


async def handle_sync_status(request: web.Request) -> web.Response:
    """Return auto-pull sync status."""
    d = get_daemon(request)
    sync = getattr(d, "auto_pull", None)
    if sync is None:
        return web.json_response({"enabled": False})
    return web.json_response(sync.get_status())


async def handle_drone_status(request: web.Request) -> web.Response:
    d = get_daemon(request)
    return web.json_response(
        {
            "enabled": d.pilot.enabled if d.pilot else False,
        }
    )


async def handle_drone_toggle(request: web.Request) -> web.Response:
    d = get_daemon(request)
    if d.pilot:
        new_state = d.toggle_drones()
        return web.json_response({"enabled": new_state})
    return json_error("pilot not running")


# --- Tasks ---


async def handle_tasks(request: web.Request) -> web.Response:
    d = get_daemon(request)
    tasks = d.task_board.all_tasks
    return web.json_response(
        {
            "tasks": [
                {
                    "id": t.id,
                    "title": t.title,
                    "description": t.description,
                    "status": t.status.value,
                    "priority": t.priority.value,
                    "task_type": t.task_type.value,
                    "assigned_worker": t.assigned_worker,
                }
                for t in tasks
            ],
            "summary": d.task_board.summary(),
        }
    )


@_handle_errors
async def handle_create_task(request: web.Request) -> web.Response:
    d = get_daemon(request)
    body = await request.json()
    title = body.get("title", "")
    if not isinstance(title, str):
        title = ""
    title = title.strip()
    if len(title) > 500:
        return json_error("Task title too long (max 500 characters)")
    description = body.get("description", "")
    if isinstance(description, str) and len(description) > 10_000:
        return json_error("Task description too long (max 10000 characters)")

    priority = _validate_priority(body.get("priority", "normal"))

    type_str = body.get("task_type", "")
    task_type = None
    if type_str:
        task_type = _validate_task_type(type_str)

    task = await d.create_task_smart(
        title=title,
        description=description,
        priority=priority,
        task_type=task_type,
    )
    return web.json_response({"id": task.id, "title": task.title}, status=201)


async def handle_create_task_from_email(request: web.Request) -> web.Response:
    """Parse a .eml file and return extracted data for the create-task modal.

    Does NOT create a task — just parses and saves attachments.
    Returns ``{title, description, attachments: [path, ...]}``.
    """
    d = get_daemon(request)
    try:
        filename, data = await read_file_field(request)
    except ValueError as e:
        return json_error(str(e))

    from swarm.tasks.task import parse_email, smart_title

    parsed = parse_email(data, filename=filename)
    subject = parsed.get("subject", "")
    body = parsed.get("body", "")

    # Use subject as title, or generate a smart title from body
    title = subject.strip()
    if not title and body:
        title = await smart_title(body)

    # Save attachments to disk so they're ready when the task is created
    attachment_paths: list[str] = []
    for att in parsed.get("attachments", []):
        path = d.save_attachment(att["filename"], att["data"])
        attachment_paths.append(path)

    return web.json_response(
        {
            "title": title or "",
            "description": body or "",
            "attachments": attachment_paths,
            "message_id": parsed.get("message_id", ""),
        }
    )


@_handle_errors
async def handle_assign_task(request: web.Request) -> web.Response:
    d = get_daemon(request)
    task_id = request.match_info["task_id"]
    body = await request.json()
    worker_name = body.get("worker", "")
    if not worker_name:
        return json_error("worker required")

    await d.assign_task(task_id, worker_name)
    return web.json_response({"status": "assigned", "task_id": task_id, "worker": worker_name})


@_handle_errors
async def handle_complete_task(request: web.Request) -> web.Response:
    d = get_daemon(request)
    task_id = request.match_info["task_id"]
    body = await request.json() if request.can_read_body else {}
    resolution = body.get("resolution", "") if body else ""
    d.complete_task(task_id, resolution=resolution)
    return web.json_response({"status": "completed", "task_id": task_id})


@_handle_errors
async def handle_fail_task(request: web.Request) -> web.Response:
    d = get_daemon(request)
    task_id = request.match_info["task_id"]
    d.fail_task(task_id)
    return web.json_response({"status": "failed", "task_id": task_id})


@_handle_errors
async def handle_unassign_task(request: web.Request) -> web.Response:
    d = get_daemon(request)
    task_id = request.match_info["task_id"]
    d.unassign_task(task_id)
    return web.json_response({"status": "unassigned", "task_id": task_id})


@_handle_errors
async def handle_reopen_task(request: web.Request) -> web.Response:
    d = get_daemon(request)
    task_id = request.match_info["task_id"]
    d.reopen_task(task_id)
    return web.json_response({"status": "reopened", "task_id": task_id})


@_handle_errors
async def handle_remove_task(request: web.Request) -> web.Response:
    d = get_daemon(request)
    task_id = request.match_info["task_id"]
    d.remove_task(task_id)
    return web.json_response({"status": "removed", "task_id": task_id})


def _validate_edit_body(body: dict[str, Any]) -> web.Response | None:
    """Return an error Response if edit body fields are invalid, else None."""
    if "title" in body:
        raw_title = body["title"]
        if isinstance(raw_title, str) and len(raw_title) > 500:
            return json_error("Task title too long (max 500 characters)")
    if "description" in body:
        desc = body["description"]
        if isinstance(desc, str) and len(desc) > 10_000:
            return json_error("Task description too long (max 10000 characters)")
    if "attachments" in body:
        uploads_dir = (Path.home() / ".swarm" / "uploads").resolve()
        for att in body["attachments"]:
            att_path = Path(att).resolve()
            if not att_path.is_relative_to(uploads_dir):
                return json_error("attachment path outside uploads directory", 400)
    return None


@_handle_errors
async def handle_edit_task(request: web.Request) -> web.Response:
    d = get_daemon(request)
    task_id = request.match_info["task_id"]
    body = await request.json()

    err = _validate_edit_body(body)
    if err is not None:
        return err

    kwargs: dict[str, Any] = {}
    if "title" in body:
        title = await d.tasks.resolve_title(body["title"], body.get("description", ""), task_id)
        if not title:
            return json_error("title or description required to generate title")
        kwargs["title"] = title
    if "description" in body:
        kwargs["description"] = body["description"]
    if "priority" in body:
        kwargs["priority"] = _validate_priority(body["priority"])
    if "task_type" in body:
        kwargs["task_type"] = _validate_task_type(body["task_type"])
    if "tags" in body:
        kwargs["tags"] = body["tags"]
    if "attachments" in body:
        kwargs["attachments"] = body["attachments"]

    d.edit_task(task_id, **kwargs)
    return web.json_response({"status": "updated", "task_id": task_id})


@_handle_errors
async def handle_upload_attachment(request: web.Request) -> web.Response:
    d = get_daemon(request)
    task_id = request.match_info["task_id"]

    task = d.task_board.get(task_id)
    if not task:
        return json_error(f"Task '{task_id}' not found", 404)

    filename, data = await read_file_field(request)
    path = d.save_attachment(filename, data)

    # Append to task attachments
    new_attachments = [*task.attachments, path]
    d.task_board.update(task_id, attachments=new_attachments)

    return web.json_response({"status": "uploaded", "path": path}, status=201)


async def handle_task_history(request: web.Request) -> web.Response:
    d = get_daemon(request)
    task_id = request.match_info["task_id"]
    limit = parse_limit(request)
    events = d.task_history.get_events(task_id, limit=limit)
    return web.json_response(
        {
            "events": [e.to_dict() for e in events],
        }
    )


@_handle_errors
async def handle_retry_draft(request: web.Request) -> web.Response:
    d = get_daemon(request)
    task_id = request.match_info["task_id"]
    await d.retry_draft_reply(task_id)
    return web.json_response({"status": "retrying", "task_id": task_id})


async def handle_worker_escape(request: web.Request) -> web.Response:
    return await _worker_action(request, lambda d, n: d.escape_worker(n), "escape_sent")


async def handle_worker_interrupt(request: web.Request) -> web.Response:
    return await _worker_action(request, lambda d, n: d.interrupt_worker(n), "interrupted")


async def handle_worker_revive(request: web.Request) -> web.Response:
    return await _worker_action(request, lambda d, n: d.revive_worker(n), "revived")


@_handle_errors
async def handle_worker_analyze(request: web.Request) -> web.Response:
    d = get_daemon(request)
    name = request.match_info["name"]
    result = await d.analyze_worker(name, force=True)
    return web.json_response(result)


@_handle_errors
async def handle_worker_merge(request: web.Request) -> web.Response:
    d = get_daemon(request)
    name = request.match_info["name"]
    result = await d.worker_svc.merge_worker(name)
    status = 200 if result.get("success") else 409
    return web.json_response(result, status=status)


@_handle_errors
async def handle_conflicts(request: web.Request) -> web.Response:
    d = get_daemon(request)
    conflicts = getattr(d, "_conflicts", [])
    return web.json_response({"conflicts": conflicts})


@_handle_errors
async def handle_workers_launch(request: web.Request) -> web.Response:
    d = get_daemon(request)
    body = await request.json() if request.can_read_body else {}
    requested = body.get("workers", [])

    # Determine which configs to launch
    running_names = {w.name.lower() for w in d.workers}
    if requested:
        configs = [
            wc
            for wc in d.config.workers
            if wc.name in requested and wc.name.lower() not in running_names
        ]
    else:
        configs = [wc for wc in d.config.workers if wc.name.lower() not in running_names]

    if not configs:
        return web.json_response({"status": "no_new_workers", "launched": []})

    launched = await d.launch_workers(configs)

    return web.json_response(
        {"status": "launched", "launched": [w.name for w in launched]},
        status=201,
    )


@_handle_errors
async def handle_workers_spawn(request: web.Request) -> web.Response:
    d = get_daemon(request)
    body = await request.json()
    name = body.get("name", "").strip()
    path = body.get("path", "").strip()
    provider = body.get("provider", "").strip()

    if not name:
        return json_error("name is required")
    if err := validate_worker_name(name):
        return json_error(err)
    if not path:
        return json_error("path is required")
    if provider and provider not in {"claude", "gemini", "codex"}:
        return json_error(f"Unknown provider '{provider}'")

    from swarm.config import WorkerConfig

    worker = await d.spawn_worker(WorkerConfig(name=name, path=path, provider=provider))

    return web.json_response(
        {"status": "spawned", "worker": worker.name},
        status=201,
    )


async def handle_workers_continue_all(request: web.Request) -> web.Response:
    d = get_daemon(request)
    count = await d.continue_all()
    return web.json_response({"status": "ok", "count": count})


@_handle_errors
async def handle_workers_send_all(request: web.Request) -> web.Response:
    d = get_daemon(request)
    body = await request.json()
    result = require_message(body)
    if isinstance(result, web.Response):
        return result
    count = await d.send_all(result)
    return web.json_response({"status": "sent", "count": count})


async def handle_workers_discover(request: web.Request) -> web.Response:
    d = get_daemon(request)
    workers = await d.discover()
    return web.json_response({"status": "ok", "workers": [{"name": w.name} for w in workers]})


async def handle_group_send(request: web.Request) -> web.Response:
    d = get_daemon(request)
    group_name = request.match_info["name"]
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return json_error("Invalid JSON in request body")
    result = require_message(body)
    if isinstance(result, web.Response):
        return result
    try:
        count = await d.send_group(group_name, result)
    except (ValueError, KeyError) as e:
        return json_error(str(e), 404)
    return web.json_response({"status": "sent", "group": group_name, "count": count})


@_handle_errors
async def handle_queen_coordinate(request: web.Request) -> web.Response:
    d = get_daemon(request)
    result = await d.coordinate_hive(force=True)
    return web.json_response(result)


async def handle_queen_queue(request: web.Request) -> web.Response:
    d = get_daemon(request)
    return web.json_response(d.queen_queue.status())


async def handle_usage(request: web.Request) -> web.Response:
    """Return per-worker, queen, and total token usage."""
    d = get_daemon(request)
    from swarm.worker.worker import TokenUsage

    workers_usage: dict[str, dict[str, object]] = {}
    total = TokenUsage()
    for w in d.workers:
        workers_usage[w.name] = w.usage.to_dict()
        total.add(w.usage)

    queen_usage = d.queen.usage.to_dict()
    queen_tu = d.queen.usage
    total.add(queen_tu)

    return web.json_response(
        {
            "workers": workers_usage,
            "queen": queen_usage,
            "total": total.to_dict(),
        }
    )


@_handle_errors
async def handle_session_kill(request: web.Request) -> web.Response:
    d = get_daemon(request)
    await d.kill_session()
    return web.json_response({"status": "killed"})


async def handle_drones_poll(request: web.Request) -> web.Response:
    d = get_daemon(request)
    if not d.pilot:
        return json_error("pilot not running")
    had_action = await d.poll_once()
    return web.json_response({"status": "ok", "had_action": had_action})


@_handle_errors
async def handle_upload(request: web.Request) -> web.Response:
    d = get_daemon(request)
    filename, data = await read_file_field(request)
    path = d.save_attachment(filename, data)
    return web.json_response({"status": "uploaded", "path": path}, status=201)


async def handle_server_stop(request: web.Request) -> web.Response:
    shutdown: asyncio.Event | None = request.app.get("shutdown_event")
    if shutdown:
        shutdown.set()
        return web.json_response({"status": "stopping"})
    return json_error("shutdown not available")


async def handle_server_restart(request: web.Request) -> web.Response:
    # Reinstall from local source so code changes take effect on restart
    from swarm.update import reinstall_from_local_source

    ok, output = await reinstall_from_local_source()
    if not ok:
        import logging

        logging.getLogger("swarm.api").warning(
            "Local reinstall failed (proceeding with restart): %s", output
        )

    restart_flag = request.app.get("restart_flag")
    if restart_flag is not None:
        restart_flag["requested"] = True
    shutdown: asyncio.Event | None = request.app.get("shutdown_event")
    if shutdown:
        shutdown.set()
        return web.json_response({"status": "restarting"})
    return json_error("shutdown not available")


# --- Config ---


async def handle_get_config(request: web.Request) -> web.Response:
    d = get_daemon(request)
    from swarm.config import serialize_config

    cfg = serialize_config(d.config)
    cfg.pop("api_password", None)
    return web.json_response(cfg)


@_handle_errors
async def handle_update_config(request: web.Request) -> web.Response:
    """Partial update of settings (drones, queen, notifications, top-level scalars)."""
    d = get_daemon(request)
    body = await request.json()
    await d.apply_config_update(body)

    from swarm.config import serialize_config

    cfg = serialize_config(d.config)
    cfg.pop("api_password", None)
    return web.json_response(cfg)


@_handle_errors
async def handle_add_config_worker(request: web.Request) -> web.Response:
    d = get_daemon(request)
    body = await request.json()
    name = body.get("name", "").strip()
    path = body.get("path", "").strip()

    if not name:
        return json_error("name is required")
    if err := validate_worker_name(name):
        return json_error(err)
    if not path:
        return json_error("path is required")

    # Strict path validation
    resolved = Path(path).expanduser().resolve()
    if not resolved.exists():
        return json_error(f"Path does not exist: {resolved}")

    # Check duplicate in config
    if d.config.get_worker(name):
        return json_error(f"Worker '{name}' already exists", 409)

    from swarm.config import WorkerConfig

    description = body.get("description", "").strip()
    provider = body.get("provider", "").strip()
    if provider and provider not in {"claude", "gemini", "codex"}:
        return json_error(f"Unknown provider '{provider}'")
    wc = WorkerConfig(name=name, path=str(resolved), description=description, provider=provider)
    d.config.workers.append(wc)

    try:
        await d.spawn_worker(wc)
    except Exception as e:
        # Rollback config on spawn failure
        d.config.workers.remove(wc)
        _log.exception("failed to spawn worker '%s'", name)
        return json_error(f"Failed to spawn worker: {e}", 500)

    d.save_config()
    return web.json_response(
        {"status": "added", "worker": name},
        status=201,
    )


@_handle_errors
async def handle_save_worker_to_config(request: web.Request) -> web.Response:
    """Save a running (spawned) worker to swarm.yaml."""
    d = get_daemon(request)
    name = request.match_info["name"]

    # Must be a running worker
    worker = d.get_worker(name)
    if not worker:
        return json_error(f"Worker '{name}' not found", 404)

    # Already in config?
    if d.config.get_worker(name):
        return json_error(f"Worker '{name}' is already in config", 409)

    from swarm.config import WorkerConfig

    # Inherit description from a config worker with the same path (e.g. the
    # source worker this one was duplicated from).
    description = ""
    for wc_existing in d.config.workers:
        if wc_existing.path == worker.path:
            description = wc_existing.description
            break

    wc = WorkerConfig(
        name=name,
        path=worker.path,
        provider=worker.provider_name,
        description=description,
    )
    d.config.workers.append(wc)
    d.save_config()
    return web.json_response({"status": "saved", "worker": name}, status=201)


@_handle_errors
async def handle_add_worker_to_group(request: web.Request) -> web.Response:
    """Add a worker to a config group (optionally creating the group)."""
    d = get_daemon(request)
    name = request.match_info["name"]

    body = await request.json()
    group_name = body.get("group", "").strip()
    create = body.get("create", False)

    if not group_name:
        return json_error("group is required")

    # Worker must exist (running or in config)
    if not d.get_worker(name) and not d.config.get_worker(name):
        return json_error(f"Worker '{name}' not found", 404)

    group = next((g for g in d.config.groups if g.name.lower() == group_name.lower()), None)
    if group:
        if name.lower() in [w.lower() for w in group.workers]:
            return json_error(f"Worker '{name}' is already in group '{group_name}'", 409)
        group.workers.append(name)
    elif create:
        from swarm.config import GroupConfig

        d.config.groups.append(GroupConfig(name=group_name, workers=[name]))
    else:
        return json_error(f"Group '{group_name}' not found", 404)

    d.save_config()
    return web.json_response({"status": "added", "worker": name, "group": group_name})


async def handle_remove_config_worker(request: web.Request) -> web.Response:
    d = get_daemon(request)
    name = request.match_info["name"]

    # Find and remove from config
    wc = d.config.get_worker(name)
    if not wc:
        return json_error(f"Worker '{name}' not found in config", 404)

    # Kill live worker if running (marks STUNG + unassigns tasks)
    try:
        await d.kill_worker(name)
    except WorkerNotFoundError:
        pass  # Worker not running — just remove from config

    d.config.workers = [w for w in d.config.workers if w.name.lower() != name.lower()]
    d.save_config()
    return web.json_response({"status": "removed", "worker": name})


@_handle_errors
async def handle_add_config_group(request: web.Request) -> web.Response:
    d = get_daemon(request)
    body = await request.json()
    name = body.get("name", "").strip()
    workers = body.get("workers", [])

    if not name:
        return json_error("name is required")
    if not isinstance(workers, list):
        return json_error("workers must be a list")

    # Check duplicate
    existing = [g for g in d.config.groups if g.name.lower() == name.lower()]
    if existing:
        return json_error(f"Group '{name}' already exists", 409)

    from swarm.config import GroupConfig

    d.config.groups.append(GroupConfig(name=name, workers=workers))
    d.save_config()
    return web.json_response({"status": "added", "group": name}, status=201)


@_handle_errors
async def handle_update_config_group(request: web.Request) -> web.Response:
    d = get_daemon(request)
    name = request.match_info["name"]
    body = await request.json()
    workers = body.get("workers", [])

    if not isinstance(workers, list):
        return json_error("workers must be a list")

    group = next((g for g in d.config.groups if g.name.lower() == name.lower()), None)
    if not group:
        return json_error(f"Group '{name}' not found", 404)

    # Handle rename
    new_name = body.get("name", "").strip()
    if new_name and new_name.lower() != group.name.lower():
        # Check duplicate
        existing = [g for g in d.config.groups if g.name.lower() == new_name.lower()]
        if existing:
            return json_error(f"Group '{new_name}' already exists", 409)
        old_name = group.name
        group.name = new_name
        # Update default_group if it referenced the old name
        if d.config.default_group and d.config.default_group.lower() == old_name.lower():
            d.config.default_group = new_name

    group.workers = workers
    d.save_config()
    return web.json_response({"status": "updated", "group": group.name, "workers": workers})


async def handle_remove_config_group(request: web.Request) -> web.Response:
    d = get_daemon(request)
    name = request.match_info["name"]

    before = len(d.config.groups)
    d.config.groups = [g for g in d.config.groups if g.name.lower() != name.lower()]
    if len(d.config.groups) == before:
        return json_error(f"Group '{name}' not found", 404)

    d.save_config()
    return web.json_response({"status": "removed", "group": name})


async def handle_list_projects(request: web.Request) -> web.Response:
    d = get_daemon(request)
    from swarm.config import discover_projects

    projects_dir = Path(d.config.projects_dir).expanduser().resolve()
    projects = discover_projects(projects_dir)
    return web.json_response(
        {
            "projects": [{"name": name, "path": path} for name, path in projects],
        }
    )


# --- Proposals ---


async def handle_proposals(request: web.Request) -> web.Response:
    d = get_daemon(request)
    pending = d.proposal_store.pending
    return web.json_response(
        {
            "proposals": [d.proposal_dict(p) for p in pending],
            "pending_count": len(pending),
        }
    )


@_handle_errors
async def handle_approve_proposal(request: web.Request) -> web.Response:
    d = get_daemon(request)
    proposal_id = request.match_info["proposal_id"]
    body = await request.json() if request.can_read_body else {}
    draft_response = bool(body.get("draft_response")) if body else False
    await d.approve_proposal(proposal_id, draft_response=draft_response)
    return web.json_response({"status": "approved", "proposal_id": proposal_id})


@_handle_errors
async def handle_reject_proposal(request: web.Request) -> web.Response:
    d = get_daemon(request)
    proposal_id = request.match_info["proposal_id"]
    d.reject_proposal(proposal_id)
    return web.json_response({"status": "rejected", "proposal_id": proposal_id})


async def handle_reject_all_proposals(request: web.Request) -> web.Response:
    d = get_daemon(request)
    count = d.reject_all_proposals()
    return web.json_response({"status": "rejected_all", "count": count})


async def handle_decisions(request: web.Request) -> web.Response:
    d = get_daemon(request)
    limit = parse_limit(request)
    history = d.proposal_store.history[:limit]
    return web.json_response({"decisions": [d.proposal_dict(p) for p in history]})


# --- Tunnel ---


async def handle_tunnel_start(request: web.Request) -> web.Response:
    d = get_daemon(request)
    if d.tunnel.is_running:
        return web.json_response(d.tunnel.to_dict())
    # Require an explicit api_password (env or config) before opening a public tunnel.
    # The auto-generated token isn't suitable because remote users can't discover it.
    explicit_pw = os.environ.get("SWARM_API_PASSWORD") or d.config.api_password
    if not explicit_pw:
        return json_error(
            "Set SWARM_API_PASSWORD or api_password in swarm.yaml before starting a public tunnel",
            400,
        )
    try:
        await d.tunnel.start()
    except RuntimeError as e:
        return json_error(str(e), 500)
    return web.json_response(d.tunnel.to_dict())


async def handle_tunnel_stop(request: web.Request) -> web.Response:
    d = get_daemon(request)
    await d.tunnel.stop()
    return web.json_response(d.tunnel.to_dict())


async def handle_tunnel_status(request: web.Request) -> web.Response:
    d = get_daemon(request)
    return web.json_response(d.tunnel.to_dict())


# --- WebSocket ---

_MAX_WS_PER_IP = 10


def _check_ws_access(request: web.Request) -> web.Response | None:
    """Validate origin, auth, and rate limit for WebSocket upgrade.

    Returns an error Response if rejected, or None if access is granted.
    """
    origin = request.headers.get("Origin", "")
    if origin and not _is_same_origin(request, origin):
        return web.Response(status=403, text="WebSocket origin rejected")

    d = get_daemon(request)
    password = _get_api_password(d)
    token = request.query.get("token", "")
    if not hmac.compare_digest(token, password):
        return web.Response(status=401, text="Unauthorized")

    ip = _get_client_ip(request)
    ws_ip_counts: dict[str, int] = request.app.setdefault("_ws_ip_counts", {})
    if ws_ip_counts.get(ip, 0) >= _MAX_WS_PER_IP:
        return web.Response(status=429, text="Too many WebSocket connections")
    return None


async def handle_websocket(request: web.Request) -> web.WebSocketResponse:
    rejection = _check_ws_access(request)
    if rejection is not None:
        return rejection

    d = get_daemon(request)

    # Per-IP WebSocket connection tracking
    ip = _get_client_ip(request)
    ws_ip_counts: dict[str, int] = request.app.setdefault("_ws_ip_counts", {})
    ws_ip_counts[ip] = ws_ip_counts.get(ip, 0) + 1

    ws = web.WebSocketResponse(heartbeat=20.0)
    await ws.prepare(request)
    _log.info("WebSocket client connected")

    d.ws_clients.add(ws)
    try:
        # Send initial state
        pending_proposals = d.proposal_store.pending
        init_payload: dict[str, object] = {
            "type": "init",
            "workers": [{"name": w.name, "state": w.display_state.value} for w in d.workers],
            "drones_enabled": d.pilot.enabled if d.pilot else False,
            "proposals": [d.proposal_dict(p) for p in pending_proposals],
            "proposal_count": len(pending_proposals),
            "queen_queue": d.queen_queue.status(),
            "test_mode": hasattr(d, "_test_log"),
            "test_run_id": d._test_log.run_id if hasattr(d, "_test_log") else None,
        }
        if getattr(d, "_update_result", None) is not None:
            from swarm.update import update_result_to_dict

            init_payload["update"] = update_result_to_dict(d._update_result)
        await ws.send_json(init_payload)

        async for msg in ws:
            if msg.type == web.WSMsgType.TEXT:
                try:
                    data = json.loads(msg.data)
                except json.JSONDecodeError:
                    await ws.send_json({"type": "error", "message": "invalid JSON"})
                    continue
                try:
                    await _handle_ws_command(d, ws, data)
                except Exception:
                    _log.exception("error handling WS command: %s", data.get("command", ""))
                    await ws.send_json({"type": "error", "message": "internal error"})
            elif msg.type == web.WSMsgType.ERROR:
                _log.warning("WebSocket error: %s", ws.exception())
    finally:
        d.ws_clients.discard(ws)
        count = ws_ip_counts.get(ip, 0)
        if count > 1:
            ws_ip_counts[ip] = count - 1
        else:
            ws_ip_counts.pop(ip, None)
        _log.info("WebSocket client disconnected")

    return ws


_ALLOWED_WS_COMMANDS = {"refresh", "toggle_drones", "focus"}


async def _handle_ws_command(
    d: SwarmDaemon, ws: web.WebSocketResponse, data: dict[str, Any]
) -> None:
    """Handle a command received over WebSocket."""
    cmd = data.get("command", "")

    if cmd not in _ALLOWED_WS_COMMANDS:
        await ws.send_json({"type": "error", "message": f"unknown command: {cmd}"})
        return

    if cmd == "refresh":
        await ws.send_json(
            {
                "type": "state",
                "workers": [
                    {
                        "name": w.name,
                        "state": w.display_state.value,
                        "state_duration": round(w.state_duration, 1),
                    }
                    for w in d.workers
                ],
            }
        )
    elif cmd == "toggle_drones":
        if d.pilot:
            new_state = d.toggle_drones()
            await ws.send_json({"type": "drones_toggled", "enabled": new_state})
    elif cmd == "focus":
        worker_name = data.get("worker", "")
        if d.pilot:
            d.pilot.set_focused_workers({worker_name} if worker_name else set())
