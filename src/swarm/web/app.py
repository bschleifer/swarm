"""Web dashboard — Jinja2 + HTMX frontend served by the daemon."""

from __future__ import annotations

import functools
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

import aiohttp
import aiohttp_jinja2
import jinja2
from aiohttp import web

from swarm.logging import get_logger
from swarm.server.daemon import SwarmOperationError, WorkerNotFoundError, console_log
from swarm.server.helpers import get_daemon, json_error
from swarm.tasks.proposal import QueenAction
from swarm.tasks.task import (
    PRIORITY_LABEL,
    PRIORITY_MAP,
    STATUS_ICON,
    TASK_TYPE_LABEL,
    TYPE_MAP,
    TaskPriority,
    TaskStatus,
    smart_title,
)
from swarm.worker.worker import WorkerState, format_duration

if TYPE_CHECKING:
    from types import ModuleType

    from swarm.config import GroupConfig
    from swarm.queen.queen import Queen
    from swarm.server.daemon import SwarmDaemon

_log = get_logger("web.app")

TEMPLATES_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"


def _get_ws_token(daemon: SwarmDaemon) -> str:
    """Return the token the dashboard should use for WebSocket auth.

    For same-origin page loads the server injects the effective token
    directly into the rendered HTML so the user is never prompted.
    """
    from swarm.server.api import _get_api_password

    return _get_api_password(daemon)


def handle_swarm_errors(
    fn: Callable[..., Awaitable[web.Response]],
) -> Callable[..., Awaitable[web.Response]]:
    """Wrap route handlers with standard error handling."""

    @functools.wraps(fn)
    async def wrapper(request: web.Request) -> web.Response:
        try:
            return await fn(request)
        except WorkerNotFoundError as e:
            return json_error(str(e), 404)
        except SwarmOperationError as e:
            return json_error(str(e), 409)
        except Exception as e:
            console_log(f"Handler error: {e}", level="error")
            return json_error(str(e), 500)

    return wrapper


def _require_queen(d: SwarmDaemon) -> Queen:
    """Return the Queen instance or raise SwarmOperationError."""
    if not d.queen:
        raise SwarmOperationError("Queen not configured")
    return d.queen


def _worker_dicts(daemon: SwarmDaemon) -> list[dict[str, Any]]:
    result = []
    for w in daemon.workers:
        d = w.to_api_dict()
        d["in_config"] = daemon.config.get_worker(w.name) is not None
        # STUNG workers show a countdown to removal
        if d["state"] == WorkerState.STUNG.value:
            remaining = max(0, w.stung_reap_timeout - w.state_duration)
            d["state_duration"] = f"{int(remaining)}s"
        else:
            d["state_duration"] = format_duration(w.state_duration)
        result.append(d)
    return result


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


def _task_dicts(daemon: SwarmDaemon) -> list[dict[str, Any]]:
    all_tasks = daemon.task_board.all_tasks
    completed_ids = {t.id for t in all_tasks if t.status == TaskStatus.COMPLETED}
    return [
        {
            "id": t.id,
            "title": t.title,
            "description": t.description,
            "status": t.status.value,
            "status_icon": STATUS_ICON.get(t.status, "?"),
            "priority": t.priority.value,
            "priority_label": PRIORITY_LABEL.get(t.priority, ""),
            "task_type": t.task_type.value,
            "task_type_label": TASK_TYPE_LABEL.get(t.task_type, "Chore"),
            "assigned_worker": t.assigned_worker,
            "created_age": _format_age(t.created_at),
            "updated_age": _format_age(t.updated_at),
            "tags": t.tags,
            "attachments": t.attachments,
            "depends_on": t.depends_on,
            "blocked": bool(t.depends_on and not all(d in completed_ids for d in t.depends_on)),
            "resolution": t.resolution,
            "source_email_id": t.source_email_id,
            "number": t.number,
        }
        for t in all_tasks
    ]


def _system_log_dicts(
    daemon: SwarmDaemon,
    limit: int = 50,
    category: str | None = None,
    notification_only: bool = False,
    query: str | None = None,
) -> list[dict[str, Any]]:
    """Build system log entry dicts with optional category/notification/text filters.

    When no category is specified, SYSTEM entries are excluded by default
    (they belong on the config page's deeper log).
    """
    from swarm.drones.log import LogCategory

    entries = daemon.drone_log.entries
    if category:
        cats: set[LogCategory] = set()
        for c in category.split(","):
            try:
                cats.add(LogCategory(c.strip()))
            except ValueError:
                pass
        if cats:
            entries = [e for e in entries if e.category in cats]
    else:
        # Exclude system-category entries by default
        entries = [e for e in entries if e.category != LogCategory.SYSTEM]
    if notification_only:
        entries = [e for e in entries if e.is_notification]
    if query:
        q = query.lower()
        entries = [
            e
            for e in entries
            if q in e.worker_name.lower() or q in e.action.value.lower() or q in e.detail.lower()
        ]
    entries = list(reversed(entries[-limit:]))
    return [
        {
            "time": e.formatted_time,
            "action": e.action.value.lower(),
            "worker": e.worker_name,
            "detail": e.detail,
            "category": e.category.value,
            "is_notification": e.is_notification,
        }
        for e in entries
    ]


# --- Routes ---


@aiohttp_jinja2.template("config.html")
async def handle_config_page(request: web.Request) -> dict[str, Any]:
    d = get_daemon(request)
    from swarm.config import serialize_config
    from swarm.update import _get_installed_version

    return {"config": serialize_config(d.config), "version": _get_installed_version()}


