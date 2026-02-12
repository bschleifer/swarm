"""REST + WebSocket API server for the swarm daemon."""

from __future__ import annotations

import asyncio
import hmac
import json
import os
import re
import time
from collections import defaultdict
from pathlib import Path
from typing import TYPE_CHECKING

from aiohttp import web

from swarm.logging import get_logger
from swarm.server.daemon import SwarmOperationError, WorkerNotFoundError

if TYPE_CHECKING:
    from swarm.server.daemon import SwarmDaemon

_log = get_logger("server.api")

_WORKER_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]+$")
_MAX_QUERY_LIMIT = 1000
_RATE_LIMIT_REQUESTS = 60  # per minute
_RATE_LIMIT_WINDOW = 60  # seconds

# Paths that require authentication for mutating methods
_CONFIG_AUTH_PREFIX = "/api/config"


def _get_client_ip(request: web.Request) -> str:
    """Get client IP, checking X-Forwarded-For for proxied requests."""
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded:
        # First entry is the original client IP
        return forwarded.split(",")[0].strip()
    return request.remote or "unknown"


def _get_api_password(daemon: SwarmDaemon) -> str | None:
    """Get API password from config or environment."""
    return os.environ.get("SWARM_API_PASSWORD") or daemon.config.api_password


@web.middleware
async def _config_auth_middleware(request: web.Request, handler):
    """Require Bearer token for mutating config endpoints."""
    if request.path.startswith(_CONFIG_AUTH_PREFIX) and request.method in ("PUT", "POST", "DELETE"):
        daemon = _get_daemon(request)
        password = _get_api_password(daemon)
        if password:
            auth = request.headers.get("Authorization", "")
            if not auth.startswith("Bearer ") or not hmac.compare_digest(auth[7:], password):
                return web.json_response({"error": "Unauthorized"}, status=401)
    return await handler(request)


@web.middleware
async def _csrf_middleware(request: web.Request, handler):
    """Reject cross-origin mutating requests.

    Checks both Origin header and requires X-Requested-With on API calls.
    Web form POSTs (non-API) are exempt from the header check.
    """
    if request.method in ("POST", "PUT", "DELETE"):
        origin = request.headers.get("Origin", "")
        if origin:
            # Allow same-host requests (any port) and localhost
            req_host = request.host.split(":")[0] if request.host else ""
            origin_host = origin.split("://")[-1].split(":")[0]
            same_host = origin_host in ("localhost", "127.0.0.1") or origin_host == req_host
            if not same_host:
                return web.Response(status=403, text="CSRF rejected")
        # Require X-Requested-With for API endpoints (not form-submitted web actions)
        if request.path.startswith("/api/") and not request.headers.get("X-Requested-With"):
            return web.Response(status=403, text="Missing X-Requested-With header")
    return await handler(request)


def create_app(daemon: SwarmDaemon, enable_web: bool = True) -> web.Application:
    """Create the aiohttp application with all routes."""
    app = web.Application(
        client_max_size=20 * 1024 * 1024,  # 20 MB for file uploads
        middlewares=[
            _csrf_middleware,
            _rate_limit_middleware,
            _config_auth_middleware,
        ],
    )
    app["daemon"] = daemon
    app["rate_limits"] = defaultdict(list)  # ip -> [timestamps]

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

    # Drones
    app.router.add_get("/api/drones/log", handle_drone_log)
    app.router.add_get("/api/drones/status", handle_drone_status)
    app.router.add_post("/api/drones/toggle", handle_drone_toggle)
    app.router.add_post("/api/drones/poll", handle_drones_poll)

    # Groups
    app.router.add_post("/api/groups/{name}/send", handle_group_send)

    # Queen
    app.router.add_post("/api/queen/coordinate", handle_queen_coordinate)

    # Session
    app.router.add_post("/api/session/kill", handle_session_kill)

    # Server
    app.router.add_post("/api/server/stop", handle_server_stop)

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

    # Serve uploaded files
    uploads_dir = Path.home() / ".swarm" / "uploads"
    uploads_dir.mkdir(parents=True, exist_ok=True)
    app.router.add_static("/uploads/", uploads_dir)

    app.router.add_get("/ws", handle_websocket)

    from swarm.server.terminal import handle_terminal_ws

    app.router.add_get("/ws/terminal", handle_terminal_ws)

    # Config endpoints
    app.router.add_get("/api/config", handle_get_config)
    app.router.add_put("/api/config", handle_update_config)
    app.router.add_post("/api/config/workers", handle_add_config_worker)
    app.router.add_delete("/api/config/workers/{name}", handle_remove_config_worker)
    app.router.add_post("/api/config/groups", handle_add_config_group)
    app.router.add_put("/api/config/groups/{name}", handle_update_config_group)
    app.router.add_delete("/api/config/groups/{name}", handle_remove_config_group)
    app.router.add_get("/api/config/projects", handle_list_projects)

    return app


