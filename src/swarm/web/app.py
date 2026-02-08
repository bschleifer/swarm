"""Web dashboard — Jinja2 + HTMX frontend served by the daemon."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import aiohttp_jinja2
import jinja2
from aiohttp import web

from swarm.logging import get_logger
from swarm.tasks.task import PRIORITY_LABEL, STATUS_ICON, TaskPriority

if TYPE_CHECKING:
    from swarm.server.daemon import SwarmDaemon

_log = get_logger("web.app")

TEMPLATES_DIR = Path(__file__).parent / "templates"


def _get_daemon(request: web.Request) -> SwarmDaemon:
    return request.app["daemon"]


def _worker_dicts(daemon: SwarmDaemon) -> list[dict]:
    return [
        {
            "name": w.name,
            "path": w.path,
            "state": w.state.value,
            "state_duration": f"{w.state_duration:.0f}",
            "revive_count": w.revive_count,
        }
        for w in daemon.workers
    ]


def _task_dicts(daemon: SwarmDaemon) -> list[dict]:
    return [
        {
            "id": t.id,
            "title": t.title,
            "status": t.status.value,
            "status_icon": STATUS_ICON.get(t.status, "?"),
            "priority": t.priority.value,
            "priority_label": PRIORITY_LABEL.get(t.priority, ""),
            "assigned_worker": t.assigned_worker,
        }
        for t in daemon.task_board.all_tasks
    ]


def _drone_dicts(daemon: SwarmDaemon, limit: int = 30) -> list[dict]:
    entries = daemon.drone_log.entries[-limit:]
    return [
        {
            "time": e.formatted_time,
            "action": e.action.value.lower(),
            "worker": e.worker_name,
            "detail": e.detail,
        }
        for e in entries
    ]


# --- Routes ---

@aiohttp_jinja2.template("config.html")
async def handle_config_page(request: web.Request) -> dict:
    d = _get_daemon(request)
    from swarm.config import serialize_config
    return {"config": serialize_config(d.config)}


@aiohttp_jinja2.template("dashboard.html")
async def handle_dashboard(request: web.Request) -> dict:
    d = _get_daemon(request)
    selected = request.query.get("worker")

    pane_content = ""
    if selected:
        worker = next((w for w in d.workers if w.name == selected), None)
        if worker:
            from swarm.tmux.cell import capture_pane
            try:
                pane_content = await capture_pane(worker.pane_id, lines=80)
            except Exception:
                pane_content = "(pane unavailable)"

    groups = [{"name": g.name, "workers": g.workers} for g in d.config.groups]

    return {
        "workers": _worker_dicts(d),
        "selected_worker": selected,
        "pane_content": pane_content,
        "tasks": _task_dicts(d),
        "task_summary": d.task_board.summary(),
        "entries": _drone_dicts(d),
        "worker_count": len(d.workers),
        "drones_enabled": d.pilot.enabled if d.pilot else False,
        "groups": groups,
    }


# --- Partials (HTMX) ---

@aiohttp_jinja2.template("partials/worker_list.html")
async def handle_partial_workers(request: web.Request) -> dict:
    d = _get_daemon(request)
    return {
        "workers": _worker_dicts(d),
        "selected_worker": request.query.get("worker"),
    }


async def handle_partial_status(request: web.Request) -> web.Response:
    d = _get_daemon(request)
    drones_state = "ON" if (d.pilot and d.pilot.enabled) else "OFF"
    return web.Response(text=f"{len(d.workers)} workers | Drones: {drones_state}")


@aiohttp_jinja2.template("partials/task_list.html")
async def handle_partial_tasks(request: web.Request) -> dict:
    d = _get_daemon(request)
    return {
        "tasks": _task_dicts(d),
        "task_summary": d.task_board.summary(),
    }


@aiohttp_jinja2.template("partials/drone_log.html")
async def handle_partial_drones(request: web.Request) -> dict:
    d = _get_daemon(request)
    return {"entries": _drone_dicts(d)}


async def handle_partial_detail(request: web.Request) -> web.Response:
    d = _get_daemon(request)
    name = request.match_info["name"]
    worker = next((w for w in d.workers if w.name == name), None)
    if not worker:
        return web.Response(text="Worker not found", status=404)

    from markupsafe import escape
    from swarm.tmux.cell import capture_pane
    try:
        content = await capture_pane(worker.pane_id, lines=80)
    except Exception:
        content = "(pane unavailable)"

    escaped = escape(content)
    state_dur = f"{worker.state_duration:.0f}"
    header = (
        f'<div style="color: var(--muted); font-size: 0.8rem; padding: 0.25rem 0.5rem; '
        f'border-bottom: 1px solid var(--panel); margin-bottom: 0.5rem;">'
        f'{escape(worker.name)} &mdash; {escape(worker.state.value)} for {state_dur}s'
        f' &mdash; {escape(worker.path)}'
        f'</div>'
    )
    return web.Response(
        text=f'{header}<div class="pane-content">{escaped}</div>',
        content_type="text/html",
    )


# --- Actions ---

async def handle_action_send(request: web.Request) -> web.Response:
    d = _get_daemon(request)
    name = request.match_info["name"]
    worker = next((w for w in d.workers if w.name == name), None)
    if not worker:
        return web.Response(status=404)

    data = await request.post()
    message = data.get("message", "")
    if message:
        from swarm.tmux.cell import send_keys
        await send_keys(worker.pane_id, message)

    return web.Response(status=204)


async def handle_action_continue(request: web.Request) -> web.Response:
    d = _get_daemon(request)
    name = request.match_info["name"]
    worker = next((w for w in d.workers if w.name == name), None)
    if not worker:
        return web.Response(status=404)

    from swarm.tmux.cell import send_enter
    await send_enter(worker.pane_id)
    return web.Response(status=204)


async def handle_action_toggle_drones(request: web.Request) -> web.Response:
    d = _get_daemon(request)
    if d.pilot:
        new_state = d.pilot.toggle()
        # Broadcast to all WS clients
        d._broadcast_ws({"type": "drones_toggled", "enabled": new_state})
        return web.json_response({"enabled": new_state})
    return web.json_response({"error": "pilot not running", "enabled": False})


async def handle_action_continue_all(request: web.Request) -> web.Response:
    d = _get_daemon(request)
    from swarm.tmux.cell import send_enter
    from swarm.worker.worker import WorkerState

    count = 0
    for w in list(d.workers):
        if w.state == WorkerState.RESTING:
            try:
                await send_enter(w.pane_id)
                count += 1
            except Exception:
                _log.warning("failed to send enter to %s", w.name, exc_info=True)
    return web.json_response({"count": count})


async def handle_action_kill(request: web.Request) -> web.Response:
    d = _get_daemon(request)
    name = request.match_info["name"]

    from swarm.worker.manager import kill_worker
    from swarm.worker.worker import WorkerState

    async with d._worker_lock:
        worker = next((w for w in d.workers if w.name == name), None)
        if not worker:
            return web.Response(status=404)
        await kill_worker(worker)
        worker.state = WorkerState.STUNG
    return web.json_response({"status": "killed", "worker": name})


async def handle_action_revive(request: web.Request) -> web.Response:
    d = _get_daemon(request)
    name = request.match_info["name"]
    worker = next((w for w in d.workers if w.name == name), None)
    if not worker:
        return web.Response(status=404)

    from swarm.worker.worker import WorkerState
    if worker.state != WorkerState.STUNG:
        return web.json_response(
            {"error": f"Worker '{name}' is {worker.state.value}, not STUNG — cannot revive"},
            status=409,
        )

    from swarm.worker.manager import revive_worker
    await revive_worker(worker)
    worker.record_revive()
    return web.json_response({"status": "revived", "worker": name})


async def handle_action_escape(request: web.Request) -> web.Response:
    d = _get_daemon(request)
    name = request.match_info["name"]
    worker = next((w for w in d.workers if w.name == name), None)
    if not worker:
        return web.Response(status=404)

    from swarm.tmux.cell import send_escape
    await send_escape(worker.pane_id)
    return web.json_response({"status": "escape_sent", "worker": name})


async def handle_action_send_all(request: web.Request) -> web.Response:
    d = _get_daemon(request)
    data = await request.post()
    message = data.get("message", "")
    if not message:
        return web.json_response({"error": "message required"}, status=400)

    from swarm.tmux.cell import send_keys

    count = 0
    for w in list(d.workers):
        try:
            await send_keys(w.pane_id, message)
            count += 1
        except Exception:
            pass
    return web.json_response({"count": count})


async def handle_action_create_task(request: web.Request) -> web.Response:
    d = _get_daemon(request)
    data = await request.post()
    title = data.get("title", "").strip()
    if not title:
        return web.json_response({"error": "title required"}, status=400)

    pri_map = {"low": TaskPriority.LOW, "normal": TaskPriority.NORMAL,
               "high": TaskPriority.HIGH, "urgent": TaskPriority.URGENT}
    priority = pri_map.get(data.get("priority", "normal"), TaskPriority.NORMAL)

    task = d.task_board.create(
        title=title,
        description=data.get("description", ""),
        priority=priority,
    )
    d._broadcast_ws({"type": "task_created", "task": {"id": task.id, "title": task.title}})
    return web.json_response({"id": task.id, "title": task.title}, status=201)


async def handle_action_assign_task(request: web.Request) -> web.Response:
    d = _get_daemon(request)
    data = await request.post()
    task_id = data.get("task_id", "")
    worker_name = data.get("worker", "")
    if not task_id or not worker_name:
        return web.json_response({"error": "task_id and worker required"}, status=400)

    if d.task_board.assign(task_id, worker_name):
        d._broadcast_ws({
            "type": "task_assigned",
            "worker": worker_name,
            "task": {"id": task_id, "title": d.task_board.get(task_id).title},
        })
        return web.json_response({"status": "assigned", "task_id": task_id, "worker": worker_name})
    return web.json_response({"error": "task not found or not assignable"}, status=404)


async def handle_action_complete_task(request: web.Request) -> web.Response:
    d = _get_daemon(request)
    data = await request.post()
    task_id = data.get("task_id", "")
    if not task_id:
        return web.json_response({"error": "task_id required"}, status=400)

    if d.task_board.complete(task_id):
        d._broadcast_ws({"type": "task_completed", "task_id": task_id})
        return web.json_response({"status": "completed", "task_id": task_id})
    return web.json_response({"error": "task not found"}, status=404)


async def handle_action_ask_queen(request: web.Request) -> web.Response:
    d = _get_daemon(request)
    if not d.queen or not d.queen.can_call:
        return web.json_response({"error": "Queen not configured"}, status=400)

    from swarm.queen.context import build_hive_context
    from swarm.tmux.cell import capture_pane

    worker_outputs: dict[str, str] = {}
    for w in list(d.workers):
        try:
            worker_outputs[w.name] = await capture_pane(w.pane_id, lines=20)
        except Exception:
            _log.debug("failed to capture pane for %s in Queen request", w.name)

    hive_ctx = build_hive_context(
        list(d.workers),
        worker_outputs=worker_outputs,
        drone_log=d.drone_log,
        task_board=d.task_board,
    )

    try:
        result = await d.queen.coordinate_hive(hive_ctx)
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)

    return web.json_response(result)


# --- A1: Send Interrupt (Ctrl-C) ---

async def handle_action_interrupt(request: web.Request) -> web.Response:
    d = _get_daemon(request)
    name = request.match_info["name"]
    worker = next((w for w in d.workers if w.name == name), None)
    if not worker:
        return web.Response(status=404)

    from swarm.tmux.cell import send_interrupt
    await send_interrupt(worker.pane_id)
    return web.json_response({"status": "interrupt_sent", "worker": name})


# --- A2: Task Removal ---

async def handle_action_remove_task(request: web.Request) -> web.Response:
    d = _get_daemon(request)
    data = await request.post()
    task_id = data.get("task_id", "")
    if not task_id:
        return web.json_response({"error": "task_id required"}, status=400)

    if d.task_board.remove(task_id):
        d._broadcast_ws({"type": "task_removed", "task_id": task_id})
        return web.json_response({"status": "removed", "task_id": task_id})
    return web.json_response({"error": "task not found"}, status=404)


# --- A3: Task Failure ---

async def handle_action_fail_task(request: web.Request) -> web.Response:
    d = _get_daemon(request)
    data = await request.post()
    task_id = data.get("task_id", "")
    if not task_id:
        return web.json_response({"error": "task_id required"}, status=400)

    if d.task_board.fail(task_id):
        d._broadcast_ws({"type": "task_failed", "task_id": task_id})
        return web.json_response({"status": "failed", "task_id": task_id})
    return web.json_response({"error": "task not found"}, status=404)


# --- A4: Per-worker Queen Analysis ---

async def handle_action_ask_queen_worker(request: web.Request) -> web.Response:
    d = _get_daemon(request)
    name = request.match_info["name"]
    worker = next((w for w in d.workers if w.name == name), None)
    if not worker:
        return web.Response(status=404)

    if not d.queen or not d.queen.can_call:
        return web.json_response({"error": "Queen not configured or on cooldown"}, status=400)

    from swarm.queen.context import build_hive_context
    from swarm.tmux.cell import capture_pane

    content = ""
    try:
        content = await capture_pane(worker.pane_id)
    except Exception:
        content = "(pane unavailable)"

    worker_outputs: dict[str, str] = {}
    for w in list(d.workers):
        try:
            worker_outputs[w.name] = await capture_pane(w.pane_id, lines=20)
        except Exception:
            _log.debug("failed to capture pane for %s in Queen worker request", w.name)

    hive_ctx = build_hive_context(
        list(d.workers),
        worker_outputs=worker_outputs,
        drone_log=d.drone_log,
        task_board=d.task_board,
    )

    try:
        result = await d.queen.analyze_worker(worker.name, content, hive_context=hive_ctx)
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)

    return web.json_response(result)


# --- A5: Group-targeted Send ---

async def handle_action_send_group(request: web.Request) -> web.Response:
    d = _get_daemon(request)
    data = await request.post()
    message = data.get("message", "")
    group_name = data.get("group", "")
    if not message:
        return web.json_response({"error": "message required"}, status=400)
    if not group_name:
        return web.json_response({"error": "group required"}, status=400)

    from swarm.tmux.cell import send_keys

    try:
        group_workers = d.config.get_group(group_name)
    except ValueError:
        return web.json_response({"error": f"unknown group: {group_name}"}, status=404)

    group_names = {w.name.lower() for w in group_workers}
    count = 0
    for w in list(d.workers):
        if w.name.lower() in group_names:
            try:
                await send_keys(w.pane_id, message)
                count += 1
            except Exception:
                pass
    return web.json_response({"count": count})


# --- A6: Launch Brood ---

async def handle_action_launch(request: web.Request) -> web.Response:
    d = _get_daemon(request)
    if d.workers:
        return web.json_response({"error": "workers already running"}, status=409)

    data = await request.post()
    names_raw = data.get("workers", "")  # comma-separated worker names

    from swarm.worker.manager import launch_hive

    if names_raw:
        names = {n.strip().lower() for n in names_raw.split(",")}
        to_launch = [w for w in d.config.workers if w.name.lower() in names]
    else:
        to_launch = list(d.config.workers)

    if not to_launch:
        return web.json_response({"error": "no workers to launch"}, status=400)

    try:
        launched = await launch_hive(
            d.config.session_name,
            to_launch,
            d.config.panes_per_window,
        )
        async with d._worker_lock:
            d.workers.extend(launched)
        if d.pilot:
            d.pilot.workers = d.workers
            d.pilot.start()

        d._broadcast_ws({"type": "workers_changed"})
        return web.json_response({
            "status": "launched",
            "count": len(launched),
            "workers": [w.name for w in launched],
        })
    except Exception as e:
        _log.error("launch failed", exc_info=True)
        return web.json_response({"error": str(e)}, status=500)


async def handle_partial_launch_config(request: web.Request) -> web.Response:
    d = _get_daemon(request)
    workers = [{"name": w.name, "path": w.path} for w in d.config.workers]
    groups = [{"name": g.name, "workers": g.workers} for g in d.config.groups]
    return web.json_response({"workers": workers, "groups": groups})


# --- A8: Kill Session (Shutdown) ---

async def handle_action_kill_session(request: web.Request) -> web.Response:
    d = _get_daemon(request)

    # Unassign all tasks
    for w in list(d.workers):
        d.task_board.unassign_worker(w.name)

    from swarm.tmux.hive import kill_session
    try:
        await kill_session(d.config.session_name)
    except Exception:
        _log.warning("kill_session failed (session may already be gone)", exc_info=True)

    async with d._worker_lock:
        d.workers.clear()
    # Pilot self-stops when workers list is empty (see pilot._loop)
    d._broadcast_ws({"type": "workers_changed"})
    return web.json_response({"status": "killed"})


def setup_web_routes(app: web.Application) -> None:
    """Add web dashboard routes to an aiohttp app."""
    aiohttp_jinja2.setup(
        app,
        loader=jinja2.FileSystemLoader(str(TEMPLATES_DIR)),
        autoescape=jinja2.select_autoescape(["html"]),
    )

    app.router.add_get("/", handle_dashboard)
    app.router.add_get("/dashboard", handle_dashboard)
    app.router.add_get("/config", handle_config_page)
    app.router.add_get("/partials/workers", handle_partial_workers)
    app.router.add_get("/partials/status", handle_partial_status)
    app.router.add_get("/partials/tasks", handle_partial_tasks)
    app.router.add_get("/partials/drones", handle_partial_drones)
    app.router.add_get("/partials/detail/{name}", handle_partial_detail)
    app.router.add_post("/action/send/{name}", handle_action_send)
    app.router.add_post("/action/continue/{name}", handle_action_continue)
    app.router.add_post("/action/kill/{name}", handle_action_kill)
    app.router.add_post("/action/revive/{name}", handle_action_revive)
    app.router.add_post("/action/escape/{name}", handle_action_escape)
    app.router.add_post("/action/toggle-drones", handle_action_toggle_drones)
    app.router.add_post("/action/continue-all", handle_action_continue_all)
    app.router.add_post("/action/send-all", handle_action_send_all)
    app.router.add_post("/action/task/create", handle_action_create_task)
    app.router.add_post("/action/task/assign", handle_action_assign_task)
    app.router.add_post("/action/task/complete", handle_action_complete_task)
    app.router.add_post("/action/task/remove", handle_action_remove_task)
    app.router.add_post("/action/task/fail", handle_action_fail_task)
    app.router.add_post("/action/ask-queen", handle_action_ask_queen)
    app.router.add_post("/action/ask-queen/{name}", handle_action_ask_queen_worker)
    app.router.add_post("/action/interrupt/{name}", handle_action_interrupt)
    app.router.add_post("/action/send-group", handle_action_send_group)
    app.router.add_post("/action/launch", handle_action_launch)
    app.router.add_get("/partials/launch-config", handle_partial_launch_config)
    app.router.add_post("/action/kill-session", handle_action_kill_session)
