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
            "Check the Swarm inbox for pending messages from other workers or the operator. "
            "Call this at three moments: (1) at the start of every task so you don't miss "
            "dependency warnings or operator hints, (2) after completing a task so downstream "
            "workers' replies don't stack up, and (3) whenever you encounter unexpected state "
            "(files changed under you, tests failing that passed last run) — another worker "
            "may have sent a 'warning' or 'finding' that explains it. Messages are marked read "
            "on retrieval, so don't call speculatively."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {},
            "examples": [{}],
        },
    },
    {
        "name": "swarm_send_message",
        "description": (
            "Send a direct message to another worker (or broadcast to '*'). Use this whenever "
            "you learn something that affects another worker's ability to do their job "
            "correctly. Message types:\n"
            "  - 'finding'    — a discovery that might be useful (schema shape, gotcha, pattern)\n"
            "  - 'warning'    — you are about to change something that will break their build\n"
            "  - 'dependency' — they need to do X before you can finish Y (blocks your task)\n"
            "  - 'status'     — routine progress update, not action-required\n"
            "Prefer direct messages over '*' broadcast — broadcast only for changes that "
            "truly affect every worker (e.g., a shared type signature changed)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "to": {
                    "type": "string",
                    "description": (
                        "Recipient worker name (e.g. 'hub', 'platform'), or '*' for "
                        "broadcast to all workers."
                    ),
                },
                "type": {
                    "type": "string",
                    "enum": ["finding", "warning", "dependency", "status"],
                    "description": "Message type — see tool description for semantics.",
                },
                "content": {
                    "type": "string",
                    "description": (
                        "The message body. Be concrete: include file paths, function "
                        "names, and any action the recipient needs to take."
                    ),
                },
            },
            "required": ["to", "type", "content"],
            "examples": [
                {
                    "to": "platform",
                    "type": "warning",
                    "content": (
                        "Renamed ContactDto.emailAddress → ContactDto.email in hub "
                        "PR #321; please update your imports."
                    ),
                },
                {
                    "to": "*",
                    "type": "finding",
                    "content": (
                        "The /api/v1/contacts endpoint now requires X-Tenant-Id "
                        "header as of platform commit abc123."
                    ),
                },
            ],
        },
    },
    {
        "name": "swarm_task_status",
        "description": (
            "Query the Swarm task board. Call this when you need to see what work is queued, "
            "who owns what, or to check whether a task you created has been picked up yet. "
            "Use filter='mine' to list only your own tasks, 'pending' to find unclaimed work, "
            "'assigned' for anything with an owner, or omit filter for everything. Open tasks "
            "(proposed/pending/assigned/in_progress) come first, newest-by-number first; "
            "completed/failed tasks sort after, most-recently-completed first. Results are "
            "capped at ``limit`` (default 50, max 500); when output is truncated a summary "
            "footer names the total. For ``filter='mine'``, completed history is suppressed "
            "unless ``include_completed`` is true — the default surfaces your actionable work "
            "rather than bury it behind old closeouts. Pass ``number`` to look up a single task "
            "by its display number (bypasses all other filters)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "filter": {
                    "type": "string",
                    "enum": ["all", "pending", "assigned", "mine"],
                    "description": "Which tasks to return (default: 'all').",
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 500,
                    "description": "Maximum rows to return (default 50, max 500).",
                },
                "include_completed": {
                    "type": "boolean",
                    "description": (
                        "Include completed/failed tasks when filter='mine'. "
                        "Default false (open tasks only). Ignored for other filters."
                    ),
                },
                "number": {
                    "type": "integer",
                    "minimum": 1,
                    "description": (
                        "Look up a single task by its display number "
                        "(e.g. 142). Overrides filter/limit."
                    ),
                },
            },
            "examples": [
                {"filter": "mine"},
                {"filter": "mine", "include_completed": True},
                {"filter": "pending", "limit": 100},
                {"number": 142},
                {},
            ],
        },
    },
    {
        "name": "swarm_claim_file",
        "description": (
            "Place an advisory lock on a file before editing it, so other workers can see "
            "the claim and avoid concurrent edits. Call this right before you start editing "
            "any shared file — config files (package.json, pyproject.toml), shared utilities, "
            "API contracts, shared types. Claims auto-expire so you don't need to release; "
            "a fresh claim renews the timer. Path MUST be absolute (the daemon will reject "
            "relative paths). If another worker holds the claim, the tool returns an error "
            "naming them — ask them via swarm_send_message rather than forcing."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": (
                        "Absolute filesystem path to claim. Use realpath output — "
                        "symlinks are resolved server-side."
                    ),
                },
            },
            "required": ["path"],
            "examples": [
                {"path": "/home/user/projects/repo/src/shared/types.ts"},
                {"path": "/home/user/projects/repo/pyproject.toml"},
            ],
        },
    },
    {
        "name": "swarm_complete_task",
        "description": (
            "Mark your currently-assigned task as completed. Call this only after you have "
            "verified your work (tests pass, /check clean, feature demonstrably works). The "
            "resolution is stored as task learnings and shown to future workers picking up "
            "similar tasks — write it for *them*, not for a manager. A good resolution names "
            "the root cause (for bugs), the files you touched, and any followup work you "
            "spotted but didn't do. Fails if you have no active task assignment."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "resolution": {
                    "type": "string",
                    "description": (
                        "What was done. Name files touched, root cause for bugs, "
                        "and any followup worth flagging."
                    ),
                },
            },
            "required": ["resolution"],
            "examples": [
                {
                    "resolution": (
                        "Fixed null pointer in ContactService.resolveTenant "
                        "(src/services/contact.ts:142) — missing guard for anonymous "
                        "sessions. Added regression test. Followup: refactor tenant "
                        "resolution out of service constructor (noted but not done)."
                    ),
                },
            ],
        },
    },
    {
        "name": "swarm_create_task",
        "description": (
            "File a new task on the Swarm task board. Use this when you discover work that "
            "needs doing but shouldn't block your current task — a bug in another module, "
            "a refactor opportunity, a followup from a fix, a cross-project change another "
            "worker owns. Set target_worker to route cross-project work (see the worker name "
            "table in CLAUDE.md). Priority defaults to 'normal'; use 'urgent' only for "
            "production-impacting issues. Attachments must be absolute paths to existing "
            "files (typically screenshots captured during debugging)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "title": {
                    "type": "string",
                    "description": (
                        "Short imperative title (e.g. 'Fix tenant resolution in "
                        "anonymous sessions')."
                    ),
                },
                "description": {
                    "type": "string",
                    "description": (
                        "What needs doing and why. Include repro steps for bugs, "
                        "acceptance criteria for features."
                    ),
                },
                "target_worker": {
                    "type": "string",
                    "description": (
                        "Worker name to assign to (e.g. 'hub', 'platform', "
                        "'project-root'). Omit to leave unassigned."
                    ),
                },
                "priority": {
                    "type": "string",
                    "enum": ["low", "normal", "high", "urgent"],
                    "description": "'urgent' only for production-impacting issues.",
                },
                "attachments": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Absolute paths to existing files (typically screenshots).",
                },
            },
            "required": ["title"],
            "examples": [
                {
                    "title": "Remove dead feature flag FEATURE_X_ENABLED",
                    "description": (
                        "Flag has been 100% rolled out for 4 weeks. Remove from "
                        "config.ts and all call sites."
                    ),
                    "priority": "low",
                },
                {
                    "title": "Nexus: emails over 1MB fail to ingest",
                    "description": (
                        "Reproduced with attached sample. Root cause likely "
                        "base64 buffer in MailParser. Repro: POST "
                        "/api/v1/nexus/ingest with the attached eml."
                    ),
                    "target_worker": "nexus",
                    "priority": "high",
                    "attachments": ["/home/user/bug-evidence/large-email.eml"],
                },
            ],
        },
    },
    {
        "name": "swarm_get_learnings",
        "description": (
            "Search learnings captured from previously-completed tasks. Call this when you "
            "start a task that sounds similar to something already done, or when you hit "
            "an unfamiliar error — another worker may have documented the fix. Results are "
            "capped at 5, so pass a specific query (function name, error message, file path) "
            "rather than a broad topic. If you find relevant learnings, cite them in your "
            "own resolution so the knowledge compounds."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "Substring to filter learnings (case-insensitive). Omit "
                        "to return all (capped at 5)."
                    ),
                },
            },
            "examples": [
                {"query": "tenant resolution"},
                {"query": "MailParser"},
                {},
            ],
        },
    },
    {
        "name": "swarm_batch",
        "description": (
            "Execute multiple swarm_* calls in a single round-trip. Use this "
            "when you're about to make two or more back-to-back swarm calls — "
            "e.g. claim_file + send_message + complete_task after a fix — so "
            "you pay one JSON-RPC round-trip instead of N. Ops execute "
            "sequentially in the order given (message ordering matters) and "
            "the combined result lists each op's outcome. Pass "
            "``fail_fast: true`` (default) to stop at the first error, or "
            "``fail_fast: false`` to continue and report each error inline. "
            "Cannot contain nested swarm_batch calls."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "ops": {
                    "type": "array",
                    "description": (
                        "Ordered list of ``{tool, args}`` pairs to execute. "
                        "``tool`` must name one of the other swarm_* tools."
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "tool": {"type": "string"},
                            "args": {"type": "object"},
                        },
                        "required": ["tool"],
                    },
                },
                "fail_fast": {
                    "type": "boolean",
                    "description": (
                        "If true (default), abort the batch on the first error. "
                        "If false, run every op and surface each error inline."
                    ),
                },
            },
            "required": ["ops"],
            "examples": [
                {
                    "ops": [
                        {
                            "tool": "swarm_claim_file",
                            "args": {"path": "/home/user/repo/src/shared.ts"},
                        },
                        {
                            "tool": "swarm_send_message",
                            "args": {
                                "to": "platform",
                                "type": "warning",
                                "content": "About to edit shared.ts",
                            },
                        },
                        {
                            "tool": "swarm_complete_task",
                            "args": {"resolution": "Edited shared.ts"},
                        },
                    ]
                },
                {
                    "ops": [
                        {"tool": "swarm_check_messages", "args": {}},
                        {"tool": "swarm_task_status", "args": {"filter": "mine"}},
                    ],
                    "fail_fast": False,
                },
            ],
        },
    },
    {
        "name": "swarm_report_progress",
        "description": (
            "Report structured progress on your current task. The operator sees these in the "
            "dashboard and uses them to decide when to intervene. Call this at meaningful "
            "milestones — finished reading, starting implementation, test passing, hit a "
            "blocker — not on every trivial step. If you're blocked (waiting on another "
            "worker, missing credentials, flaky test), set blockers to the specific thing "
            "that would unblock you so the operator can act."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "phase": {
                    "type": "string",
                    "description": (
                        "Current phase label. Conventional values: 'reading', "
                        "'planning', 'implementing', 'testing', 'debugging', "
                        "'shipping'."
                    ),
                },
                "pct": {
                    "type": "number",
                    "description": (
                        "Estimated completion percentage 0-100. Be honest — "
                        "overestimates frustrate the operator."
                    ),
                },
                "blockers": {
                    "type": "string",
                    "description": "Specific blocker, if any. Empty string when making progress.",
                },
            },
            "examples": [
                {"phase": "implementing", "pct": 40},
                {
                    "phase": "debugging",
                    "pct": 60,
                    "blockers": "Waiting on platform worker to deploy schema change from PR #87.",
                },
                {"phase": "shipping", "pct": 95},
            ],
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


_TASK_STATUS_DEFAULT_LIMIT = 50
_TASK_STATUS_MAX_LIMIT = 500
_OPEN_STATUSES = {"proposed", "pending", "assigned", "in_progress"}


def _format_task_line(t: Any) -> str:
    w = t.assigned_worker or "unassigned"
    return f"#{t.number} [{t.status.value}] {t.title} ({w})"


def _sort_tasks_for_display(tasks: list[Any]) -> list[Any]:
    """Open tasks first (newest-by-number DESC), then completed/failed by
    completed_at DESC (falling back to number DESC). Older implementations
    sorted ASC and sliced the head, which hid newer assignments — see task
    #142."""

    def key(t: Any) -> tuple[int, float, int]:
        is_open = t.status.value in _OPEN_STATUSES
        # Primary: open first (0) vs closed (1).
        # Secondary: most recent first — completed_at for closed tasks,
        # or number for open ones (a proxy for recency without requiring
        # a db timestamp).
        recency = -(t.completed_at or 0.0) if not is_open else -float(t.number)
        return (0 if is_open else 1, recency, -t.number)

    return sorted(tasks, key=key)


def _lookup_task_by_number(d: SwarmDaemon, raw: Any) -> list[dict[str, Any]]:
    try:
        target = int(raw)
    except (TypeError, ValueError):
        return [{"type": "text", "text": f"Invalid 'number': {raw!r}"}]
    for t in d.task_board.all_tasks:
        if t.number == target:
            return [{"type": "text", "text": _format_task_line(t)}]
    return [{"type": "text", "text": f"No task found with number #{target}."}]


def _coerce_limit(raw: Any) -> int | str:
    """Return a clamped integer limit or a user-facing error string."""
    try:
        limit = int(raw)
    except (TypeError, ValueError):
        return f"Invalid 'limit': {raw!r}"
    if limit < 1:
        return "'limit' must be >= 1."
    return min(limit, _TASK_STATUS_MAX_LIMIT)


def _apply_task_filter(
    tasks: list[Any], filt: str, worker_name: str, *, include_completed: bool
) -> list[Any]:
    if filt == "pending":
        return [t for t in tasks if t.status.value == "pending"]
    if filt == "assigned":
        return [t for t in tasks if t.assigned_worker is not None]
    if filt == "mine":
        mine = [t for t in tasks if t.assigned_worker == worker_name]
        # Default for 'mine' surfaces actionable work. Completed/failed rows
        # used to crowd out newer assignments from the old fixed 20-row
        # window (task #142). Opt back in with include_completed=True.
        if not include_completed:
            mine = [t for t in mine if t.status.value in _OPEN_STATUSES]
        return mine
    return tasks


def _handle_task_status(
    d: SwarmDaemon, worker_name: str, args: dict[str, Any]
) -> list[dict[str, Any]]:
    if not d.task_board:
        return [{"type": "text", "text": "No task board available."}]

    # Single-task lookup by display number — bypasses filter/limit so a worker
    # that hears about task #142 from another channel can always pull it up.
    if (number := args.get("number")) is not None:
        return _lookup_task_by_number(d, number)

    limit = _coerce_limit(args.get("limit", _TASK_STATUS_DEFAULT_LIMIT))
    if isinstance(limit, str):
        return [{"type": "text", "text": limit}]

    tasks = _apply_task_filter(
        list(d.task_board.all_tasks),
        args.get("filter", "all"),
        worker_name,
        include_completed=bool(args.get("include_completed", False)),
    )
    total = len(tasks)
    shown = _sort_tasks_for_display(tasks)[:limit]
    if not shown:
        return [{"type": "text", "text": "No tasks found."}]

    lines = [_format_task_line(t) for t in shown]
    if total > len(shown):
        lines.append(
            f"\n… {total - len(shown)} more not shown "
            f"(total={total}, limit={limit}). "
            "Pass a higher 'limit' or a more specific 'filter'."
        )
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
    # Record cross-project attribution BEFORE assigning. When a worker
    # files a task for a *different* worker, the calling worker is the
    # source and the arg is the target — without this the task row
    # lands in the DB with ``source_worker=''`` and cross-project
    # lineage is lost. Self-targeted tasks aren't cross-project and
    # are skipped.
    if target and target != worker_name:
        source = worker_name if worker_name and worker_name != "unknown" else ""
        d.edit_task(
            task.id,
            source_worker=source,
            target_worker=target,
            actor=worker_name,
        )
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


def _validate_batch_op(op: Any) -> tuple[str, dict[str, Any], str]:
    """Validate a single batch op. Returns ``(tool, args, error)``.

    ``error`` is empty when the op is valid. Otherwise it explains why
    the op cannot run; tool/args are still returned so callers can log
    them in the failure line.
    """
    if not isinstance(op, dict):
        return "", {}, "invalid op: not an object"
    tool = op.get("tool", "")
    if tool == "swarm_batch":
        return tool, {}, "nested swarm_batch is not allowed"
    if tool not in _TOOL_NAMES:
        return tool, {}, "unknown tool"
    op_args = op.get("args") or {}
    if not isinstance(op_args, dict):
        return tool, {}, "'args' must be an object"
    return tool, op_args, ""


def _handle_batch(d: SwarmDaemon, worker_name: str, args: dict[str, Any]) -> list[dict[str, Any]]:
    """Execute a sequence of swarm_* ops in one MCP round-trip.

    Workers that need claim_file + send_message + complete_task today
    pay three JSON-RPC round-trips. ``swarm_batch`` lets them send one
    request. Each op is still logged individually via
    ``handle_tool_call`` so the dashboard shows the real activity, not
    a single opaque "batch" entry.
    """
    if "ops" not in args:
        return [{"type": "text", "text": "Missing 'ops' — provide a non-empty array of ops."}]
    ops = args.get("ops") or []
    if not isinstance(ops, list) or not ops:
        return [
            {
                "type": "text",
                "text": "'ops' must be a non-empty array — batch needs at least one op to execute.",
            }
        ]

    fail_fast = args.get("fail_fast", True)
    total = len(ops)
    lines: list[str] = [f"Batch results ({total} ops):"]
    aborted = False

    for idx, op in enumerate(ops, start=1):
        tool, op_args, error = _validate_batch_op(op)
        label = tool or "?"
        if error:
            lines.append(f"[{idx}/{total}] {label} → error: {error}")
            if fail_fast:
                aborted = True
                break
            continue
        op_result = handle_tool_call(d, worker_name, tool, op_args)
        text = op_result[0].get("text", "") if op_result else ""
        lines.append(f"[{idx}/{total}] {tool} → {text}")

    if aborted:
        lines.append(f"Batch aborted after error (stopped with {len(lines) - 1}/{total} ops).")
    return [{"type": "text", "text": "\n".join(lines)}]


_HANDLERS = {
    "swarm_check_messages": _handle_check_messages,
    "swarm_send_message": _handle_send_message,
    "swarm_task_status": _handle_task_status,
    "swarm_claim_file": _handle_claim_file,
    "swarm_complete_task": _handle_complete_task,
    "swarm_create_task": _handle_create_task,
    "swarm_get_learnings": _handle_get_learnings,
    "swarm_report_progress": _handle_report_progress,
    "swarm_batch": _handle_batch,
}