@web.middleware
async def _rate_limit_middleware(request: web.Request, handler):
    """Simple in-memory rate limiter: N requests/minute per client IP."""
    # Exempt WebSocket upgrades and health checks
    if request.path in ("/ws", "/ws/terminal", "/api/health"):
        return await handler(request)

    ip = _get_client_ip(request)
    now = time.time()
    timestamps = request.app["rate_limits"][ip]
    # Prune old entries
    cutoff = now - _RATE_LIMIT_WINDOW
    timestamps[:] = [t for t in timestamps if t > cutoff]

    if len(timestamps) >= _RATE_LIMIT_REQUESTS:
        return web.json_response(
            {"error": "Rate limit exceeded. Try again later."},
            status=429,
        )

    timestamps.append(now)
    return await handler(request)


def _get_daemon(request: web.Request) -> SwarmDaemon:
    return request.app["daemon"]


def _validate_worker_name(name: str) -> str | None:
    """Validate worker name, return error message or None."""
    if not name or not _WORKER_NAME_RE.match(name):
        return f"Invalid worker name: '{name}'. Use alphanumeric, dash, or underscore only."
    return None


# --- Health ---


async def handle_health(request: web.Request) -> web.Response:
    d = _get_daemon(request)
    return web.json_response(
        {
            "status": "ok",
            "workers": len(d.workers),
            "drones_enabled": d.pilot.enabled if d.pilot else False,
            "uptime": time.time() - d.start_time,
        }
    )


# --- Workers ---


async def handle_workers(request: web.Request) -> web.Response:
    d = _get_daemon(request)
    workers = []
    for w in list(d.workers):
        workers.append(
            {
                "name": w.name,
                "path": w.path,
                "pane_id": w.pane_id,
                "state": w.state.value,
                "state_duration": round(w.state_duration, 1),
                "revive_count": w.revive_count,
            }
        )
    return web.json_response({"workers": workers})


async def handle_worker_detail(request: web.Request) -> web.Response:
    d = _get_daemon(request)
    name = request.match_info["name"]
    worker = d.get_worker(name)
    if not worker:
        return web.json_response({"error": f"Worker '{name}' not found"}, status=404)

    try:
        content = await d.capture_worker_output(name)
    except Exception:
        content = "(pane unavailable)"

    return web.json_response(
        {
            "name": worker.name,
            "path": worker.path,
            "pane_id": worker.pane_id,
            "state": worker.state.value,
            "state_duration": round(worker.state_duration, 1),
            "revive_count": worker.revive_count,
            "pane_content": content,
        }
    )


async def handle_worker_send(request: web.Request) -> web.Response:
    d = _get_daemon(request)
    name = request.match_info["name"]

    body = await request.json()
    message = body.get("message", "")
    if not isinstance(message, str) or not message.strip():
        return web.json_response({"error": "message must be a non-empty string"}, status=400)

    try:
        await d.send_to_worker(name, message)
    except WorkerNotFoundError:
        return web.json_response({"error": f"Worker '{name}' not found"}, status=404)
    return web.json_response({"status": "sent", "worker": name})


async def handle_worker_continue(request: web.Request) -> web.Response:
    d = _get_daemon(request)
    name = request.match_info["name"]
    try:
        await d.continue_worker(name)
    except WorkerNotFoundError:
        return web.json_response({"error": f"Worker '{name}' not found"}, status=404)
    return web.json_response({"status": "continued", "worker": name})


async def handle_worker_kill(request: web.Request) -> web.Response:
    d = _get_daemon(request)
    name = request.match_info["name"]
    try:
        await d.kill_worker(name)
    except WorkerNotFoundError:
        return web.json_response({"error": f"Worker '{name}' not found"}, status=404)
    return web.json_response({"status": "killed", "worker": name})


# --- Drones ---


async def handle_drone_log(request: web.Request) -> web.Response:
    d = _get_daemon(request)
    try:
        limit = min(int(request.query.get("limit", "50")), _MAX_QUERY_LIMIT)
    except ValueError:
        limit = 50
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


async def handle_drone_status(request: web.Request) -> web.Response:
    d = _get_daemon(request)
    return web.json_response(
        {
            "enabled": d.pilot.enabled if d.pilot else False,
        }
    )