@aiohttp_jinja2.template("dashboard.html")
async def handle_dashboard(request: web.Request) -> dict[str, Any]:
    d = get_daemon(request)
    from swarm.update import _get_installed_version, _is_dev_install, build_sha

    selected = request.query.get("worker")

    worker_output = ""
    if selected:
        worker = d.get_worker(selected)
        if worker:
            worker_output = await d.safe_capture_output(selected)

    groups, ungrouped = _build_worker_groups(d)

    proposals = [
        {
            **d.proposal_dict(p),
            "age_str": _format_age(p.created_at),
        }
        for p in d.proposal_store.pending
    ]

    # Build worker→task_title map
    worker_tasks: dict[str, str] = {}
    for t in d.task_board.active_tasks:
        if t.assigned_worker:
            worker_tasks[t.assigned_worker] = t.title

    return {
        "workers": _worker_dicts(d),
        "selected_worker": selected,
        "worker_output": worker_output,
        "tasks": _task_dicts(d),
        "task_summary": d.task_board.summary(),
        "worker_count": len(d.workers),
        "drones_enabled": d.pilot.enabled if d.pilot else False,
        "groups": groups,
        "ungrouped": ungrouped,
        "ws_auth_required": True,  # auth is always required (auto-token if no explicit password)
        "ws_token": _get_ws_token(d),
        "proposals": proposals,
        "proposal_count": len(proposals),
        "worker_tasks": worker_tasks,
        "tool_buttons": [{"label": b.label, "command": b.command} for b in d.config.tool_buttons],
        "action_buttons": [
            {
                "label": b.label,
                "action": b.action,
                "command": b.command,
                "style": b.style,
                "show_mobile": b.show_mobile,
                "show_desktop": b.show_desktop,
            }
            for b in d.config.action_buttons
        ],
        "task_buttons": [
            {
                "label": b.label,
                "action": b.action,
                "show_mobile": b.show_mobile,
                "show_desktop": b.show_desktop,
            }
            for b in d.config.task_buttons
        ],
        "tunnel": d.tunnel.to_dict(),
        "version": _get_installed_version(),
        "is_dev": _is_dev_install(),
        "build_sha": build_sha(),
    }


# --- Partials (HTMX) ---


def _build_worker_groups(daemon: SwarmDaemon) -> tuple[list[dict], list[dict]]:
    """Build grouped worker data for the sidebar template.

    The auto-generated "all" group is skipped when other groups exist
    (it's a catch-all that duplicates the real groups).  Each worker
    appears in the first group that claims it.
    """
    workers = _worker_dicts(daemon)
    config_groups = daemon.config.groups
    if not config_groups:
        return [], []

    # Skip the auto-generated "all" group when real groups exist
    real_groups = [g for g in config_groups if g.name.lower() != "all"]
    if real_groups:
        config_groups = real_groups

    worker_map = {w["name"].lower(): w for w in workers}
    grouped_names: set[str] = set()
    groups = []

    for g in config_groups:
        members = _collect_group_members(g, worker_map, grouped_names)
        if members:
            worst = min(members, key=lambda w: WorkerState(w["state"]).priority)
            groups.append(
                {
                    "name": g.name,
                    "members": members,
                    "worker_count": len(members),
                    "worst_state": worst["state"],
                }
            )

    ungrouped = [w for w in workers if w["name"].lower() not in grouped_names]
    return groups, ungrouped


def _collect_group_members(
    group: GroupConfig, worker_map: dict[str, Any], grouped_names: set[str]
) -> list[dict[str, Any]]:
    """Collect workers for a group, skipping already-claimed ones."""
    members = []
    for name in group.workers:
        key = name.lower()
        if key in grouped_names:
            continue
        w = worker_map.get(key)
        if w:
            members.append(w)
            grouped_names.add(key)
    return members


