"""Web dashboard — Jinja2 + HTMX frontend served by the daemon."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import aiohttp_jinja2
import jinja2
from aiohttp import web

from swarm.logging import get_logger
from swarm.server.daemon import SwarmOperationError, WorkerNotFoundError, console_log
from swarm.tasks.task import PRIORITY_LABEL, PRIORITY_MAP, STATUS_ICON, TaskPriority

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
            "pane_id": w.pane_id,
            "state": w.state.value,
            "state_duration": f"{w.state_duration:.0f}",
            "revive_count": w.revive_count,
        }
        for w in daemon.workers
    ]


def _format_age(ts: float) -> str:
    """Format a timestamp as a human-readable age string."""
    import time

    delta = time.time() - ts
    if delta < 60:
        return "just now"
    if delta < 3600:
        return f"{int(delta // 60)}m ago"
    if delta < 86400:
        return f"{int(delta // 3600)}h ago"
    return f"{int(delta // 86400)}d ago"


def _task_dicts(daemon: SwarmDaemon) -> list[dict]:
    return [
        {
            "id": t.id,
            "title": t.title,
            "description": t.description,
            "status": t.status.value,
            "status_icon": STATUS_ICON.get(t.status, "?"),
            "priority": t.priority.value,
            "priority_label": PRIORITY_LABEL.get(t.priority, ""),
            "assigned_worker": t.assigned_worker,
            "created_age": _format_age(t.created_at),
            "updated_age": _format_age(t.updated_at),
            "tags": t.tags,
            "attachments": t.attachments,
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
        worker = d.get_worker(selected)
        if worker:
            try:
                pane_content = await d.capture_worker_output(selected, lines=80)
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
        "ws_auth_required": bool(d.config.api_password),
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
    return web.Response(text=f"{len(d.workers)} workers")


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
    worker = d.get_worker(name)
    if not worker:
        return web.Response(text="Worker not found", status=404)

    from markupsafe import escape

    try:
        content = await d.capture_worker_output(name, lines=80)
    except Exception:
        content = "(pane unavailable)"

    escaped = escape(content)
    state_dur = f"{worker.state_duration:.0f}"
    header = (
        f'<div style="color: var(--muted); font-size: 0.8rem; padding: 0.25rem 0.5rem; '
        f'border-bottom: 1px solid var(--panel); margin-bottom: 0.5rem;">'
        f"{escape(worker.name)} &mdash; {escape(worker.state.value)} for {state_dur}s"
        f" &mdash; {escape(worker.path)}"
        f"</div>"
    )
    return web.Response(
        text=f'{header}<div class="pane-content">{escaped}</div>',
        content_type="text/html",
    )


# --- Actions ---


async def handle_action_send(request: web.Request) -> web.Response:
    d = _get_daemon(request)
    name = request.match_info["name"]
    data = await request.post()
    message = data.get("message", "")
    if message:
        try:
            await d.send_to_worker(name, message)
            console_log(f'Message sent to "{name}"')
        except WorkerNotFoundError:
            return web.Response(status=404)
    return web.Response(status=204)


async def handle_action_continue(request: web.Request) -> web.Response:
    d = _get_daemon(request)
    name = request.match_info["name"]
    try:
        await d.continue_worker(name)
        console_log(f'Continued "{name}"')
    except WorkerNotFoundError:
        return web.Response(status=404)
    return web.Response(status=204)


async def handle_action_toggle_drones(request: web.Request) -> web.Response:
    d = _get_daemon(request)
    if d.pilot:
        new_state = d.toggle_drones()
        console_log(f"Drones toggled {'ON' if new_state else 'OFF'}")
        return web.json_response({"enabled": new_state})
    return web.json_response({"error": "pilot not running", "enabled": False})


async def handle_action_continue_all(request: web.Request) -> web.Response:
    d = _get_daemon(request)
    count = await d.continue_all()
    console_log(f"Continue all — {count} worker(s)")
    return web.json_response({"count": count})


async def handle_action_kill(request: web.Request) -> web.Response:
    d = _get_daemon(request)
    name = request.match_info["name"]
    try:
        await d.kill_worker(name)
        console_log(f'Killed worker "{name}"', level="warn")
    except WorkerNotFoundError:
        return web.Response(status=404)
    return web.json_response({"status": "killed", "worker": name})


async def handle_action_revive(request: web.Request) -> web.Response:
    d = _get_daemon(request)
    name = request.match_info["name"]
    try:
        await d.revive_worker(name)
        console_log(f'Revived worker "{name}"')
    except WorkerNotFoundError:
        return web.Response(status=404)
    except SwarmOperationError as e:
        return web.json_response({"error": str(e)}, status=409)
    return web.json_response({"status": "revived", "worker": name})


async def handle_action_escape(request: web.Request) -> web.Response:
    d = _get_daemon(request)
    name = request.match_info["name"]
    try:
        await d.escape_worker(name)
        console_log(f'Escape sent to "{name}"')
    except WorkerNotFoundError:
        return web.Response(status=404)
    return web.json_response({"status": "escape_sent", "worker": name})


async def handle_action_send_all(request: web.Request) -> web.Response:
    d = _get_daemon(request)
    data = await request.post()
    message = data.get("message", "")
    if not message:
        return web.json_response({"error": "message required"}, status=400)

    count = await d.send_all(message)
    console_log(f"Broadcast sent to {count} worker(s)")
    return web.json_response({"count": count})


async def handle_action_create_task(request: web.Request) -> web.Response:
    d = _get_daemon(request)
    data = await request.post()
    title = data.get("title", "").strip()
    if not title:
        return web.json_response({"error": "title required"}, status=400)

    priority = PRIORITY_MAP.get(data.get("priority", "normal"), TaskPriority.NORMAL)

    task = d.create_task(
        title=title,
        description=data.get("description", ""),
        priority=priority,
    )
    console_log(f'Task created: "{title}" ({priority.value})')
    return web.json_response({"id": task.id, "title": task.title}, status=201)


async def handle_action_assign_task(request: web.Request) -> web.Response:
    d = _get_daemon(request)
    data = await request.post()
    task_id = data.get("task_id", "")
    worker_name = data.get("worker", "")
    if not task_id or not worker_name:
        return web.json_response({"error": "task_id and worker required"}, status=400)

    try:
        d.assign_task(task_id, worker_name)
        console_log(f'Task assigned to "{worker_name}"')
    except SwarmOperationError as e:
        return web.json_response({"error": str(e)}, status=404)
    return web.json_response({"status": "assigned", "task_id": task_id, "worker": worker_name})


async def handle_action_complete_task(request: web.Request) -> web.Response:
    d = _get_daemon(request)
    data = await request.post()
    task_id = data.get("task_id", "")
    if not task_id:
        return web.json_response({"error": "task_id required"}, status=400)

    try:
        d.complete_task(task_id)
        console_log(f"Task completed: {task_id[:8]}")
    except SwarmOperationError as e:
        return web.json_response({"error": str(e)}, status=404)
    return web.json_response({"status": "completed", "task_id": task_id})


async def handle_action_ask_queen(request: web.Request) -> web.Response:
    d = _get_daemon(request)
    if not d.queen:
        return web.json_response({"error": "Queen not configured"}, status=400)
    if not d.queen.can_call:
        wait = d.queen.cooldown_remaining
        console_log(f"Queen on cooldown ({wait:.0f}s remaining)", level="warn")
        return web.json_response(
            {"error": f"Queen on cooldown — try again in {wait:.0f}s"}, status=429
        )

    console_log("Queen coordinating hive...")
    try:
        result = await d.coordinate_hive()
    except Exception as e:
        console_log(f"Queen error: {e}", level="error")
        return web.json_response({"error": str(e)}, status=500)

    n = len(result.get("directives", []))
    console_log(f"Queen done — {n} directive(s)")
    return web.json_response(result)


# --- A1: Send Interrupt (Ctrl-C) ---


async def handle_action_interrupt(request: web.Request) -> web.Response:
    d = _get_daemon(request)
    name = request.match_info["name"]
    try:
        await d.interrupt_worker(name)
        console_log(f'Ctrl-C sent to "{name}"')
    except WorkerNotFoundError:
        return web.Response(status=404)
    return web.json_response({"status": "interrupt_sent", "worker": name})


# --- A2: Task Removal ---


async def handle_action_remove_task(request: web.Request) -> web.Response:
    d = _get_daemon(request)
    data = await request.post()
    task_id = data.get("task_id", "")
    if not task_id:
        return web.json_response({"error": "task_id required"}, status=400)

    try:
        d.remove_task(task_id)
        console_log(f"Task removed: {task_id[:8]}")
    except SwarmOperationError as e:
        return web.json_response({"error": str(e)}, status=404)
    return web.json_response({"status": "removed", "task_id": task_id})


# --- A3: Task Failure ---


async def handle_action_fail_task(request: web.Request) -> web.Response:
    d = _get_daemon(request)
    data = await request.post()
    task_id = data.get("task_id", "")
    if not task_id:
        return web.json_response({"error": "task_id required"}, status=400)

    try:
        d.fail_task(task_id)
        console_log(f"Task failed: {task_id[:8]}", level="warn")
    except SwarmOperationError as e:
        return web.json_response({"error": str(e)}, status=404)
    return web.json_response({"status": "failed", "task_id": task_id})


# --- A4: Per-worker Queen Analysis ---


async def handle_action_ask_queen_worker(request: web.Request) -> web.Response:
    d = _get_daemon(request)
    name = request.match_info["name"]

    if not d.queen:
        return web.json_response({"error": "Queen not configured"}, status=400)
    if not d.queen.can_call:
        wait = d.queen.cooldown_remaining
        console_log(f"Queen on cooldown ({wait:.0f}s remaining)", level="warn")
        return web.json_response(
            {"error": f"Queen on cooldown — try again in {wait:.0f}s"}, status=429
        )

    console_log(f'Queen analyzing "{name}"...')
    try:
        result = await d.analyze_worker(name)
    except WorkerNotFoundError:
        return web.Response(status=404)
    except Exception as e:
        console_log(f"Queen error: {e}", level="error")
        return web.json_response({"error": str(e)}, status=500)

    console_log(f'Queen analysis of "{name}" complete')
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

    try:
        count = await d.send_group(group_name, message)
        console_log(f'Group "{group_name}" — sent to {count} worker(s)')
    except ValueError:
        return web.json_response({"error": f"unknown group: {group_name}"}, status=404)
    return web.json_response({"count": count})


# --- A6: Launch Brood ---


async def handle_action_launch(request: web.Request) -> web.Response:
    d = _get_daemon(request)

    data = await request.post()
    names_raw = data.get("workers", "")  # comma-separated worker names

    # Skip workers that are already running
    running_names = {w.name.lower() for w in d.workers}

    if names_raw:
        names = {n.strip().lower() for n in names_raw.split(",")}
        to_launch = [
            w
            for w in d.config.workers
            if w.name.lower() in names and w.name.lower() not in running_names
        ]
    else:
        to_launch = [w for w in d.config.workers if w.name.lower() not in running_names]

    if not to_launch:
        return web.json_response({"error": "no workers to launch"}, status=400)

    try:
        console_log(f"Launching {len(to_launch)} worker(s)...")
        launched = await d.launch_workers(to_launch)
        names = ", ".join(w.name for w in launched)
        console_log(f"Launched: {names}")
        return web.json_response(
            {
                "status": "launched",
                "count": len(launched),
                "workers": [w.name for w in launched],
            }
        )
    except Exception as e:
        console_log(f"Launch failed: {e}", level="error")
        _log.error("launch failed", exc_info=True)
        return web.json_response({"error": str(e)}, status=500)


async def handle_partial_launch_config(request: web.Request) -> web.Response:
    d = _get_daemon(request)
    running_names = {w.name.lower() for w in d.workers}
    workers = [
        {"name": w.name, "path": w.path, "running": w.name.lower() in running_names}
        for w in d.config.workers
    ]
    groups = [{"name": g.name, "workers": g.workers} for g in d.config.groups]
    return web.json_response({"workers": workers, "groups": groups})


async def handle_action_spawn(request: web.Request) -> web.Response:
    """Spawn a single worker into the running session."""
    d = _get_daemon(request)
    data = await request.post()
    name = data.get("name", "").strip()
    path = data.get("path", "").strip()

    if not name or not path:
        return web.json_response({"error": "name and path required"}, status=400)

    from swarm.config import WorkerConfig

    wc = WorkerConfig(name=name, path=path)
    try:
        worker = await d.spawn_worker(wc)
        console_log(f'Spawned worker "{worker.name}"')
        return web.json_response({"status": "spawned", "worker": worker.name})
    except SwarmOperationError as e:
        return web.json_response({"error": str(e)}, status=409)
    except Exception as e:
        console_log(f"Spawn failed: {e}", level="error")
        _log.error("spawn failed", exc_info=True)
        return web.json_response({"error": str(e)}, status=500)


# --- A8: Kill Session (Shutdown) ---


async def handle_action_kill_session(request: web.Request) -> web.Response:
    d = _get_daemon(request)
    console_log("Killing session — all workers terminated", level="warn")
    await d.kill_session()
    return web.json_response({"status": "killed"})


async def handle_action_edit_task(request: web.Request) -> web.Response:
    d = _get_daemon(request)
    data = await request.post()
    task_id = data.get("task_id", "")
    if not task_id:
        return web.json_response({"error": "task_id required"}, status=400)

    kwargs: dict = {}
    title = data.get("title", "").strip()
    if title:
        kwargs["title"] = title
    desc = data.get("description")
    if desc is not None:
        kwargs["description"] = desc
    pri = data.get("priority")
    if pri and pri in PRIORITY_MAP:
        kwargs["priority"] = PRIORITY_MAP[pri]
    tags_raw = data.get("tags", "").strip()
    if tags_raw:
        kwargs["tags"] = [t.strip() for t in tags_raw.split(",") if t.strip()]

    try:
        d.edit_task(task_id, **kwargs)
        console_log(f"Task edited: {task_id[:8]}")
    except SwarmOperationError as e:
        return web.json_response({"error": str(e)}, status=404)
    return web.json_response({"status": "updated", "task_id": task_id})


async def handle_action_upload_attachment(request: web.Request) -> web.Response:
    d = _get_daemon(request)
    reader = await request.multipart()

    task_id = None
    file_data = None
    file_name = "upload"

    while True:
        field = await reader.next()
        if field is None:
            break
        if field.name == "task_id":
            task_id = (await field.text()).strip()
        elif field.name == "file":
            file_name = field.filename or "upload"
            file_data = await field.read(decode=False)

    if not task_id or file_data is None:
        return web.json_response({"error": "task_id and file required"}, status=400)

    task = d.task_board.get(task_id)
    if not task:
        return web.json_response({"error": f"Task '{task_id}' not found"}, status=404)

    path = d.save_attachment(file_name, file_data)
    new_attachments = list(task.attachments) + [path]
    d.task_board.update(task_id, attachments=new_attachments)

    console_log(f"Attachment uploaded: {file_name}")
    return web.json_response({"status": "uploaded", "path": path}, status=201)


async def handle_action_stop_server(request: web.Request) -> web.Response:
    """Trigger graceful shutdown of the web server."""
    console_log("Web server stopping...")
    shutdown_event = request.app.get("shutdown_event")
    if shutdown_event:
        shutdown_event.set()
        return web.json_response({"status": "stopping"})
    return web.json_response({"error": "no shutdown event configured"}, status=500)


def setup_web_routes(app: web.Application) -> None:
    """Add web dashboard routes to an aiohttp app."""
    import os

    env = aiohttp_jinja2.setup(
        app,
        loader=jinja2.FileSystemLoader(str(TEMPLATES_DIR)),
        autoescape=jinja2.select_autoescape(["html"]),
    )
    env.filters["basename"] = lambda p: os.path.basename(p)

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
    app.router.add_post("/action/spawn", handle_action_spawn)
    app.router.add_get("/partials/launch-config", handle_partial_launch_config)
    app.router.add_post("/action/kill-session", handle_action_kill_session)
    app.router.add_post("/action/task/edit", handle_action_edit_task)
    app.router.add_post("/action/task/upload", handle_action_upload_attachment)
    app.router.add_post("/action/stop-server", handle_action_stop_server)