async def handle_drone_toggle(request: web.Request) -> web.Response:
    d = _get_daemon(request)
    if d.pilot:
        new_state = d.toggle_drones()
        return web.json_response({"enabled": new_state})
    return web.json_response({"error": "pilot not running"}, status=400)


# --- Tasks ---


async def handle_tasks(request: web.Request) -> web.Response:
    d = _get_daemon(request)
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


async def handle_create_task(request: web.Request) -> web.Response:
    d = _get_daemon(request)
    body = await request.json()
    title = body.get("title", "")
    if not isinstance(title, str):
        title = ""
    title = title.strip()
    description = body.get("description", "")

    if not title:
        from swarm.tasks.task import smart_title

        title = await smart_title(description)
    if not title:
        return web.json_response({"error": "title or description required"}, status=400)

    from swarm.tasks.task import PRIORITY_MAP, TYPE_MAP, auto_classify_type

    pri_str = body.get("priority", "normal")
    if pri_str not in PRIORITY_MAP:
        opts = ", ".join(sorted(PRIORITY_MAP))
        return web.json_response(
            {"error": f"priority must be one of: {opts}"},
            status=400,
        )

    priority = PRIORITY_MAP[pri_str]

    type_str = body.get("task_type", "")
    if type_str and type_str not in TYPE_MAP:
        opts = ", ".join(sorted(TYPE_MAP))
        return web.json_response(
            {"error": f"task_type must be one of: {opts}"},
            status=400,
        )
    task_type = TYPE_MAP[type_str] if type_str else auto_classify_type(title, description)

    task = d.create_task(
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
    d = _get_daemon(request)
    reader = await request.multipart()
    field = await reader.next()
    if not field or field.name != "file":
        return web.json_response({"error": "file field required"}, status=400)

    filename = field.filename or ""

    data = await field.read(decode=False)
    if not data:
        return web.json_response({"error": "empty file"}, status=400)

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


async def handle_assign_task(request: web.Request) -> web.Response:
    d = _get_daemon(request)
    task_id = request.match_info["task_id"]
    body = await request.json()
    worker_name = body.get("worker", "")
    if not worker_name:
        return web.json_response({"error": "worker required"}, status=400)

    try:
        await d.assign_task(task_id, worker_name)
    except SwarmOperationError as e:
        return web.json_response({"error": str(e)}, status=404)
    return web.json_response({"status": "assigned", "task_id": task_id, "worker": worker_name})


async def handle_complete_task(request: web.Request) -> web.Response:
    d = _get_daemon(request)
    task_id = request.match_info["task_id"]
    body = await request.json() if request.can_read_body else {}
    resolution = body.get("resolution", "") if body else ""
    try:
        d.complete_task(task_id, resolution=resolution)
    except SwarmOperationError as e:
        return web.json_response({"error": str(e)}, status=404)
    return web.json_response({"status": "completed", "task_id": task_id})


async def handle_fail_task(request: web.Request) -> web.Response:
    d = _get_daemon(request)
    task_id = request.match_info["task_id"]
    try:
        d.fail_task(task_id)
    except SwarmOperationError as e:
        return web.json_response({"error": str(e)}, status=404)
    return web.json_response({"status": "failed", "task_id": task_id})


async def handle_unassign_task(request: web.Request) -> web.Response:
    d = _get_daemon(request)
    task_id = request.match_info["task_id"]
    try:
        d.unassign_task(task_id)
    except SwarmOperationError as e:
        return web.json_response({"error": str(e)}, status=404)
    return web.json_response({"status": "unassigned", "task_id": task_id})


async def handle_reopen_task(request: web.Request) -> web.Response:
    d = _get_daemon(request)
    task_id = request.match_info["task_id"]
    try:
        d.reopen_task(task_id)
    except SwarmOperationError as e:
        return web.json_response({"error": str(e)}, status=400)
    return web.json_response({"status": "reopened", "task_id": task_id})


async def handle_remove_task(request: web.Request) -> web.Response:
    d = _get_daemon(request)
    task_id = request.match_info["task_id"]
    try:
        d.remove_task(task_id)
    except SwarmOperationError as e:
        return web.json_response({"error": str(e)}, status=404)
    return web.json_response({"status": "removed", "task_id": task_id})


async def _resolve_title(title_raw: str, desc_hint: str, task_board, task_id: str) -> str:
    """Resolve a title: return as-is if non-empty, regenerate from description if empty."""
    title = title_raw.strip() if isinstance(title_raw, str) else ""
    if title:
        return title
    desc = desc_hint or ""
    if not desc:
        task = task_board.get(task_id)
        desc = task.description if task else ""
    if desc:
        from swarm.tasks.task import smart_title

        return await smart_title(desc)
    return ""


async def handle_edit_task(request: web.Request) -> web.Response:  # noqa: C901
    d = _get_daemon(request)
    task_id = request.match_info["task_id"]
    body = await request.json()

    from swarm.tasks.task import PRIORITY_MAP, TYPE_MAP

    kwargs: dict = {}
    if "title" in body:
        title = await _resolve_title(
            body["title"], body.get("description", ""), d.task_board, task_id
        )
        if not title:
            return web.json_response(
                {"error": "title or description required to generate title"}, status=400
            )
        kwargs["title"] = title
    if "description" in body:
        kwargs["description"] = body["description"]
    if "priority" in body:
        pri_str = body["priority"]
        if pri_str not in PRIORITY_MAP:
            return web.json_response({"error": "invalid priority"}, status=400)
        kwargs["priority"] = PRIORITY_MAP[pri_str]
    if "task_type" in body:
        type_str = body["task_type"]
        if type_str not in TYPE_MAP:
            return web.json_response({"error": "invalid task_type"}, status=400)
        kwargs["task_type"] = TYPE_MAP[type_str]
    if "tags" in body:
        kwargs["tags"] = body["tags"]
    if "attachments" in body:
        kwargs["attachments"] = body["attachments"]

    try:
        d.edit_task(task_id, **kwargs)
    except SwarmOperationError as e:
        return web.json_response({"error": str(e)}, status=404)
    return web.json_response({"status": "updated", "task_id": task_id})


async def handle_upload_attachment(request: web.Request) -> web.Response:
    d = _get_daemon(request)
    task_id = request.match_info["task_id"]

    task = d.task_board.get(task_id)
    if not task:
        return web.json_response({"error": f"Task '{task_id}' not found"}, status=404)

    reader = await request.multipart()
    field = await reader.next()
    if not field or field.name != "file":
        return web.json_response({"error": "file field required"}, status=400)

    filename = field.filename or "upload"
    data = await field.read(decode=False)
    path = d.save_attachment(filename, data)

    # Append to task attachments
    new_attachments = list(task.attachments) + [path]
    d.task_board.update(task_id, attachments=new_attachments)

    return web.json_response({"status": "uploaded", "path": path}, status=201)


async def handle_task_history(request: web.Request) -> web.Response:
    d = _get_daemon(request)
    task_id = request.match_info["task_id"]
    try:
        limit = min(int(request.query.get("limit", "50")), _MAX_QUERY_LIMIT)
    except ValueError:
        limit = 50
    events = d.task_history.get_events(task_id, limit=limit)
    return web.json_response(
        {
            "events": [e.to_dict() for e in events],
        }
    )


async def handle_retry_draft(request: web.Request) -> web.Response:
    d = _get_daemon(request)
    task_id = request.match_info["task_id"]
    try:
        await d.retry_draft_reply(task_id)
    except SwarmOperationError as e:
        return web.json_response({"error": str(e)}, status=400)
    return web.json_response({"status": "retrying", "task_id": task_id})


async def handle_worker_escape(request: web.Request) -> web.Response:
    d = _get_daemon(request)
    name = request.match_info["name"]
    try:
        await d.escape_worker(name)
    except WorkerNotFoundError:
        return web.json_response({"error": f"Worker '{name}' not found"}, status=404)
    return web.json_response({"status": "escape_sent", "worker": name})


async def handle_worker_interrupt(request: web.Request) -> web.Response:
    d = _get_daemon(request)
    name = request.match_info["name"]
    try:
        await d.interrupt_worker(name)
    except WorkerNotFoundError:
        return web.json_response({"error": f"Worker '{name}' not found"}, status=404)
    return web.json_response({"status": "interrupted", "worker": name})


async def handle_worker_revive(request: web.Request) -> web.Response:
    d = _get_daemon(request)
    name = request.match_info["name"]
    try:
        await d.revive_worker(name)
    except WorkerNotFoundError:
        return web.json_response({"error": f"Worker '{name}' not found"}, status=404)
    except SwarmOperationError as e:
        return web.json_response({"error": str(e)}, status=400)
    return web.json_response({"status": "revived", "worker": name})


async def handle_worker_analyze(request: web.Request) -> web.Response:
    d = _get_daemon(request)
    name = request.match_info["name"]
    if not d.queen.can_call:
        return web.json_response(
            {"error": "Queen on cooldown", "retry_after": d.queen.cooldown_remaining},
            status=429,
        )
    try:
        result = await d.analyze_worker(name)
    except WorkerNotFoundError:
        return web.json_response({"error": f"Worker '{name}' not found"}, status=404)
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)
    return web.json_response(result)