@aiohttp_jinja2.template("partials/worker_list.html")
async def handle_partial_workers(request: web.Request) -> dict[str, Any]:
    d = get_daemon(request)
    groups, ungrouped = _build_worker_groups(d)
    worker_tasks: dict[str, str] = {}
    for t in d.task_board.active_tasks:
        if t.assigned_worker:
            worker_tasks[t.assigned_worker] = t.title
    return {
        "workers": _worker_dicts(d),
        "groups": groups,
        "ungrouped": ungrouped,
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


# --- Actions ---


@handle_swarm_errors
async def handle_action_send(request: web.Request) -> web.Response:
    d = get_daemon(request)
    name = request.match_info["name"]
    data = await request.post()
    message = data.get("message", "")
    if message:
        await d.send_to_worker(name, message)
        console_log(f'Message sent to "{name}"')
    return web.Response(status=204)


@handle_swarm_errors
async def handle_action_continue(request: web.Request) -> web.Response:
    d = get_daemon(request)
    name = request.match_info["name"]
    await d.continue_worker(name)
    console_log(f'Continued "{name}"')
    return web.Response(status=204)


@handle_swarm_errors
async def handle_action_toggle_drones(request: web.Request) -> web.Response:
    d = get_daemon(request)
    if d.pilot:
        new_state = d.toggle_drones()
        console_log(f"Drones toggled {'ON' if new_state else 'OFF'}")
        return web.json_response({"enabled": new_state})
    return web.json_response({"error": "pilot not running", "enabled": False})


@handle_swarm_errors
async def handle_action_continue_all(request: web.Request) -> web.Response:
    d = get_daemon(request)
    count = await d.continue_all()
    console_log(f"Continue all — {count} worker(s)")
    return web.json_response({"count": count})


@handle_swarm_errors
async def handle_action_kill(request: web.Request) -> web.Response:
    d = get_daemon(request)
    name = request.match_info["name"]
    await d.kill_worker(name)
    console_log(f'Killed worker "{name}"', level="warn")
    return web.json_response({"status": "killed", "worker": name})


@handle_swarm_errors
async def handle_action_revive(request: web.Request) -> web.Response:
    d = get_daemon(request)
    name = request.match_info["name"]
    await d.revive_worker(name)
    console_log(f'Revived worker "{name}"')
    return web.json_response({"status": "revived", "worker": name})


@handle_swarm_errors
async def handle_action_escape(request: web.Request) -> web.Response:
    d = get_daemon(request)
    name = request.match_info["name"]
    await d.escape_worker(name)
    console_log(f'Escape sent to "{name}"')
    return web.json_response({"status": "escape_sent", "worker": name})


@handle_swarm_errors
async def handle_action_redraw(request: web.Request) -> web.Response:
    d = get_daemon(request)
    name = request.match_info["name"]
    await d.redraw_worker(name)
    return web.json_response({"status": "redraw_sent", "worker": name})


@handle_swarm_errors
async def handle_action_send_all(request: web.Request) -> web.Response:
    d = get_daemon(request)
    data = await request.post()
    message = data.get("message", "")
    if not message:
        return json_error("message required")

    count = await d.send_all(message)
    console_log(f"Broadcast sent to {count} worker(s)")
    return web.json_response({"count": count})


@handle_swarm_errors
async def handle_action_create_task(request: web.Request) -> web.Response:
    d = get_daemon(request)
    data = await request.post()

    priority = PRIORITY_MAP.get(data.get("priority", "normal"), TaskPriority.NORMAL)
    type_str = data.get("task_type", "").strip()
    task_type = TYPE_MAP.get(type_str) if type_str else None

    deps_raw = data.get("depends_on", "").strip()
    depends_on = [x.strip() for x in deps_raw.split(",") if x.strip()] if deps_raw else None

    att_raw = data.get("attachments", "").strip()
    attachments = [a.strip() for a in att_raw.split(",") if a.strip()] if att_raw else None

    task = await d.create_task_smart(
        title=data.get("title", "").strip(),
        description=data.get("description", ""),
        priority=priority,
        task_type=task_type,
        depends_on=depends_on,
        attachments=attachments,
        source_email_id=data.get("source_email_id", "").strip(),
    )
    console_log(f'Task created: "{task.title}" ({task.priority.value}, {task.task_type.value})')
    return web.json_response({"id": task.id, "title": task.title}, status=201)


@handle_swarm_errors
async def handle_action_assign_task(request: web.Request) -> web.Response:
    d = get_daemon(request)
    data = await request.post()
    task_id = data.get("task_id", "")
    worker_name = data.get("worker", "")
    if not task_id or not worker_name:
        return json_error("task_id and worker required")

    await d.assign_task(task_id, worker_name)
    console_log(f'Task assigned to "{worker_name}"')
    return web.json_response({"status": "assigned", "task_id": task_id, "worker": worker_name})


@handle_swarm_errors
async def handle_action_complete_task(request: web.Request) -> web.Response:
    d = get_daemon(request)
    data = await request.post()
    task_id = data.get("task_id", "")
    resolution = data.get("resolution", "").strip()
    if not task_id:
        return json_error("task_id required")

    d.complete_task(task_id, resolution=resolution)
    console_log(f"Task completed: {task_id[:8]}")
    return web.json_response({"status": "completed", "task_id": task_id})


@handle_swarm_errors
async def handle_action_ask_queen(request: web.Request) -> web.Response:
    d = get_daemon(request)
    queen = _require_queen(d)

    console_log("Queen coordinating hive...")
    result = await d.coordinate_hive(force=True)
    n = len(result.get("directives", []))
    console_log(f"Queen done — {n} directive(s)")
    result["cooldown"] = queen.cooldown_remaining
    return web.json_response(result)


@handle_swarm_errors
async def handle_action_ask_queen_question(request: web.Request) -> web.Response:
    d = get_daemon(request)
    data = await request.post()
    question = data.get("question", "").strip()
    if not question:
        return json_error("question required")

    queen = _require_queen(d)

    console_log(f"Queen asked: {question[:60]}...")
    hive_ctx = await d.gather_hive_context()
    prompt = (
        f"You are the Queen of a swarm of {queen.provider_display_name} agents.\n\n"
        f"{hive_ctx}\n\n"
        f"The operator asks: {question}\n\n"
        f"Respond with a JSON object:\n"
        f'{{\n  "assessment": "your analysis",\n'
        f'  "directives": ['
        f'{{"worker": "name", '
        f'"action": "continue|send_message|restart|wait", '
        f'"message": "if applicable", "reason": "why"}}],\n'
        f'  "suggestions": ["any high-level suggestions"]\n}}'
    )
    result = await queen.ask(prompt, force=True)
    console_log("Queen answered operator question")
    result["cooldown"] = queen.cooldown_remaining
    return web.json_response(result)


# --- A1: Send Interrupt (Ctrl-C) ---


@handle_swarm_errors
async def handle_action_interrupt(request: web.Request) -> web.Response:
    d = get_daemon(request)
    name = request.match_info["name"]
    await d.interrupt_worker(name)
    console_log(f'Ctrl-C sent to "{name}"')
    return web.json_response({"status": "interrupt_sent", "worker": name})


# --- A2: Task Removal ---


@handle_swarm_errors
async def handle_action_remove_task(request: web.Request) -> web.Response:
    d = get_daemon(request)
    data = await request.post()
    task_id = data.get("task_id", "")
    if not task_id:
        return json_error("task_id required")

    d.remove_task(task_id)
    console_log(f"Task removed: {task_id[:8]}")
    return web.json_response({"status": "removed", "task_id": task_id})


# --- A3: Task Failure ---


@handle_swarm_errors
async def handle_action_fail_task(request: web.Request) -> web.Response:
    d = get_daemon(request)
    data = await request.post()
    task_id = data.get("task_id", "")
    if not task_id:
        return json_error("task_id required")

    d.fail_task(task_id)
    console_log(f"Task failed: {task_id[:8]}", level="warn")
    return web.json_response({"status": "failed", "task_id": task_id})


@handle_swarm_errors
async def handle_action_reopen_task(request: web.Request) -> web.Response:
    d = get_daemon(request)
    data = await request.post()
    task_id = data.get("task_id", "")
    if not task_id:
        return json_error("task_id required")

    d.reopen_task(task_id)
    console_log(f"Task reopened: {task_id[:8]}")
    return web.json_response({"status": "reopened", "task_id": task_id})


@handle_swarm_errors
async def handle_action_unassign_task(request: web.Request) -> web.Response:
    d = get_daemon(request)
    data = await request.post()
    task_id = data.get("task_id", "")
    if not task_id:
        return json_error("task_id required")

    d.unassign_task(task_id)
    console_log(f"Task unassigned: {task_id[:8]}")
    return web.json_response({"status": "unassigned", "task_id": task_id})


# --- A4: Per-worker Queen Analysis ---


@handle_swarm_errors
async def handle_action_ask_queen_worker(request: web.Request) -> web.Response:
    d = get_daemon(request)
    name = request.match_info["name"]

    _require_queen(d)

    console_log(f'Queen analyzing "{name}"...')
    result = await d.analyze_worker(name, force=True)
    console_log(f'Queen analysis of "{name}" complete')

    # If Queen recommends complete_task, create a proper completion proposal
    # so the user gets the full completion dialog (with draft email option).
    if result.get("action") == QueenAction.COMPLETE_TASK and d.task_board:
        from swarm.tasks.proposal import AssignmentProposal

        active = [
            t
            for t in d.task_board.tasks_for_worker(name)
            if t.status in (TaskStatus.ASSIGNED, TaskStatus.IN_PROGRESS)
        ]
        if active:
            task = active[0]
            proposal = AssignmentProposal.completion(
                worker_name=name,
                task_id=task.id,
                task_title=task.title,
                assessment=result.get("assessment", ""),
                reasoning=result.get("reasoning", ""),
                confidence=result.get("confidence", 0.8),
            )
            d.queue_proposal(proposal)
            console_log(f'Created completion proposal for "{task.title}"')

    result["cooldown"] = d.queen.cooldown_remaining
    return web.json_response(result)


# --- A5: Group-targeted Send ---


@handle_swarm_errors
async def handle_action_send_group(request: web.Request) -> web.Response:
    d = get_daemon(request)
    data = await request.post()
    message = data.get("message", "")
    group_name = data.get("group", "")
    if not message:
        return json_error("message required")
    if not group_name:
        return json_error("group required")

    count = await d.send_group(group_name, message)
    console_log(f'Group "{group_name}" — sent to {count} worker(s)')
    return web.json_response({"count": count})


# --- A6: Launch Brood ---


@handle_swarm_errors
async def handle_action_launch(request: web.Request) -> web.Response:
    d = get_daemon(request)

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
        return json_error("no workers to launch")

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


async def handle_partial_launch_config(request: web.Request) -> web.Response:
    d = get_daemon(request)
    running_names = {w.name.lower() for w in d.workers}
    workers = [
        {"name": w.name, "path": w.path, "running": w.name.lower() in running_names}
        for w in d.config.workers
    ]
    groups = [{"name": g.name, "workers": g.workers} for g in d.config.groups]
    return web.json_response({"workers": workers, "groups": groups})


@handle_swarm_errors
async def handle_action_spawn(request: web.Request) -> web.Response:
    """Spawn a single worker into the running session."""
    d = get_daemon(request)
    data = await request.post()
    name = data.get("name", "").strip()
    path = data.get("path", "").strip()

    if not name or not path:
        return json_error("name and path required")

    from swarm.config import WorkerConfig

    wc = WorkerConfig(name=name, path=path)
    worker = await d.spawn_worker(wc)
    console_log(f'Spawned worker "{worker.name}"')
    return web.json_response({"status": "spawned", "worker": worker.name})


# --- A8: Kill Session (Shutdown) ---


@handle_swarm_errors
async def handle_action_kill_session(request: web.Request) -> web.Response:
    d = get_daemon(request)
    data = await request.post()
    all_sessions = data.get("all", "") == "1"
    scope = "all sessions" if all_sessions else "session"
    console_log(f"Killing {scope} — all workers terminated", level="warn")
    await d.kill_session(all_sessions=all_sessions)
    return web.json_response({"status": "killed"})


@handle_swarm_errors
async def handle_action_edit_task(request: web.Request) -> web.Response:
    d = get_daemon(request)
    data = await request.post()
    task_id = data.get("task_id", "")
    if not task_id:
        return json_error("task_id required")

    kwargs: dict[str, Any] = {}
    title = data.get("title", "").strip()
    desc = data.get("description")
    if title:
        kwargs["title"] = title
    elif "title" in data and desc:
        # Title cleared — regenerate from description
        kwargs["title"] = await smart_title(desc)
    if desc is not None:
        kwargs["description"] = desc
    if data.get("priority") and data["priority"] in PRIORITY_MAP:
        kwargs["priority"] = PRIORITY_MAP[data["priority"]]
    if data.get("task_type") and data["task_type"] in TYPE_MAP:
        kwargs["task_type"] = TYPE_MAP[data["task_type"]]
    tags_raw = data.get("tags", "").strip()
    if tags_raw:
        kwargs["tags"] = [t.strip() for t in tags_raw.split(",") if t.strip()]
    deps_raw = data.get("depends_on", "")
    if deps_raw:
        kwargs["depends_on"] = [x.strip() for x in deps_raw.strip().split(",") if x.strip()]

    d.edit_task(task_id, **kwargs)
    console_log(f"Task edited: {task_id[:8]}")
    return web.json_response({"status": "updated", "task_id": task_id})


async def handle_action_upload_attachment(request: web.Request) -> web.Response:
    d = get_daemon(request)
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
        return json_error("task_id and file required")

    task = d.task_board.get(task_id)
    if not task:
        return json_error(f"Task '{task_id}' not found", 404)

    path = d.save_attachment(file_name, file_data)
    new_attachments = [*task.attachments, path]
    d.task_board.update(task_id, attachments=new_attachments)

    console_log(f"Attachment uploaded: {file_name}")
    return web.json_response({"status": "uploaded", "path": path}, status=201)


async def handle_action_fetch_outlook_email(request: web.Request) -> web.Response:
    """Fetch an email from Microsoft Graph API using a message ID."""
    d = get_daemon(request)
    data = await request.post()
    message_id = data.get("message_id", "").strip()
    if not message_id:
        return json_error("message_id required")

    # Check if Graph API is configured and connected
    if not d.graph_mgr:
        return json_error("Microsoft Graph not configured")

    graph_token = await d.graph_mgr.get_token()
    if not graph_token:
        return json_error("Microsoft Graph not connected — authenticate first")

    console_log(f"Fetching email via Graph: {message_id[:30]}...")
    return await _fetch_graph_email(d, message_id, graph_token)


async def _translate_exchange_id(
    sess: aiohttp.ClientSession, headers: dict[str, str], ews_id: str
) -> str | None:
    """Translate an EWS-format message ID to REST format via Graph API."""
    url = "https://graph.microsoft.com/v1.0/me/translateExchangeIds"
    payload = {
        "inputIds": [ews_id],
        "sourceIdType": "ewsId",
        "targetIdType": "restId",
    }
    try:
        async with sess.post(url, headers=headers, json=payload) as resp:
            if resp.status != 200:
                err = await resp.text()
                console_log(f"translateExchangeIds failed ({resp.status}): {err[:200]}")
                return None
            data = await resp.json()
            values = data.get("value", [])
            if values:
                return values[0].get("targetId")
    except Exception as exc:
        console_log(f"translateExchangeIds error: {exc}")
    return None


async def _fetch_attachment_bytes(
    sess: aiohttp.ClientSession,
    headers: dict[str, str],
    msg_id: str,
    att_id: str,
    quote: Callable[..., str],
    yarl: ModuleType,
) -> str:
    """Fetch a single attachment's contentBytes from Graph API."""
    import aiohttp as _aiohttp

    encoded_msg = quote(msg_id, safe="")
    encoded_att = quote(att_id, safe="")
    url = yarl.URL(
        f"https://graph.microsoft.com/v1.0/me/messages/{encoded_msg}/attachments/{encoded_att}",
        encoded=True,
    )
    try:
        async with sess.get(url, headers=headers, timeout=_aiohttp.ClientTimeout(total=30)) as resp:
            if resp.status != 200:
                console_log(f"Attachment fetch failed ({resp.status})")
                return ""
            data = await resp.json()
            return data.get("contentBytes", "")
    except Exception as exc:
        console_log(f"Attachment fetch error: {exc}")
        return ""


async def _fetch_graph_email(d: SwarmDaemon, message_id: str, token: str) -> web.Response:
    """Fetch email + attachments from Microsoft Graph API."""
    from urllib.parse import quote

    import aiohttp as _aiohttp
    import yarl

    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    base = "https://graph.microsoft.com/v1.0/me/messages"

    async def _graph_get(sess: _aiohttp.ClientSession, mid: str) -> tuple[int, str]:
        encoded = quote(mid, safe="")
        url = yarl.URL(f"{base}/{encoded}?$expand=attachments", encoded=True)
        console_log(f"Graph GET: {str(url)[:120]}...")
        async with sess.get(url, headers=headers, timeout=_aiohttp.ClientTimeout(total=15)) as resp:
            return resp.status, await resp.text()

    import json as _json

    try:
        async with _aiohttp.ClientSession() as sess:
            effective_id = message_id
            status, body = await _graph_get(sess, message_id)

            # If 400/404, the ID may be in EWS format — translate to REST format
            if status in (400, 404) and ("/" in message_id or "+" in message_id):
                console_log("Direct fetch failed; translating EWS ID to REST format...")
                rest_id = await _translate_exchange_id(sess, headers, message_id)
                if rest_id and rest_id != message_id:
                    effective_id = rest_id
                    status, body = await _graph_get(sess, rest_id)

            if status != 200:
                console_log(f"Graph API error {status}: {body[:200]}", level="error")
                return json_error(f"Graph API {status}: {body[:200]}")

            msg = _json.loads(body)

            # Individually fetch attachment bytes if missing (Graph sometimes omits them)
            attachments = msg.get("attachments", [])
            for att in attachments:
                if (
                    att.get("@odata.type") == "#microsoft.graph.fileAttachment"
                    and not att.get("contentBytes")
                    and att.get("id")
                ):
                    att["contentBytes"] = await _fetch_attachment_bytes(
                        sess, headers, effective_id, att["id"], quote, yarl
                    )
    except Exception as exc:
        return json_error(str(exc)[:200])

    # Delegate business logic (HTML→text, title gen, type classification) to daemon
    result = await d.process_email_data(
        subject=msg.get("subject", ""),
        body_content=msg.get("body", {}).get("content", ""),
        body_type=msg.get("body", {}).get("contentType", "text"),
        attachment_dicts=attachments,
        effective_id=effective_id,
    )
    return web.json_response(result)


@handle_swarm_errors
async def handle_action_fetch_image(request: web.Request) -> web.Response:
    """Fetch an external image URL and save it as an attachment."""
    d = get_daemon(request)
    data = await request.post()
    url = data.get("url", "").strip()
    if not url:
        return json_error("url required")

    path = await d.fetch_and_save_image(url)
    return web.json_response({"path": path}, status=201)


async def handle_action_upload(request: web.Request) -> web.Response:
    """Upload a file and return its absolute server path."""
    d = get_daemon(request)
    reader = await request.multipart()

    file_data = None
    file_name = "upload"

    while True:
        field = await reader.next()
        if field is None:
            break
        if field.name == "file":
            file_name = field.filename or "upload"
            file_data = await field.read(decode=False)

    if file_data is None:
        return json_error("file required")

    path = d.save_attachment(file_name, file_data)
    console_log(f"File uploaded: {file_name} → {path}")
    return web.json_response({"path": path}, status=201)


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
    html = '<div class="history-container">'
    for ev in events:
        cls = action_class.get(ev.action.value, "text-muted")
        html += (
            f'<div class="history-entry">'
            f'<span class="history-time">{escape(ev.formatted_time)}</span>'
            f'<span class="history-action {cls}">{escape(ev.action.value)}</span>'
            f'<span class="text-muted">{escape(ev.actor)}</span>'
        )
        if ev.detail:
            html += f'<span class="history-detail">{escape(ev.detail)}</span>'
        html += "</div>"
    html += "</div>"
    return web.Response(text=html, content_type="text/html")


@handle_swarm_errors
async def handle_action_retry_draft(request: web.Request) -> web.Response:
    d = get_daemon(request)
    data = await request.post()
    task_id = data.get("task_id", "")
    if not task_id:
        return json_error("task_id required")

    await d.retry_draft_reply(task_id)
    console_log(f"Retrying draft reply for task {task_id[:8]}")
    return web.json_response({"status": "retrying", "task_id": task_id})


@handle_swarm_errors
async def handle_action_approve_proposal(request: web.Request) -> web.Response:
    d = get_daemon(request)
    data = await request.post()
    proposal_id = data.get("proposal_id", "")
    if not proposal_id:
        return json_error("proposal_id required")
    draft_response = data.get("draft_response") == "true"
    await d.approve_proposal(proposal_id, draft_response=draft_response)
    console_log(f"Proposal approved: {proposal_id[:8]}")
    return web.json_response({"status": "approved", "proposal_id": proposal_id})


@handle_swarm_errors
async def handle_action_reject_proposal(request: web.Request) -> web.Response:
    d = get_daemon(request)
    data = await request.post()
    proposal_id = data.get("proposal_id", "")
    if not proposal_id:
        return json_error("proposal_id required")
    d.reject_proposal(proposal_id)
    console_log(f"Proposal rejected: {proposal_id[:8]}")
    return web.json_response({"status": "rejected", "proposal_id": proposal_id})


@handle_swarm_errors
async def handle_action_reject_all_proposals(request: web.Request) -> web.Response:
    d = get_daemon(request)
    count = d.reject_all_proposals()
    console_log(f"All proposals rejected ({count})")
    return web.json_response({"status": "rejected_all", "count": count})


@handle_swarm_errors
async def handle_action_stop_server(request: web.Request) -> web.Response:
    """Trigger graceful shutdown of the web server."""
    console_log("Web server stopping...")
    shutdown_event = request.app.get("shutdown_event")
    if shutdown_event:
        shutdown_event.set()
        return web.json_response({"status": "stopping"})
    return json_error("no shutdown event configured", 500)


# --- Tunnel ---


@handle_swarm_errors
async def handle_action_tunnel_start(request: web.Request) -> web.Response:
    d = get_daemon(request)
    if d.tunnel.is_running:
        return web.json_response(d.tunnel.to_dict())
    try:
        await d.tunnel.start()
    except RuntimeError as e:
        return json_error(str(e), 500)
    result = d.tunnel.to_dict()
    if not d.config.api_password:
        result["warning"] = "Tunnel is public — set api_password for security"
    console_log(f"Tunnel started: {d.tunnel.url}")
    return web.json_response(result)


@handle_swarm_errors
async def handle_action_tunnel_stop(request: web.Request) -> web.Response:
    d = get_daemon(request)
    await d.tunnel.stop()
    console_log("Tunnel stopped")
    return web.json_response(d.tunnel.to_dict())


# --- Microsoft Graph OAuth ---


async def handle_graph_login(request: web.Request) -> web.Response:
    """Start OAuth flow: generate PKCE, redirect to Microsoft."""
    import secrets as _secrets

    d = get_daemon(request)
    if not d.graph_mgr:
        return web.Response(text="Graph not configured — set client_id in Config", status=400)

    from swarm.auth.graph import generate_pkce_verifier

    state = _secrets.token_urlsafe(16)
    verifier = generate_pkce_verifier()
    d._graph_auth_pending[state] = verifier

    auth_url = d.graph_mgr.get_auth_url(state, verifier)
    raise web.HTTPFound(auth_url)


async def handle_graph_callback(request: web.Request) -> web.Response:
    """OAuth callback — exchange code for tokens, redirect to /config."""
    d = get_daemon(request)
    code = request.query.get("code", "")
    state = request.query.get("state", "")
    error = request.query.get("error", "")

    if error:
        return web.Response(
            text=f"Microsoft auth error: {error} — {request.query.get('error_description', '')}",
            status=400,
        )

    if not code or not state:
        return web.Response(text="Missing code or state parameter", status=400)

    verifier = d._graph_auth_pending.pop(state, None)
    if not verifier:
        return web.Response(
            text="Auth state expired (server was likely restarted). "
            'Go back to Config → Integrations and click "Connect Microsoft Account" again.',
            status=400,
        )

    if not d.graph_mgr:
        return web.Response(text="Graph not configured", status=400)

    ok = await d.graph_mgr.exchange_code(code, verifier)
    if not ok:
        detail = d.graph_mgr.last_error or "unknown error"
        console_log(f"Graph token exchange failed: {detail}", level="error")
        return web.Response(
            text=f"Token exchange failed: {detail}",
            content_type="text/plain",
            status=400,
        )

    console_log("Microsoft Graph connected")
    raise web.HTTPFound("/config")


async def handle_graph_status(request: web.Request) -> web.Response:
    """Return Graph connection status as JSON."""
    d = get_daemon(request)
    if not d.graph_mgr:
        return web.json_response({"connected": False, "configured": False})
    return web.json_response({"connected": d.graph_mgr.is_connected(), "configured": True})


async def handle_graph_disconnect(request: web.Request) -> web.Response:
    """Disconnect Microsoft Graph (delete tokens)."""
    d = get_daemon(request)
    if d.graph_mgr:
        d.graph_mgr.disconnect()
        console_log("Microsoft Graph disconnected")
    return web.json_response({"status": "disconnected"})


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


async def handle_action_clear_logs(request: web.Request) -> web.Response:
    """Truncate ~/.swarm/swarm.log."""
    log_path = Path.home() / ".swarm" / "swarm.log"
    try:
        log_path.write_text("")
        console_log("Log file cleared")
    except OSError as e:
        return json_error(str(e), 500)
    return web.json_response({"status": "cleared"})


async def handle_bee_icon(request: web.Request) -> web.Response:
    """Serve the bee icon SVG with caching."""
    static = request.app.get("static_dir", STATIC_DIR)
    return web.FileResponse(
        static / "bee-icon.svg",
        headers={"Cache-Control": "public, max-age=86400"},
    )


async def handle_service_worker(request: web.Request) -> web.Response:
    """Serve sw.js from root path (service workers need root scope)."""
    static = request.app.get("static_dir", STATIC_DIR)
    return web.FileResponse(
        static / "sw.js",
        headers={"Content-Type": "application/javascript", "Cache-Control": "no-cache"},
    )


async def handle_offline_page(request: web.Request) -> web.Response:
    """Serve the PWA offline fallback page."""
    static = request.app.get("static_dir", STATIC_DIR)
    return web.FileResponse(static / "offline.html")


async def handle_manifest(request: web.Request) -> web.Response:
    """PWA manifest for add-to-homescreen support."""
    manifest = {
        "name": "Swarm",
        "short_name": "Swarm",
        "start_url": "/",
        "display": "standalone",
        "background_color": "#2A1B0E",
        "theme_color": "#D8A03D",
        "icons": [
            {
                "src": "/static/icon-192.png",
                "sizes": "192x192",
                "type": "image/png",
                "purpose": "any maskable",
            },
            {
                "src": "/static/icon-512.png",
                "sizes": "512x512",
                "type": "image/png",
                "purpose": "any maskable",
            },
        ],
    }
    return web.json_response(manifest)


@handle_swarm_errors
async def handle_action_check_update(request: web.Request) -> web.Response:
    """Force a fresh update check and return the result."""
    from swarm.update import check_for_update, update_result_to_dict

    d = get_daemon(request)
    result = await check_for_update(force=True)
    d._update_result = result
    if result.available:
        d.broadcast_ws({"type": "update_available", **update_result_to_dict(result)})
    return web.json_response(update_result_to_dict(result))


@handle_swarm_errors
async def handle_action_install_update(request: web.Request) -> web.Response:
    """Install the update via uv tool reinstall."""
    from swarm.update import perform_update

    d = get_daemon(request)
    console_log("Installing update...")

    def _on_output(line: str) -> None:
        d.broadcast_ws({"type": "update_progress", "line": line})

    success, output = await perform_update(on_output=_on_output)
    if success:
        console_log("Update installed successfully")
        d.broadcast_ws({"type": "update_installed"})
    else:
        console_log(f"Update failed: {output[:200]}", level="error")
        d.broadcast_ws({"type": "update_failed"})
    return web.json_response({"success": success, "output": output})


@handle_swarm_errors
async def handle_action_update_and_restart(request: web.Request) -> web.Response:
    """Install the update and restart the server process via os.execv."""
    from swarm.update import perform_update

    d = get_daemon(request)
    console_log("Installing update and restarting...")

    def _on_output(line: str) -> None:
        d.broadcast_ws({"type": "update_progress", "line": line})

    success, output = await perform_update(on_output=_on_output)
    if not success:
        console_log(f"Update failed: {output[:200]}", level="error")
        d.broadcast_ws({"type": "update_failed"})
        return web.json_response({"success": False, "output": output})

    console_log("Update installed — restarting server")
    d.broadcast_ws({"type": "update_restarting"})
    restart_flag = request.app.get("restart_flag")
    if restart_flag is not None:
        restart_flag["requested"] = True
    shutdown_event = request.app.get("shutdown_event")
    if shutdown_event:
        shutdown_event.set()
    return web.json_response({"success": True, "restarting": True})


def _resolve_web_dirs(app: web.Application) -> tuple[Path, Path]:
    """Resolve templates/static dirs, preferring source tree for dev mode.

    Set SWARM_DEV=1 (uses config file's parent as source root) or
    SWARM_DEV=/path/to/swarm to serve templates and static files from
    the source tree. Edits are reflected on page reload without reinstalling.
    """
    import os

    templates_dir = TEMPLATES_DIR
    static_dir = STATIC_DIR

    dev_val = os.environ.get("SWARM_DEV", "")
    dev_root = ""
    if dev_val and dev_val != "0":
        if dev_val == "1":
            # Resolve from config file location
            daemon = app.get("daemon")
            if daemon and getattr(daemon.config, "source_path", None):
                dev_root = str(Path(daemon.config.source_path).parent)
        else:
            dev_root = dev_val

    if dev_root:
        src_web = Path(dev_root) / "src" / "swarm" / "web"
        src_templates = src_web / "templates"
        src_static = src_web / "static"
        if src_templates.is_dir():
            templates_dir = src_templates
            _log.info("dev mode: serving templates from %s", templates_dir)
        if src_static.is_dir():
            static_dir = src_static
            _log.info("dev mode: serving static from %s", static_dir)

    return templates_dir, static_dir


def setup_web_routes(app: web.Application) -> None:
    """Add web dashboard routes to an aiohttp app."""
    import os

    templates_dir, static_dir = _resolve_web_dirs(app)
    app["static_dir"] = static_dir

    env = aiohttp_jinja2.setup(
        app,
        loader=jinja2.FileSystemLoader(str(templates_dir)),
        autoescape=jinja2.select_autoescape(["html"]),
    )
    env.filters["basename"] = lambda p: os.path.basename(p)

    app.router.add_get("/", handle_dashboard)
    app.router.add_get("/dashboard", handle_dashboard)
    app.router.add_get("/config", handle_config_page)
    app.router.add_get("/manifest.json", handle_manifest)
    app.router.add_get("/partials/workers", handle_partial_workers)
    app.router.add_get("/partials/status", handle_partial_status)
    app.router.add_get("/partials/tasks", handle_partial_tasks)
    app.router.add_get("/partials/system-log", handle_partial_system_log)
    app.router.add_get("/partials/detail/{name}", handle_partial_detail)
    app.router.add_post("/action/send/{name}", handle_action_send)
    app.router.add_post("/action/continue/{name}", handle_action_continue)
    app.router.add_post("/action/kill/{name}", handle_action_kill)
    app.router.add_post("/action/revive/{name}", handle_action_revive)
    app.router.add_post("/action/escape/{name}", handle_action_escape)
    app.router.add_post("/action/redraw/{name}", handle_action_redraw)
    app.router.add_post("/action/toggle-drones", handle_action_toggle_drones)
    app.router.add_post("/action/continue-all", handle_action_continue_all)
    app.router.add_post("/action/send-all", handle_action_send_all)
    app.router.add_post("/action/task/create", handle_action_create_task)
    app.router.add_post("/action/task/assign", handle_action_assign_task)
    app.router.add_post("/action/task/complete", handle_action_complete_task)
    app.router.add_post("/action/task/remove", handle_action_remove_task)
    app.router.add_post("/action/task/fail", handle_action_fail_task)
    app.router.add_post("/action/task/unassign", handle_action_unassign_task)
    app.router.add_post("/action/task/reopen", handle_action_reopen_task)
    app.router.add_post("/action/ask-queen", handle_action_ask_queen)
    app.router.add_post("/action/ask-queen-question", handle_action_ask_queen_question)
    app.router.add_post("/action/ask-queen/{name}", handle_action_ask_queen_worker)
    app.router.add_post("/action/interrupt/{name}", handle_action_interrupt)
    app.router.add_post("/action/send-group", handle_action_send_group)
    app.router.add_post("/action/launch", handle_action_launch)
    app.router.add_post("/action/spawn", handle_action_spawn)
    app.router.add_get("/partials/launch-config", handle_partial_launch_config)
    app.router.add_post("/action/kill-session", handle_action_kill_session)
    app.router.add_post("/action/task/edit", handle_action_edit_task)
    app.router.add_post("/action/task/upload", handle_action_upload_attachment)
    app.router.add_post("/action/upload", handle_action_upload)
    app.router.add_post("/action/fetch-image", handle_action_fetch_image)
    app.router.add_post("/action/fetch-outlook-email", handle_action_fetch_outlook_email)
    app.router.add_get("/partials/task-history/{task_id}", handle_partial_task_history)
    app.router.add_post("/action/stop-server", handle_action_stop_server)
    app.router.add_get("/partials/logs", handle_partial_logs)
    app.router.add_post("/action/clear-logs", handle_action_clear_logs)
    app.router.add_post("/action/task/retry-draft", handle_action_retry_draft)
    app.router.add_post("/action/proposal/approve", handle_action_approve_proposal)
    app.router.add_post("/action/proposal/reject", handle_action_reject_proposal)
    app.router.add_post("/action/proposal/reject-all", handle_action_reject_all_proposals)
    # Tunnel
    app.router.add_post("/action/tunnel/start", handle_action_tunnel_start)
    app.router.add_post("/action/tunnel/stop", handle_action_tunnel_stop)
    # Microsoft Graph OAuth
    app.router.add_get("/auth/graph/login", handle_graph_login)
    app.router.add_get("/auth/graph/callback", handle_graph_callback)
    app.router.add_get("/auth/graph/status", handle_graph_status)
    app.router.add_post("/auth/graph/disconnect", handle_graph_disconnect)

    # Updates
    app.router.add_post("/action/check-update", handle_action_check_update)
    app.router.add_post("/action/install-update", handle_action_install_update)
    app.router.add_post("/action/update-and-restart", handle_action_update_and_restart)

    app.router.add_static("/static", static_dir)
    app.router.add_get("/bee-icon.svg", handle_bee_icon)
    app.router.add_get("/sw.js", handle_service_worker)
    app.router.add_get("/offline.html", handle_offline_page)
