"""REST + WebSocket API server for the swarm daemon."""

from __future__ import annotations

import asyncio
import json
import re
import time
from collections import defaultdict
from typing import TYPE_CHECKING

from aiohttp import web

from swarm.logging import get_logger

if TYPE_CHECKING:
    from swarm.server.daemon import SwarmDaemon

_log = get_logger("server.api")

_WORKER_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]+$")
_MAX_QUERY_LIMIT = 1000
_RATE_LIMIT_REQUESTS = 60  # per minute
_RATE_LIMIT_WINDOW = 60  # seconds


def create_app(daemon: SwarmDaemon, enable_web: bool = True) -> web.Application:
    """Create the aiohttp application with all routes."""
    app = web.Application(middlewares=[_rate_limit_middleware])
    app["daemon"] = daemon
    app["rate_limits"] = defaultdict(list)  # ip -> [timestamps]

    # Web dashboard routes (before API to allow / to serve dashboard)
    if enable_web:
        from swarm.web.app import setup_web_routes
        setup_web_routes(app)

    app.router.add_get("/api/health", handle_health)
    app.router.add_get("/api/workers", handle_workers)
    app.router.add_get("/api/workers/{name}", handle_worker_detail)
    app.router.add_post("/api/workers/{name}/send", handle_worker_send)
    app.router.add_post("/api/workers/{name}/continue", handle_worker_continue)
    app.router.add_post("/api/workers/{name}/kill", handle_worker_kill)
    app.router.add_get("/api/buzz/log", handle_buzz_log)
    app.router.add_get("/api/buzz/status", handle_buzz_status)
    app.router.add_post("/api/buzz/toggle", handle_buzz_toggle)
    app.router.add_get("/api/tasks", handle_tasks)
    app.router.add_post("/api/tasks", handle_create_task)
    app.router.add_post("/api/tasks/{task_id}/assign", handle_assign_task)
    app.router.add_post("/api/tasks/{task_id}/complete", handle_complete_task)
    app.router.add_get("/ws", handle_websocket)

    return app


@web.middleware
async def _rate_limit_middleware(request: web.Request, handler):
    """Simple in-memory rate limiter: N requests/minute per client IP."""
    # Exempt WebSocket upgrades and health checks
    if request.path in ("/ws", "/api/health"):
        return await handler(request)

    ip = request.remote or "unknown"
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
    return web.json_response({
        "status": "ok",
        "workers": len(d.workers),
        "buzz_enabled": d.pilot.enabled if d.pilot else False,
        "uptime": time.time() - d.start_time,
    })


# --- Workers ---

async def handle_workers(request: web.Request) -> web.Response:
    d = _get_daemon(request)
    workers = []
    for w in list(d.workers):
        workers.append({
            "name": w.name,
            "path": w.path,
            "pane_id": w.pane_id,
            "state": w.state.value,
            "state_duration": round(w.state_duration, 1),
            "revive_count": w.revive_count,
        })
    return web.json_response({"workers": workers})


async def handle_worker_detail(request: web.Request) -> web.Response:
    d = _get_daemon(request)
    name = request.match_info["name"]
    worker = next((w for w in d.workers if w.name == name), None)
    if not worker:
        return web.json_response({"error": f"Worker '{name}' not found"}, status=404)

    from swarm.tmux.cell import capture_pane
    try:
        content = await capture_pane(worker.pane_id)
    except Exception:
        content = "(pane unavailable)"

    return web.json_response({
        "name": worker.name,
        "path": worker.path,
        "pane_id": worker.pane_id,
        "state": worker.state.value,
        "state_duration": round(worker.state_duration, 1),
        "revive_count": worker.revive_count,
        "pane_content": content,
    })


async def handle_worker_send(request: web.Request) -> web.Response:
    d = _get_daemon(request)
    name = request.match_info["name"]
    worker = next((w for w in d.workers if w.name == name), None)
    if not worker:
        return web.json_response({"error": f"Worker '{name}' not found"}, status=404)

    body = await request.json()
    message = body.get("message", "")
    if not isinstance(message, str) or not message.strip():
        return web.json_response({"error": "message must be a non-empty string"}, status=400)

    from swarm.tmux.cell import send_keys
    await send_keys(worker.pane_id, message)
    return web.json_response({"status": "sent", "worker": name})


async def handle_worker_continue(request: web.Request) -> web.Response:
    d = _get_daemon(request)
    name = request.match_info["name"]
    worker = next((w for w in d.workers if w.name == name), None)
    if not worker:
        return web.json_response({"error": f"Worker '{name}' not found"}, status=404)

    from swarm.tmux.cell import send_enter
    await send_enter(worker.pane_id)
    return web.json_response({"status": "continued", "worker": name})