async def handle_workers_launch(request: web.Request) -> web.Response:
    d = _get_daemon(request)
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

    try:
        launched = await d.launch_workers(configs)
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)

    return web.json_response(
        {"status": "launched", "launched": [w.name for w in launched]},
        status=201,
    )


async def handle_workers_spawn(request: web.Request) -> web.Response:
    d = _get_daemon(request)
    body = await request.json()
    name = body.get("name", "").strip()
    path = body.get("path", "").strip()

    if not name:
        return web.json_response({"error": "name is required"}, status=400)
    if err := _validate_worker_name(name):
        return web.json_response({"error": err}, status=400)
    if not path:
        return web.json_response({"error": "path is required"}, status=400)

    from swarm.config import WorkerConfig

    try:
        worker = await d.spawn_worker(WorkerConfig(name=name, path=path))
    except SwarmOperationError as e:
        return web.json_response({"error": str(e)}, status=409)
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)

    return web.json_response(
        {"status": "spawned", "worker": worker.name, "pane_id": worker.pane_id},
        status=201,
    )


async def handle_workers_continue_all(request: web.Request) -> web.Response:
    d = _get_daemon(request)
    count = await d.continue_all()
    return web.json_response({"status": "ok", "count": count})


async def handle_workers_send_all(request: web.Request) -> web.Response:
    d = _get_daemon(request)
    body = await request.json()
    message = body.get("message", "")
    if not isinstance(message, str) or not message.strip():
        return web.json_response({"error": "message must be a non-empty string"}, status=400)
    count = await d.send_all(message)
    return web.json_response({"status": "sent", "count": count})


