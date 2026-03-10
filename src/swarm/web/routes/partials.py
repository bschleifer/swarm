"""HTMX partial routes."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import aiohttp_jinja2
from aiohttp import web

from swarm.server.helpers import get_daemon
from swarm.web.app import _system_log_dicts, _task_dicts, _worker_dicts
from swarm.worker.worker import WorkerState, format_duration


@aiohttp_jinja2.template("partials/worker_list.html")
async def handle_partial_workers(request: web.Request) -> dict[str, Any]:
    d = get_daemon(request)
    worker_tasks: dict[str, str] = {}
    for t in d.task_board.active_tasks:
        if t.assigned_worker:
            worker_tasks[t.assigned_worker] = t.title
    return {
        "workers": _worker_dicts(d),
        "selected_worker": request.query.get("worker"),
        "worker_tasks": worker_tasks,
    }


async def handle_partial_status(request: web.Request) -> web.Response:
    d = get_daemon(request)
    workers = d.workers
    total = len(workers)
    if total == 0:
        return web.Response(text="0 workers", content_type="text/html")

    from collections import Counter

    counts = Counter(w.display_state.value for w in workers)
    parts = []
    for state in WorkerState:
        c = counts.get(state.value, 0)
        if c > 0:
            parts.append(f'<span class="{state.css_class}">{c} {state.display}</span>')
    breakdown = ", ".join(parts)
    return web.Response(text=f"{total} workers: {breakdown}", content_type="text/html")


@aiohttp_jinja2.template("partials/task_list.html")
async def handle_partial_tasks(request: web.Request) -> dict[str, Any]:
    d = get_daemon(request)
    tasks = _task_dicts(d)

    # Filter by status (supports comma-separated multi-select from JS)
    status_filter = request.query.get("status")
    if status_filter and status_filter != "all":
        match_statuses: set[str] = set()
        for s in status_filter.split(","):
            s = s.strip()
            if s == "assigned":
                match_statuses.update(("assigned", "in_progress"))
            elif s:
                match_statuses.add(s)
        if match_statuses:
            tasks = [t for t in tasks if t["status"] in match_statuses]

    # Filter by priority (supports comma-separated multi-select)
    priority_filter = request.query.get("priority")
    if priority_filter and priority_filter != "all":
        priorities = {p.strip() for p in priority_filter.split(",") if p.strip()}
        if priorities:
            tasks = [t for t in tasks if t["priority"] in priorities]

    # Text search
    q = request.query.get("q", "").strip().lower()
    if q:
        tasks = [
            t for t in tasks if q in t["title"].lower() or q in (t.get("description") or "").lower()
        ]

    return {
        "tasks": tasks,
        "task_summary": d.task_board.summary(),
        "task_buttons": [
            {
                "label": b.label,
                "action": b.action,
                "show_mobile": b.show_mobile,
                "show_desktop": b.show_desktop,
            }
            for b in d.config.task_buttons
        ],
    }


@aiohttp_jinja2.template("partials/system_log.html")
async def handle_partial_system_log(request: web.Request) -> dict[str, Any]:
    d = get_daemon(request)
    category = request.query.get("category")
    notification = request.query.get("notification") == "true"
    query = request.query.get("q", "").strip() or None
    entries = _system_log_dicts(d, category=category, notification_only=notification, query=query)
    return {"entries": entries}


async def handle_partial_detail(request: web.Request) -> web.Response:
    d = get_daemon(request)
    name = request.match_info["name"]
    worker = d.get_worker(name)
    if not worker:
        return web.Response(text="Worker not found", status=404)

    from markupsafe import escape

    content = await d.safe_capture_output(name)

    escaped = escape(content)
    state_dur = format_duration(worker.state_duration)
    header = (
        f'<div class="detail-header">'
        f"{escape(worker.name)} &mdash; {escape(worker.display_state.value)} for {state_dur}"
        f" &mdash; {escape(worker.path)}"
        f"</div>"
    )
    return web.Response(
        text=f'{header}<div class="worker-output">{escaped}</div>',
        content_type="text/html",
    )


async def handle_partial_launch_config(request: web.Request) -> web.Response:
    d = get_daemon(request)
    running_names = {w.name.lower() for w in d.workers}
    workers = [
        {"name": w.name, "path": w.path, "running": w.name.lower() in running_names}
        for w in d.config.workers
    ]
    groups = [{"name": g.name, "workers": g.workers} for g in d.config.groups]
    return web.json_response({"workers": workers, "groups": groups})


async def handle_partial_task_history(request: web.Request) -> web.Response:
    """Return task history events as HTML for inline display."""
    d = get_daemon(request)
    task_id = request.match_info["task_id"]
    events = d.task_history.get_events(task_id, limit=50)
    if not events:
        return web.Response(
            text='<div class="history-empty">No history</div>',
            content_type="text/html",
        )

    from markupsafe import escape

    action_class = {
        "CREATED": "text-leaf",
        "ASSIGNED": "text-lavender",
        "COMPLETED": "text-leaf",
        "FAILED": "text-poppy",
        "REMOVED": "text-poppy",
        "EDITED": "text-honey",
    }
    parts = ['<div class="history-container">']
    for ev in events:
        cls = action_class.get(ev.action.value, "text-muted")
        parts.append(
            f'<div class="history-entry">'
            f'<span class="history-time">{escape(ev.formatted_time)}</span>'
            f'<span class="history-action {cls}">{escape(ev.action.value)}</span>'
            f'<span class="text-muted">{escape(ev.actor)}</span>'
        )
        if ev.detail:
            parts.append(f'<span class="history-detail">{escape(ev.detail)}</span>')
        parts.append("</div>")
    parts.append("</div>")
    html = "".join(parts)
    return web.Response(text=html, content_type="text/html")


async def handle_partial_logs(request: web.Request) -> web.Response:
    """Return the last N lines of ~/.swarm/swarm.log, optionally filtered by level."""
    log_path = Path.home() / ".swarm" / "swarm.log"
    if not log_path.exists():
        return web.Response(text="(no log file found)", content_type="text/plain")

    lines_count = min(int(request.query.get("lines", "500")), 5000)
    level_filter = request.query.get("level", "").upper()

    try:
        text = log_path.read_text(errors="replace")
    except OSError:
        return web.Response(text="(could not read log file)", content_type="text/plain")

    all_lines = text.splitlines()
    if level_filter:
        all_lines = [ln for ln in all_lines if level_filter in ln]
    tail = list(reversed(all_lines[-lines_count:]))
    return web.Response(text="\n".join(tail), content_type="text/plain")


def register(app: web.Application) -> None:
    """Register partial routes."""
    app.router.add_get("/partials/workers", handle_partial_workers)
    app.router.add_get("/partials/status", handle_partial_status)
    app.router.add_get("/partials/tasks", handle_partial_tasks)
    app.router.add_get("/partials/system-log", handle_partial_system_log)
    app.router.add_get("/partials/detail/{name}", handle_partial_detail)
    app.router.add_get("/partials/launch-config", handle_partial_launch_config)
    app.router.add_get("/partials/task-history/{task_id}", handle_partial_task_history)
    app.router.add_get("/partials/logs", handle_partial_logs)
