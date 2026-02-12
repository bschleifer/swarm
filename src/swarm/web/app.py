"""Web dashboard — Jinja2 + HTMX frontend served by the daemon."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import aiohttp_jinja2
import jinja2
from aiohttp import web

from swarm.logging import get_logger
from swarm.server.daemon import SwarmOperationError, WorkerNotFoundError, console_log
from swarm.tasks.task import (
    PRIORITY_LABEL,
    PRIORITY_MAP,
    STATUS_ICON,
    TASK_TYPE_LABEL,
    TYPE_MAP,
    TaskPriority,
    auto_classify_type,
    smart_title,
)

if TYPE_CHECKING:
    from swarm.server.daemon import SwarmDaemon

_log = get_logger("web.app")

TEMPLATES_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"


def _get_daemon(request: web.Request) -> SwarmDaemon:
    return request.app["daemon"]


def _html_to_text(html: str) -> str:
    """Convert HTML email body to readable plain text preserving structure."""
    import html as _html
    import re

    text = html
    # Block elements → newlines
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"</p>", "\n\n", text, flags=re.IGNORECASE)
    text = re.sub(r"</div>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"</tr>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"</li>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<li[^>]*>", "  • ", text, flags=re.IGNORECASE)
    text = re.sub(r"<h[1-6][^>]*>", "\n## ", text, flags=re.IGNORECASE)
    text = re.sub(r"</h[1-6]>", "\n", text, flags=re.IGNORECASE)
    # Strip remaining tags
    text = re.sub(r"<[^>]+>", "", text)
    # Decode HTML entities
    text = _html.unescape(text)
    # Normalize whitespace within lines but preserve newlines
    lines = text.split("\n")
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in lines]
    # Collapse 3+ blank lines → 2
    text = "\n".join(lines)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


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
    all_tasks = daemon.task_board.all_tasks
    completed_ids = {t.id for t in all_tasks if t.status.value == "completed"}
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

    proposals = [
        {
            **d._proposal_dict(p),
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
        "pane_content": pane_content,
        "tasks": _task_dicts(d),
        "task_summary": d.task_board.summary(),
        "entries": _drone_dicts(d),
        "worker_count": len(d.workers),
        "drones_enabled": d.pilot.enabled if d.pilot else False,
        "groups": groups,
        "ws_auth_required": bool(d.config.api_password),
        "proposals": proposals,
        "proposal_count": len(proposals),
        "worker_tasks": worker_tasks,
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
    state_priority = {"STUNG": 0, "WAITING": 1, "BUZZING": 2, "RESTING": 3}
    grouped_names: set[str] = set()
    groups = []

    for g in config_groups:
        members = _collect_group_members(g, worker_map, grouped_names)
        if members:
            worst = min(members, key=lambda w: state_priority.get(w["state"], 9))
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


def _collect_group_members(group, worker_map: dict, grouped_names: set[str]) -> list[dict]:
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
async def handle_partial_workers(request: web.Request) -> dict:
    d = _get_daemon(request)
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
    d = _get_daemon(request)
    workers = d.workers
    total = len(workers)
    if total == 0:
        return web.Response(text="0 workers", content_type="text/html")

    from collections import Counter

    counts = Counter(w.state.value for w in workers)
    parts = []
    state_colors = {
        "BUZZING": "var(--leaf)",
        "WAITING": "var(--honey)",
        "RESTING": "var(--muted)",
        "STUNG": "var(--poppy)",
    }
    for state in ("BUZZING", "WAITING", "RESTING", "STUNG"):
        c = counts.get(state, 0)
        if c > 0:
            color = state_colors.get(state, "var(--muted)")
            parts.append(f'<span style="color:{color};">{c} {state.lower()}</span>')
    breakdown = ", ".join(parts)
    return web.Response(text=f"{total} workers: {breakdown}", content_type="text/html")


@aiohttp_jinja2.template("partials/task_list.html")
async def handle_partial_tasks(request: web.Request) -> dict:
    d = _get_daemon(request)
    tasks = _task_dicts(d)

    # Filter by status
    status_filter = request.query.get("status")
    if status_filter and status_filter != "all":
        # "assigned" filter matches both assigned and in_progress
        if status_filter == "assigned":
            tasks = [t for t in tasks if t["status"] in ("assigned", "in_progress")]
        else:
            tasks = [t for t in tasks if t["status"] == status_filter]

    # Filter by priority
    priority_filter = request.query.get("priority")
    if priority_filter and priority_filter != "all":
        tasks = [t for t in tasks if t["priority"] == priority_filter]

    return {
        "tasks": tasks,
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
    description = data.get("description", "")

    if not title:
        console_log("Generating title from description via Claude...")
        title = await smart_title(description)
        if title:
            console_log(f"Generated title: {title}")
        else:
            console_log("Title generation failed — no title or description", level="warn")
    if not title:
        return web.json_response({"error": "title or description required"}, status=400)

    priority = PRIORITY_MAP.get(data.get("priority", "normal"), TaskPriority.NORMAL)

    type_str = data.get("task_type", "").strip()
    task_type = TYPE_MAP.get(type_str) if type_str else auto_classify_type(title, description)

    deps_raw = data.get("depends_on", "").strip()
    depends_on = [d.strip() for d in deps_raw.split(",") if d.strip()] if deps_raw else None

    # Pre-saved attachment paths (e.g. from email import)
    att_raw = data.get("attachments", "").strip()
    attachments = [a.strip() for a in att_raw.split(",") if a.strip()] if att_raw else None

    source_email_id = data.get("source_email_id", "").strip()

    task = d.create_task(
        title=title,
        description=description,
        priority=priority,
        task_type=task_type,
        depends_on=depends_on,
        attachments=attachments,
        source_email_id=source_email_id,
    )
    console_log(f'Task created: "{title}" ({priority.value}, {task_type.value})')
    return web.json_response({"id": task.id, "title": task.title}, status=201)


async def handle_action_assign_task(request: web.Request) -> web.Response:
    d = _get_daemon(request)
    data = await request.post()
    task_id = data.get("task_id", "")
    worker_name = data.get("worker", "")
    if not task_id or not worker_name:
        return web.json_response({"error": "task_id and worker required"}, status=400)

    try:
        await d.assign_task(task_id, worker_name)
        console_log(f'Task assigned to "{worker_name}"')
    except SwarmOperationError as e:
        return web.json_response({"error": str(e)}, status=404)
    return web.json_response({"status": "assigned", "task_id": task_id, "worker": worker_name})


async def handle_action_complete_task(request: web.Request) -> web.Response:
    d = _get_daemon(request)
    data = await request.post()
    task_id = data.get("task_id", "")
    resolution = data.get("resolution", "").strip()
    if not task_id:
        return web.json_response({"error": "task_id required"}, status=400)

    try:
        d.complete_task(task_id, resolution=resolution)
        console_log(f"Task completed: {task_id[:8]}")
    except SwarmOperationError as e:
        return web.json_response({"error": str(e)}, status=404)
    return web.json_response({"status": "completed", "task_id": task_id})


async def handle_action_ask_queen(request: web.Request) -> web.Response:
    d = _get_daemon(request)
    if not d.queen:
        return web.json_response({"error": "Queen not configured"}, status=400)

    console_log("Queen coordinating hive...")
    try:
        result = await d.coordinate_hive(force=True)
    except Exception as e:
        console_log(f"Queen error: {e}", level="error")
        return web.json_response({"error": str(e)}, status=500)

    n = len(result.get("directives", []))
    console_log(f"Queen done — {n} directive(s)")
    result["cooldown"] = d.queen.cooldown_remaining if d.queen else 0
    return web.json_response(result)


async def handle_action_ask_queen_question(request: web.Request) -> web.Response:
    d = _get_daemon(request)
    data = await request.post()
    question = data.get("question", "").strip()
    if not question:
        return web.json_response({"error": "question required"}, status=400)

    if not d.queen:
        return web.json_response({"error": "Queen not configured"}, status=400)

    console_log(f"Queen asked: {question[:60]}...")
    try:
        hive_ctx = await d.gather_hive_context()
        prompt = (
            f"You are the Queen of a swarm of Claude Code agents.\n\n"
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
        result = await d.queen.ask(prompt, force=True)
    except Exception as e:
        console_log(f"Queen error: {e}", level="error")
        return web.json_response({"error": str(e)}, status=500)

    console_log("Queen answered operator question")
    result["cooldown"] = d.queen.cooldown_remaining if d.queen else 0
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


async def handle_action_reopen_task(request: web.Request) -> web.Response:
    d = _get_daemon(request)
    data = await request.post()
    task_id = data.get("task_id", "")
    if not task_id:
        return web.json_response({"error": "task_id required"}, status=400)

    try:
        d.reopen_task(task_id)
        console_log(f"Task reopened: {task_id[:8]}")
    except SwarmOperationError as e:
        return web.json_response({"error": str(e)}, status=400)
    return web.json_response({"status": "reopened", "task_id": task_id})


async def handle_action_unassign_task(request: web.Request) -> web.Response:
    d = _get_daemon(request)
    data = await request.post()
    task_id = data.get("task_id", "")
    if not task_id:
        return web.json_response({"error": "task_id required"}, status=400)

    try:
        d.unassign_task(task_id)
        console_log(f"Task unassigned: {task_id[:8]}")
    except SwarmOperationError as e:
        return web.json_response({"error": str(e)}, status=404)
    return web.json_response({"status": "unassigned", "task_id": task_id})


# --- A4: Per-worker Queen Analysis ---


async def handle_action_ask_queen_worker(request: web.Request) -> web.Response:
    d = _get_daemon(request)
    name = request.match_info["name"]

    if not d.queen:
        return web.json_response({"error": "Queen not configured"}, status=400)

    console_log(f'Queen analyzing "{name}"...')
    try:
        result = await d.analyze_worker(name, force=True)
    except WorkerNotFoundError:
        return web.Response(status=404)
    except Exception as e:
        console_log(f"Queen error: {e}", level="error")
        return web.json_response({"error": str(e)}, status=500)

    console_log(f'Queen analysis of "{name}" complete')
    result["cooldown"] = d.queen.cooldown_remaining if d.queen else 0
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


async def handle_action_fetch_outlook_email(request: web.Request) -> web.Response:
    """Fetch an email from Microsoft Graph API using a message ID."""
    d = _get_daemon(request)
    data = await request.post()
    message_id = data.get("message_id", "").strip()
    if not message_id:
        return web.json_response({"error": "message_id required"}, status=400)

    # Check if Graph API is configured and connected
    if not d.graph_mgr:
        return web.json_response({"error": "Microsoft Graph not configured"})

    graph_token = await d.graph_mgr.get_token()
    if not graph_token:
        return web.json_response({"error": "Microsoft Graph not connected — authenticate first"})

    console_log(f"Fetching email via Graph: {message_id[:30]}...")
    return await _fetch_graph_email(d, message_id, graph_token)


async def _translate_exchange_id(sess, headers: dict, ews_id: str) -> str | None:
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


async def _fetch_attachment_bytes(sess, headers, msg_id, att_id, quote, yarl) -> str:
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


async def _save_graph_attachments(d, sess, headers, msg_id, attachments):
    """Save Graph API file attachments to disk, fetching content individually if needed."""
    import base64
    from urllib.parse import quote

    import yarl

    _log_parts = []
    for a in attachments[:5]:
        _log_parts.append(
            f"{a.get('name', '?')} ({a.get('@odata.type', '?')}, "
            f"inline={a.get('isInline')}, "
            f"bytes={'yes' if a.get('contentBytes') else 'no'}, "
            f"size={a.get('size', '?')})"
        )
    console_log(
        f"Graph email: {len(attachments)} attachment(s)"
        + (", " + ", ".join(_log_parts) if _log_parts else "")
    )

    paths: list[str] = []
    for att in attachments:
        if att.get("@odata.type") != "#microsoft.graph.fileAttachment":
            continue
        name = att.get("name", "attachment")
        raw_b64 = att.get("contentBytes", "")
        if not raw_b64:
            att_id = att.get("id")
            if att_id:
                console_log(f"Fetching attachment '{name}' individually...")
                raw_b64 = await _fetch_attachment_bytes(sess, headers, msg_id, att_id, quote, yarl)
        if raw_b64:
            content_bytes = base64.b64decode(raw_b64)
            if content_bytes:
                paths.append(d.save_attachment(name, content_bytes))
    return paths


async def _fetch_graph_email(d, message_id: str, token: str):
    """Fetch email + attachments from Microsoft Graph API."""
    import aiohttp as _aiohttp
    from urllib.parse import quote

    import yarl

    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    base = "https://graph.microsoft.com/v1.0/me/messages"

    async def _graph_get(sess, mid):
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
                return web.json_response({"error": f"Graph API {status}: {body[:200]}"})

            msg = _json.loads(body)
            body_content = msg.get("body", {}).get("content", "")
            body_type = msg.get("body", {}).get("contentType", "text")

            subject = msg.get("subject", "")
            if body_type.lower() == "html":
                body_text = _html_to_text(body_content)
            else:
                body_text = body_content.strip()

            # Prepend subject as context header in description
            if subject:
                description = f"Subject: {subject}\n\n{body_text}"
            else:
                description = body_text

            attachment_paths = await _save_graph_attachments(
                d, sess, headers, effective_id, msg.get("attachments", [])
            )
    except Exception as exc:
        return web.json_response({"error": str(exc)[:200]})

    # Auto-generate a concise title from the description
    from swarm.tasks.task import auto_classify_type, smart_title

    title = await smart_title(description)
    if not title:
        title = subject  # fall back to raw subject

    # Auto-classify task type
    task_type = auto_classify_type(title, description)

    return web.json_response(
        {
            "title": title,
            "description": description,
            "task_type": task_type.value,
            "attachments": attachment_paths,
            "message_id": effective_id,
        }
    )


async def handle_action_fetch_image(request: web.Request) -> web.Response:
    """Fetch an external image URL and save it as an attachment."""
    import aiohttp as _aiohttp

    d = _get_daemon(request)
    data = await request.post()
    url = data.get("url", "").strip()
    if not url:
        return web.json_response({"error": "url required"}, status=400)

    # Skip blob: URLs (in-memory, can't be fetched externally)
    if url.startswith("blob:"):
        return web.json_response({"error": "blob: URLs cannot be fetched"}, status=422)

    # Only allow http/https/file URLs
    if not url.startswith(("http://", "https://", "file:///")):
        return web.json_response({"error": "unsupported URL scheme"}, status=400)

    try:
        if url.startswith("file:///"):
            # Local file (e.g. Outlook temp path accessible via WSL)
            from pathlib import Path as _Path
            from urllib.parse import unquote as _unquote

            local_path = _unquote(url[8:])  # strip file:///
            # Convert Windows path to WSL if needed
            if len(local_path) > 1 and local_path[1] == ":":
                drive = local_path[0].lower()
                local_path = f"/mnt/{drive}{local_path[2:].replace(chr(92), '/')}"
            fp = _Path(local_path)
            if not fp.exists():
                return web.json_response({"error": "file not found"}, status=404)
            img_data = fp.read_bytes()
            filename = fp.name
        else:
            async with _aiohttp.ClientSession() as sess:
                async with sess.get(url, timeout=_aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status != 200:
                        return web.json_response({"error": f"HTTP {resp.status}"}, status=502)
                    img_data = await resp.read()
                    # Derive filename from URL
                    filename = url.split("/")[-1].split("?")[0] or "image.png"
    except Exception as exc:
        return web.json_response({"error": str(exc)[:200]}, status=502)

    if not img_data:
        return web.json_response({"error": "empty response"}, status=502)

    path = d.save_attachment(filename, img_data)
    return web.json_response({"path": path}, status=201)


async def handle_action_upload(request: web.Request) -> web.Response:
    """Upload a file and return its absolute server path."""
    d = _get_daemon(request)
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
        return web.json_response({"error": "file required"}, status=400)

    path = d.save_attachment(file_name, file_data)
    console_log(f"File uploaded: {file_name} → {path}")
    return web.json_response({"path": path}, status=201)


async def handle_partial_task_history(request: web.Request) -> web.Response:
    """Return task history events as HTML for inline display."""
    d = _get_daemon(request)
    task_id = request.match_info["task_id"]
    events = d.task_history.get_events(task_id, limit=50)
    if not events:
        no_hist = '<div style="color:var(--muted);font-size:0.8rem;padding:0.5rem;">'
        no_hist += "No history</div>"
        return web.Response(text=no_hist, content_type="text/html")

    from markupsafe import escape

    html = '<div style="padding:0.25rem 0.5rem;">'
    for ev in events:
        color = {
            "CREATED": "var(--leaf)",
            "ASSIGNED": "var(--lavender)",
            "COMPLETED": "var(--leaf)",
            "FAILED": "var(--poppy)",
            "REMOVED": "var(--poppy)",
            "EDITED": "var(--honey)",
        }.get(ev.action.value, "var(--muted)")
        html += (
            f'<div style="font-size:0.75rem;padding:0.15rem 0;display:flex;gap:0.5rem;">'
            f'<span style="color:var(--muted);min-width:120px;">{escape(ev.formatted_time)}</span>'
            f'<span style="color:{color};min-width:70px;font-weight:bold;">'
            f"{escape(ev.action.value)}</span>"
            f'<span style="color:var(--muted);">{escape(ev.actor)}</span>'
        )
        if ev.detail:
            html += f'<span style="color:var(--beeswax);">{escape(ev.detail)}</span>'
        html += "</div>"
    html += "</div>"
    return web.Response(text=html, content_type="text/html")


async def handle_action_retry_draft(request: web.Request) -> web.Response:
    d = _get_daemon(request)
    data = await request.post()
    task_id = data.get("task_id", "")
    if not task_id:
        return web.json_response({"error": "task_id required"}, status=400)

    try:
        await d.retry_draft_reply(task_id)
        console_log(f"Retrying draft reply for task {task_id[:8]}")
    except SwarmOperationError as e:
        return web.json_response({"error": str(e)}, status=400)
    return web.json_response({"status": "retrying", "task_id": task_id})


async def handle_action_approve_proposal(request: web.Request) -> web.Response:
    d = _get_daemon(request)
    data = await request.post()
    proposal_id = data.get("proposal_id", "")
    if not proposal_id:
        return web.json_response({"error": "proposal_id required"}, status=400)
    draft_response = data.get("draft_response") == "true"
    try:
        await d.approve_proposal(proposal_id, draft_response=draft_response)
        console_log(f"Proposal approved: {proposal_id[:8]}")
    except SwarmOperationError as e:
        return web.json_response({"error": str(e)}, status=404)
    return web.json_response({"status": "approved", "proposal_id": proposal_id})


async def handle_action_reject_proposal(request: web.Request) -> web.Response:
    d = _get_daemon(request)
    data = await request.post()
    proposal_id = data.get("proposal_id", "")
    if not proposal_id:
        return web.json_response({"error": "proposal_id required"}, status=400)
    try:
        d.reject_proposal(proposal_id)
        console_log(f"Proposal rejected: {proposal_id[:8]}")
    except SwarmOperationError as e:
        return web.json_response({"error": str(e)}, status=404)
    return web.json_response({"status": "rejected", "proposal_id": proposal_id})


async def handle_action_reject_all_proposals(request: web.Request) -> web.Response:
    d = _get_daemon(request)
    count = d.reject_all_proposals()
    console_log(f"All proposals rejected ({count})")
    return web.json_response({"status": "rejected_all", "count": count})


async def handle_action_stop_server(request: web.Request) -> web.Response:
    """Trigger graceful shutdown of the web server."""
    console_log("Web server stopping...")
    shutdown_event = request.app.get("shutdown_event")
    if shutdown_event:
        shutdown_event.set()
        return web.json_response({"status": "stopping"})
    return web.json_response({"error": "no shutdown event configured"}, status=500)


# --- Microsoft Graph OAuth ---


async def handle_graph_login(request: web.Request) -> web.Response:
    """Start OAuth flow: generate PKCE, redirect to Microsoft."""
    import secrets as _secrets

    d = _get_daemon(request)
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
    d = _get_daemon(request)
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
    d = _get_daemon(request)
    if not d.graph_mgr:
        return web.json_response({"connected": False, "configured": False})
    return web.json_response({"connected": d.graph_mgr.is_connected(), "configured": True})


async def handle_graph_disconnect(request: web.Request) -> web.Response:
    """Disconnect Microsoft Graph (delete tokens)."""
    d = _get_daemon(request)
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
    tail = all_lines[-lines_count:]
    return web.Response(text="\n".join(tail), content_type="text/plain")


async def handle_action_clear_logs(request: web.Request) -> web.Response:
    """Truncate ~/.swarm/swarm.log."""
    log_path = Path.home() / ".swarm" / "swarm.log"
    try:
        log_path.write_text("")
        console_log("Log file cleared")
    except OSError as e:
        return web.json_response({"error": str(e)}, status=500)
    return web.json_response({"status": "cleared"})


async def handle_bee_icon(request: web.Request) -> web.Response:
    """Serve the bee icon SVG with caching."""
    svg_path = STATIC_DIR / "bee-icon.svg"
    return web.FileResponse(
        svg_path,
        headers={"Cache-Control": "public, max-age=86400"},
    )


async def handle_manifest(request: web.Request) -> web.Response:
    """PWA manifest for add-to-homescreen support."""
    manifest = {
        "name": "Swarm's Bee Hive",
        "short_name": "Bee Hive",
        "start_url": "/",
        "display": "standalone",
        "background_color": "#2A1B0E",
        "theme_color": "#D8A03D",
        "icons": [
            {
                "src": "/bee-icon.svg",
                "sizes": "any",
                "type": "image/svg+xml",
            }
        ],
    }
    return web.json_response(manifest)


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
    app.router.add_get("/manifest.json", handle_manifest)
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
    # Microsoft Graph OAuth
    app.router.add_get("/auth/graph/login", handle_graph_login)
    app.router.add_get("/auth/graph/callback", handle_graph_callback)
    app.router.add_get("/auth/graph/status", handle_graph_status)
    app.router.add_post("/auth/graph/disconnect", handle_graph_disconnect)

    app.router.add_static("/static", STATIC_DIR)
    app.router.add_get("/bee-icon.svg", handle_bee_icon)