async def handle_workers_discover(request: web.Request) -> web.Response:
    d = _get_daemon(request)
    workers = await d.discover()
    return web.json_response(
        {"status": "ok", "workers": [{"name": w.name, "pane_id": w.pane_id} for w in workers]}
    )


async def handle_group_send(request: web.Request) -> web.Response:
    d = _get_daemon(request)
    group_name = request.match_info["name"]
    body = await request.json()
    message = body.get("message", "")
    if not isinstance(message, str) or not message.strip():
        return web.json_response({"error": "message must be a non-empty string"}, status=400)
    try:
        count = await d.send_group(group_name, message)
    except (ValueError, KeyError) as e:
        return web.json_response({"error": str(e)}, status=404)
    return web.json_response({"status": "sent", "group": group_name, "count": count})


async def handle_queen_coordinate(request: web.Request) -> web.Response:
    d = _get_daemon(request)
    if not d.queen.can_call:
        return web.json_response(
            {"error": "Queen on cooldown", "retry_after": d.queen.cooldown_remaining},
            status=429,
        )
    try:
        result = await d.coordinate_hive()
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)
    return web.json_response(result)


async def handle_session_kill(request: web.Request) -> web.Response:
    d = _get_daemon(request)
    try:
        await d.kill_session()
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)
    return web.json_response({"status": "killed"})


async def handle_drones_poll(request: web.Request) -> web.Response:
    d = _get_daemon(request)
    if not d.pilot:
        return web.json_response({"error": "pilot not running"}, status=400)
    had_action = await d.poll_once()
    return web.json_response({"status": "ok", "had_action": had_action})


async def handle_upload(request: web.Request) -> web.Response:
    d = _get_daemon(request)
    reader = await request.multipart()
    field = await reader.next()
    if not field or field.name != "file":
        return web.json_response({"error": "file field required"}, status=400)
    filename = field.filename or "upload"
    data = await field.read(decode=False)
    path = d.save_attachment(filename, data)
    return web.json_response({"status": "uploaded", "path": path}, status=201)


async def handle_server_stop(request: web.Request) -> web.Response:
    shutdown: asyncio.Event | None = request.app.get("shutdown_event")
    if shutdown:
        shutdown.set()
        return web.json_response({"status": "stopping"})
    return web.json_response({"error": "shutdown not available"}, status=400)


# --- Config ---


async def handle_get_config(request: web.Request) -> web.Response:
    d = _get_daemon(request)
    from swarm.config import serialize_config

    cfg = serialize_config(d.config)
    cfg.pop("api_password", None)
    return web.json_response(cfg)


