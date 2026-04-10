"""MCP tool definitions for the Swarm server.

Each tool is a dict conforming to MCP's Tool schema:
  {name, description, inputSchema: {type, properties, required}}

Handler functions take (daemon, worker_name, arguments) and return content.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from swarm.server.daemon import SwarmDaemon

# ---------------------------------------------------------------------------
# Tool definitions (MCP schema format)
# ---------------------------------------------------------------------------

TOOLS: list[dict[str, Any]] = [
    {
        "name": "swarm_check_messages",
        "description": (
            "Check for pending messages from other workers or the operator. "
            "Call at the start of each task, after completing a task, and when "
            "you encounter unexpected changes."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "swarm_send_message",
        "description": (
            "Send a message to another worker. Use for sharing discoveries, "
            "dependency warnings, or status updates."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "to": {
                    "type": "string",
                    "description": "Recipient worker name, or '*' for broadcast",
                },
                "type": {
                    "type": "string",
                    "enum": ["finding", "warning", "dependency", "status"],
                    "description": "Message type",
                },
                "content": {
                    "type": "string",
                    "description": "Message content",
                },
            },
            "required": ["to", "type", "content"],
        },
    },
    {
        "name": "swarm_task_status",
        "description": "Query the Swarm task board for current task assignments and status.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "filter": {
                    "type": "string",
                    "enum": ["all", "pending", "assigned", "mine"],
                    "description": "Filter tasks (default: all)",
                },
            },
        },
    },
    {
        "name": "swarm_claim_file",
        "description": (
            "Claim an advisory lock on a file before editing. "
            "Other workers will see the claim and avoid concurrent edits."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Absolute file path to claim",
                },
            },
            "required": ["path"],
        },
    },
    {
        "name": "swarm_complete_task",
        "description": "Mark your assigned task as completed with a resolution summary.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "resolution": {
                    "type": "string",
                    "description": "What was done to complete the task",
                },
            },
            "required": ["resolution"],
        },
    },
    {
        "name": "swarm_create_task",
        "description": "Create a new task on the Swarm task board.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Task title"},
                "description": {
                    "type": "string",
                    "description": "Task description",
                },
                "target_worker": {
                    "type": "string",
                    "description": "Worker to assign to (optional)",
                },
                "priority": {
                    "type": "string",
                    "enum": ["low", "normal", "high", "urgent"],
                },
                "attachments": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Absolute file paths to attach (e.g. screenshots)",
                },
            },
            "required": ["title"],
        },
    },
    {
        "name": "swarm_get_learnings",
        "description": (
            "Query learnings from previously completed tasks. "
            "Useful for understanding patterns and avoiding repeated mistakes."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search term to filter learnings (optional)",
                },
            },
        },
    },
    {
        "name": "swarm_report_progress",
        "description": "Report structured progress on your current task.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "phase": {
                    "type": "string",
                    "description": "Current phase (e.g., 'reading', 'implementing')",
                },
                "pct": {
                    "type": "number",
                    "description": "Estimated completion percentage (0-100)",
                },
                "blockers": {
                    "type": "string",
                    "description": "Current blockers, if any",
                },
            },
        },
    },
]

# Tool name → handler function mapping
_TOOL_NAMES = {t["name"] for t in TOOLS}


# ---------------------------------------------------------------------------
# Tool handlers
# ---------------------------------------------------------------------------


def handle_tool_call(
    daemon: SwarmDaemon,
    worker_name: str,
    tool_name: str,
    arguments: dict[str, Any],
) -> list[dict[str, Any]]:
    """Dispatch a tool call and return MCP content blocks."""
    from swarm.drones.log import LogCategory, SystemAction

    handler = _HANDLERS.get(tool_name)
    if not handler:
        return [{"type": "text", "text": f"Unknown tool: {tool_name}"}]
    try:
        result = handler(daemon, worker_name, arguments)
        # Log to buzz log so MCP activity is visible in the dashboard
        short_name = tool_name.removeprefix("swarm_")
        detail = result[0].get("text", "")[:120] if result else ""
        daemon.drone_log.add(
            SystemAction.OPERATOR,
            worker_name,
            f"mcp:{short_name} → {detail}",
            category=LogCategory.WORKER,
        )
        return result
    except Exception as e:
        return [{"type": "text", "text": f"Error: {e}"}]


def _handle_check_messages(
    d: SwarmDaemon, worker_name: str, _args: dict[str, Any]
) -> list[dict[str, Any]]:
    messages = d.message_store.get_unread(worker_name)
    if not messages:
        return [{"type": "text", "text": "No pending messages."}]
    # Mark as read
    d.message_store.mark_read(worker_name, [m.id for m in messages])
    lines = []
    for m in messages:
        lines.append(f"[{m.msg_type}] from {m.sender}: {m.content}")
    return [{"type": "text", "text": "\n".join(lines)}]


def _handle_send_message(
    d: SwarmDaemon, worker_name: str, args: dict[str, Any]
) -> list[dict[str, Any]]:
    recipient = args.get("to", "")
    msg_type = args.get("type", "finding")
    content = args.get("content", "")
    if not recipient or not content:
        return [{"type": "text", "text": "Missing 'to' or 'content'"}]
    msg_id = d.message_store.send(worker_name, recipient, msg_type, content)
    if msg_id:
        from swarm.drones.log import LogCategory, SystemAction

        d.drone_log.add(
            SystemAction.OPERATOR,
            worker_name,
            f"→ {recipient}: {content[:80]}",
            category=LogCategory.MESSAGE,
        )
        return [{"type": "text", "text": f"Message sent to {recipient}."}]
    return [{"type": "text", "text": "Failed to send message."}]


def _handle_task_status(
    d: SwarmDaemon, worker_name: str, args: dict[str, Any]
) -> list[dict[str, Any]]:
    if not d.task_board:
        return [{"type": "text", "text": "No task board available."}]
    filt = args.get("filter", "all")
    tasks = d.task_board.all_tasks
    if filt == "pending":
        tasks = [t for t in tasks if t.status.value == "pending"]
    elif filt == "assigned":
        tasks = [t for t in tasks if t.assigned_worker is not None]
    elif filt == "mine":
        tasks = [t for t in tasks if t.assigned_worker == worker_name]
    lines = []
    for t in tasks[:20]:
        w = t.assigned_worker or "unassigned"
        lines.append(f"#{t.number} [{t.status.value}] {t.title} ({w})")
    if not lines:
        return [{"type": "text", "text": "No tasks found."}]
    return [{"type": "text", "text": "\n".join(lines)}]


def _handle_claim_file(
    d: SwarmDaemon, worker_name: str, args: dict[str, Any]
) -> list[dict[str, Any]]:
    import os
    import time

    path = args.get("path", "")
    if not path:
        return [{"type": "text", "text": "Missing 'path'"}]
    resolved = os.path.realpath(path)
    now = time.time()
    lock = d.file_locks.get(resolved)
    if lock:
        owner, ts = lock
        if owner != worker_name and (now - ts) < d._file_lock_ttl:
            return [{"type": "text", "text": f"File claimed by {owner}."}]
    d.file_locks[resolved] = (worker_name, now)
    return [{"type": "text", "text": f"File claimed: {path}"}]


def _handle_complete_task(
    d: SwarmDaemon, worker_name: str, args: dict[str, Any]
) -> list[dict[str, Any]]:
    resolution = args.get("resolution", "")
    # Find worker's assigned task
    if not d.task_board:
        return [{"type": "text", "text": "No task board."}]
    for task in d.task_board.all_tasks:
        if task.assigned_worker == worker_name and task.status.value in (
            "assigned",
            "in_progress",
        ):
            d.complete_task(task.id, actor=worker_name, resolution=resolution)
            return [{"type": "text", "text": f"Task #{task.number} completed."}]
    return [{"type": "text", "text": "No active task found."}]


def _handle_create_task(
    d: SwarmDaemon, worker_name: str, args: dict[str, Any]
) -> list[dict[str, Any]]:
    title = args.get("title", "")
    if not title:
        return [{"type": "text", "text": "Missing 'title'"}]
    attachments = args.get("attachments") or None
    if attachments:
        from pathlib import Path

        validated: list[str] = []
        for p in attachments:
            rp = Path(p).resolve()
            if not rp.exists():
                return [{"type": "text", "text": f"Attachment not found: {p}"}]
            validated.append(str(rp))
        attachments = validated
    task = d.create_task(
        title=title,
        description=args.get("description", ""),
        attachments=attachments,
        actor=worker_name,
    )
    target = args.get("target_worker")
    if target:
        import asyncio

        try:
            loop = asyncio.get_running_loop()
            _task = loop.create_task(d.assign_task(task.id, target, actor=worker_name))
            _task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)
        except RuntimeError:
            d.task_board.assign(task.id, target)
    return [{"type": "text", "text": f"Task created: #{task.number} {title}"}]


def _handle_get_learnings(
    d: SwarmDaemon, worker_name: str, args: dict[str, Any]
) -> list[dict[str, Any]]:
    if not d.task_board:
        return [{"type": "text", "text": "No task board."}]
    query = args.get("query", "").lower()
    results = []
    for t in d.task_board.all_tasks:
        if not t.learnings:
            continue
        if query and query not in t.title.lower() and query not in t.learnings.lower():
            continue
        results.append(f"Task #{t.number} ({t.title}):\n{t.learnings}")
    if not results:
        return [{"type": "text", "text": "No learnings found."}]
    return [{"type": "text", "text": "\n---\n".join(results[:5])}]


def _handle_report_progress(
    d: SwarmDaemon, worker_name: str, args: dict[str, Any]
) -> list[dict[str, Any]]:
    phase = args.get("phase", "")
    pct = args.get("pct", -1)
    blockers = args.get("blockers", "")
    parts = []
    if phase:
        parts.append(f"phase={phase}")
    if pct >= 0:
        parts.append(f"{pct}%")
    if blockers:
        parts.append(f"blockers: {blockers}")
    summary = ", ".join(parts) if parts else "progress update"
    from swarm.drones.log import LogCategory, SystemAction

    d.drone_log.add(
        SystemAction.OPERATOR,
        worker_name,
        f"progress: {summary}",
        category=LogCategory.WORKER,
    )
    d.broadcast_ws(
        {
            "type": "worker_progress",
            "worker": worker_name,
            "phase": phase,
            "pct": pct,
            "blockers": blockers,
        }
    )
    return [{"type": "text", "text": "Progress reported."}]


_HANDLERS = {
    "swarm_check_messages": _handle_check_messages,
    "swarm_send_message": _handle_send_message,
    "swarm_task_status": _handle_task_status,
    "swarm_claim_file": _handle_claim_file,
    "swarm_complete_task": _handle_complete_task,
    "swarm_create_task": _handle_create_task,
    "swarm_get_learnings": _handle_get_learnings,
    "swarm_report_progress": _handle_report_progress,
}