async def handle_worker_kill(request: web.Request) -> web.Response:
    d = _get_daemon(request)
    name = request.match_info["name"]
    worker = next((w for w in d.workers if w.name == name), None)
    if not worker:
        return web.json_response({"error": f"Worker '{name}' not found"}, status=404)

    from swarm.worker.manager import kill_worker
    await kill_worker(worker)
    async with d._worker_lock:
        if worker in d.workers:
            d.workers.remove(worker)
    return web.json_response({"status": "killed", "worker": name})


# --- Buzz ---

async def handle_buzz_log(request: web.Request) -> web.Response:
    d = _get_daemon(request)
    limit = min(int(request.query.get("limit", "50")), _MAX_QUERY_LIMIT)
    entries = d.buzz_log.entries[-limit:]
    return web.json_response({
        "entries": [
            {
                "time": e.formatted_time,
                "action": e.action.value,
                "worker": e.worker_name,
                "detail": e.detail,
            }
            for e in entries
        ]
    })


async def handle_buzz_status(request: web.Request) -> web.Response:
    d = _get_daemon(request)
    return web.json_response({
        "enabled": d.pilot.enabled if d.pilot else False,
    })


async def handle_buzz_toggle(request: web.Request) -> web.Response:
    d = _get_daemon(request)
    if d.pilot:
        new_state = d.pilot.toggle()
        return web.json_response({"enabled": new_state})
    return web.json_response({"error": "pilot not running"}, status=400)


# --- Tasks ---

async def handle_tasks(request: web.Request) -> web.Response:
    d = _get_daemon(request)
    tasks = d.task_board.all_tasks
    return web.json_response({
        "tasks": [
            {
                "id": t.id,
                "title": t.title,
                "description": t.description,
                "status": t.status.value,
                "priority": t.priority.value,
                "assigned_worker": t.assigned_worker,
            }
            for t in tasks
        ],
        "summary": d.task_board.summary(),
    })


async def handle_create_task(request: web.Request) -> web.Response:
    d = _get_daemon(request)
    body = await request.json()
    title = body.get("title", "")
    if not isinstance(title, str) or not title.strip():
        return web.json_response({"error": "title must be a non-empty string"}, status=400)

    from swarm.tasks.task import TaskPriority
    valid_priorities = {"low", "normal", "high", "urgent"}
    pri_str = body.get("priority", "normal")
    if pri_str not in valid_priorities:
        return web.json_response({"error": f"priority must be one of: {', '.join(sorted(valid_priorities))}"}, status=400)

    pri_map = {"low": TaskPriority.LOW, "normal": TaskPriority.NORMAL,
               "high": TaskPriority.HIGH, "urgent": TaskPriority.URGENT}
    priority = pri_map[pri_str]

    task = d.task_board.create(
        title=title.strip(),
        description=body.get("description", ""),
        priority=priority,
    )
    return web.json_response({"id": task.id, "title": task.title}, status=201)


async def handle_assign_task(request: web.Request) -> web.Response:
    d = _get_daemon(request)
    task_id = request.match_info["task_id"]
    body = await request.json()
    worker_name = body.get("worker", "")
    if not worker_name:
        return web.json_response({"error": "worker required"}, status=400)

    # Validate worker exists
    worker = next((w for w in d.workers if w.name == worker_name), None)
    if not worker:
        return web.json_response({"error": f"Worker '{worker_name}' not found"}, status=404)

    if d.task_board.assign(task_id, worker_name):
        return web.json_response({"status": "assigned", "task_id": task_id, "worker": worker_name})
    return web.json_response({"error": f"Task '{task_id}' not found"}, status=404)


async def handle_complete_task(request: web.Request) -> web.Response:
    d = _get_daemon(request)
    task_id = request.match_info["task_id"]
    if d.task_board.complete(task_id):
        return web.json_response({"status": "completed", "task_id": task_id})
    return web.json_response({"error": f"Task '{task_id}' not found or cannot be completed"}, status=404)


# --- WebSocket ---

async def handle_websocket(request: web.Request) -> web.WebSocketResponse:
    d = _get_daemon(request)
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    _log.info("WebSocket client connected")

    d.ws_clients.add(ws)
    try:
        # Send initial state
        await ws.send_json({
            "type": "init",
            "workers": [
                {"name": w.name, "state": w.state.value}
                for w in d.workers
            ],
            "buzz_enabled": d.pilot.enabled if d.pilot else False,
        })

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
        await ws.send_json({
            "type": "state",
            "workers": [
                {"name": w.name, "state": w.state.value, "state_duration": round(w.state_duration, 1)}
                for w in d.workers
            ],
        })
    elif cmd == "toggle_buzz":
        if d.pilot:
            new_state = d.pilot.toggle()
            await ws.send_json({"type": "buzz_toggled", "enabled": new_state})
    else:
        await ws.send_json({"type": "error", "message": f"unknown command: {cmd}"})