async def handle_update_config(request: web.Request) -> web.Response:  # noqa: C901
    """Partial update of settings (drones, queen, notifications, top-level scalars)."""
    d = _get_daemon(request)
    body = await request.json()

    # Apply drones updates
    if "drones" in body:
        bz = body["drones"]
        cfg = d.config.drones
        for key in (
            "enabled",
            "escalation_threshold",
            "poll_interval",
            "auto_approve_yn",
            "max_revive_attempts",
            "max_poll_failures",
            "max_idle_interval",
            "auto_stop_on_complete",
        ):
            if key in bz:
                val = bz[key]
                if key in ("enabled", "auto_approve_yn", "auto_stop_on_complete"):
                    if not isinstance(val, bool):
                        return web.json_response(
                            {"error": f"drones.{key} must be boolean"},
                            status=400,
                        )
                else:
                    if not isinstance(val, (int, float)):
                        return web.json_response(
                            {"error": f"drones.{key} must be a number"},
                            status=400,
                        )
                    if val < 0:
                        return web.json_response(
                            {"error": f"drones.{key} must be >= 0"},
                            status=400,
                        )
                setattr(cfg, key, val)
        if "approval_rules" in bz:
            rules_raw = bz["approval_rules"]
            if not isinstance(rules_raw, list):
                return web.json_response(
                    {"error": "drones.approval_rules must be a list"}, status=400
                )
            from swarm.config import DroneApprovalRule

            parsed_rules = []
            for i, r in enumerate(rules_raw):
                if not isinstance(r, dict):
                    return web.json_response(
                        {"error": f"drones.approval_rules[{i}] must be an object"}, status=400
                    )
                pattern = r.get("pattern", "")
                action = r.get("action", "approve")
                if action not in ("approve", "escalate"):
                    msg = f"drones.approval_rules[{i}].action must be 'approve' or 'escalate'"
                    return web.json_response({"error": msg}, status=400)
                try:
                    import re

                    re.compile(pattern)
                except re.error as exc:
                    return web.json_response(
                        {"error": f"drones.approval_rules[{i}].pattern: invalid regex: {exc}"},
                        status=400,
                    )
                parsed_rules.append(DroneApprovalRule(pattern=pattern, action=action))
            d.config.drones.approval_rules = parsed_rules

    # Apply queen updates
    if "queen" in body:
        qn = body["queen"]
        cfg = d.config.queen
        if "cooldown" in qn:
            if not isinstance(qn["cooldown"], (int, float)) or qn["cooldown"] < 0:
                return web.json_response(
                    {"error": "queen.cooldown must be a non-negative number"},
                    status=400,
                )
            cfg.cooldown = qn["cooldown"]
        if "enabled" in qn:
            if not isinstance(qn["enabled"], bool):
                return web.json_response({"error": "queen.enabled must be boolean"}, status=400)
            cfg.enabled = qn["enabled"]
        if "system_prompt" in qn:
            if not isinstance(qn["system_prompt"], str):
                return web.json_response(
                    {"error": "queen.system_prompt must be a string"}, status=400
                )
            cfg.system_prompt = qn["system_prompt"]
        if "min_confidence" in qn:
            val = qn["min_confidence"]
            if not isinstance(val, (int, float)) or not (0.0 <= val <= 1.0):
                return web.json_response(
                    {"error": "queen.min_confidence must be a number between 0.0 and 1.0"},
                    status=400,
                )
            cfg.min_confidence = float(val)

    # Apply notifications updates
    if "notifications" in body:
        nt = body["notifications"]
        cfg = d.config.notifications
        for key in ("terminal_bell", "desktop"):
            if key in nt:
                if not isinstance(nt[key], bool):
                    return web.json_response(
                        {"error": f"notifications.{key} must be boolean"},
                        status=400,
                    )
                setattr(cfg, key, nt[key])
        if "debounce_seconds" in nt:
            if not isinstance(nt["debounce_seconds"], (int, float)) or nt["debounce_seconds"] < 0:
                return web.json_response(
                    {"error": "notifications.debounce_seconds must be >= 0"},
                    status=400,
                )
            cfg.debounce_seconds = nt["debounce_seconds"]

    # Worker description updates: {"workers": {"name": "desc", ...}}
    if "workers" in body and isinstance(body["workers"], dict):
        for wname, desc in body["workers"].items():
            wc = d.config.get_worker(wname)
            if wc and isinstance(desc, str):
                wc.description = desc

    # default_group update
    if "default_group" in body:
        dg = body["default_group"]
        if not isinstance(dg, str):
            return web.json_response({"error": "default_group must be a string"}, status=400)
        if dg:
            group_names = {g.name.lower() for g in d.config.groups}
            if dg.lower() not in group_names:
                return web.json_response(
                    {"error": f"default_group '{dg}' does not match any defined group"},
                    status=400,
                )
        d.config.default_group = dg

    # Top-level scalars (read-only warning fields are still editable, but user is warned)
    for key in ("session_name", "projects_dir", "log_level"):
        if key in body:
            setattr(d.config, key, body[key])

    # Graph integration settings
    if "graph_client_id" in body:
        cid = body["graph_client_id"]
        if isinstance(cid, str):
            d.config.graph_client_id = cid.strip()
    if "graph_tenant_id" in body:
        tid = body["graph_tenant_id"]
        if isinstance(tid, str):
            d.config.graph_tenant_id = tid.strip() or "common"

    # Workflows — task-type to skill-command mapping
    if "workflows" in body:
        wf = body["workflows"]
        if not isinstance(wf, dict):
            return web.json_response({"error": "workflows must be an object"}, status=400)
        valid_types = {"bug", "feature", "verify", "chore"}
        cleaned: dict[str, str] = {}
        for k, v in wf.items():
            if k not in valid_types:
                return web.json_response(
                    {"error": f"workflows key '{k}' is not a valid task type"}, status=400
                )
            if not isinstance(v, str):
                return web.json_response({"error": f"workflows.{k} must be a string"}, status=400)
            cleaned[k] = v.strip()
        d.config.workflows = cleaned
        from swarm.tasks.workflows import apply_config_overrides

        apply_config_overrides(cleaned)

    # Rebuild graph manager if client_id changed
    d.graph_mgr = d._build_graph_manager(d.config)

    # Hot-reload and save
    await d.reload_config(d.config)
    d.save_config()

    from swarm.config import serialize_config

    return web.json_response(serialize_config(d.config))


async def handle_add_config_worker(request: web.Request) -> web.Response:
    d = _get_daemon(request)
    body = await request.json()
    name = body.get("name", "").strip()
    path = body.get("path", "").strip()

    if not name:
        return web.json_response({"error": "name is required"}, status=400)
    if err := _validate_worker_name(name):
        return web.json_response({"error": err}, status=400)
    if not path:
        return web.json_response({"error": "path is required"}, status=400)

    # Strict path validation
    resolved = Path(path).expanduser().resolve()
    if not resolved.exists():
        return web.json_response({"error": f"Path does not exist: {resolved}"}, status=400)

    # Check duplicate in config
    if d.config.get_worker(name):
        return web.json_response({"error": f"Worker '{name}' already exists"}, status=409)

    from swarm.config import WorkerConfig

    description = body.get("description", "").strip()
    wc = WorkerConfig(name=name, path=str(resolved), description=description)
    d.config.workers.append(wc)

    try:
        worker = await d.spawn_worker(wc)
    except Exception as e:
        # Rollback config addition
        d.config.workers.remove(wc)
        return web.json_response({"error": f"Failed to create pane: {e}"}, status=500)

    d.save_config()
    return web.json_response(
        {"status": "added", "worker": name, "pane_id": worker.pane_id},
        status=201,
    )


async def handle_remove_config_worker(request: web.Request) -> web.Response:
    d = _get_daemon(request)
    name = request.match_info["name"]

    # Find and remove from config
    wc = d.config.get_worker(name)
    if not wc:
        return web.json_response({"error": f"Worker '{name}' not found in config"}, status=404)

    # Kill live worker if running (marks STUNG + unassigns tasks)
    try:
        await d.kill_worker(name)
    except WorkerNotFoundError:
        pass  # Worker not running — just remove from config

    d.config.workers = [w for w in d.config.workers if w.name.lower() != name.lower()]
    d.save_config()
    return web.json_response({"status": "removed", "worker": name})


async def handle_add_config_group(request: web.Request) -> web.Response:
    d = _get_daemon(request)
    body = await request.json()
    name = body.get("name", "").strip()
    workers = body.get("workers", [])

    if not name:
        return web.json_response({"error": "name is required"}, status=400)
    if not isinstance(workers, list):
        return web.json_response({"error": "workers must be a list"}, status=400)

    # Check duplicate
    existing = [g for g in d.config.groups if g.name.lower() == name.lower()]
    if existing:
        return web.json_response({"error": f"Group '{name}' already exists"}, status=409)

    from swarm.config import GroupConfig

    d.config.groups.append(GroupConfig(name=name, workers=workers))
    d.save_config()
    return web.json_response({"status": "added", "group": name}, status=201)


async def handle_update_config_group(request: web.Request) -> web.Response:
    d = _get_daemon(request)
    name = request.match_info["name"]
    body = await request.json()
    workers = body.get("workers", [])

    if not isinstance(workers, list):
        return web.json_response({"error": "workers must be a list"}, status=400)

    group = next((g for g in d.config.groups if g.name.lower() == name.lower()), None)
    if not group:
        return web.json_response({"error": f"Group '{name}' not found"}, status=404)

    # Handle rename
    new_name = body.get("name", "").strip()
    if new_name and new_name.lower() != group.name.lower():
        # Check duplicate
        existing = [g for g in d.config.groups if g.name.lower() == new_name.lower()]
        if existing:
            return web.json_response({"error": f"Group '{new_name}' already exists"}, status=409)
        old_name = group.name
        group.name = new_name
        # Update default_group if it referenced the old name
        if d.config.default_group and d.config.default_group.lower() == old_name.lower():
            d.config.default_group = new_name

    group.workers = workers
    d.save_config()
    return web.json_response({"status": "updated", "group": group.name, "workers": workers})


async def handle_remove_config_group(request: web.Request) -> web.Response:
    d = _get_daemon(request)
    name = request.match_info["name"]

    before = len(d.config.groups)
    d.config.groups = [g for g in d.config.groups if g.name.lower() != name.lower()]
    if len(d.config.groups) == before:
        return web.json_response({"error": f"Group '{name}' not found"}, status=404)

    d.save_config()
    return web.json_response({"status": "removed", "group": name})


async def handle_list_projects(request: web.Request) -> web.Response:
    d = _get_daemon(request)
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
    d = _get_daemon(request)
    pending = d.proposal_store.pending
    return web.json_response(
        {
            "proposals": [d._proposal_dict(p) for p in pending],
            "pending_count": len(pending),
        }
    )


async def handle_approve_proposal(request: web.Request) -> web.Response:
    d = _get_daemon(request)
    proposal_id = request.match_info["proposal_id"]
    body = await request.json() if request.can_read_body else {}
    draft_response = bool(body.get("draft_response")) if body else False
    try:
        await d.approve_proposal(proposal_id, draft_response=draft_response)
    except SwarmOperationError as e:
        return web.json_response({"error": str(e)}, status=404)
    return web.json_response({"status": "approved", "proposal_id": proposal_id})


async def handle_reject_proposal(request: web.Request) -> web.Response:
    d = _get_daemon(request)
    proposal_id = request.match_info["proposal_id"]
    try:
        d.reject_proposal(proposal_id)
    except SwarmOperationError as e:
        return web.json_response({"error": str(e)}, status=404)
    return web.json_response({"status": "rejected", "proposal_id": proposal_id})


async def handle_reject_all_proposals(request: web.Request) -> web.Response:
    d = _get_daemon(request)
    count = d.reject_all_proposals()
    return web.json_response({"status": "rejected_all", "count": count})


# --- WebSocket ---


async def handle_websocket(request: web.Request) -> web.WebSocketResponse:
    d = _get_daemon(request)
    password = _get_api_password(d)
    if password:
        token = request.query.get("token", "")
        if not hmac.compare_digest(token, password):
            return web.Response(status=401, text="Unauthorized")
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    _log.info("WebSocket client connected")

    d.ws_clients.add(ws)
    try:
        # Send initial state
        pending_proposals = d.proposal_store.pending
        await ws.send_json(
            {
                "type": "init",
                "workers": [{"name": w.name, "state": w.state.value} for w in d.workers],
                "drones_enabled": d.pilot.enabled if d.pilot else False,
                "proposals": [d._proposal_dict(p) for p in pending_proposals],
                "proposal_count": len(pending_proposals),
            }
        )

        async for msg in ws:
            if msg.type == web.WSMsgType.TEXT:
                try:
                    data = json.loads(msg.data)
                    await _handle_ws_command(d, ws, data)
                except json.JSONDecodeError:
                    await ws.send_json({"type": "error", "message": "invalid JSON"})
            elif msg.type == web.WSMsgType.ERROR:
                _log.warning("WebSocket error: %s", ws.exception())
    finally:
        d.ws_clients.discard(ws)
        _log.info("WebSocket client disconnected")

    return ws


async def _handle_ws_command(d: SwarmDaemon, ws: web.WebSocketResponse, data: dict) -> None:
    """Handle a command received over WebSocket."""
    cmd = data.get("command", "")

    if cmd == "refresh":
        await ws.send_json(
            {
                "type": "state",
                "workers": [
                    {
                        "name": w.name,
                        "state": w.state.value,
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
    else:
        await ws.send_json({"type": "error", "message": f"unknown command: {cmd}"})
