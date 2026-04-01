"""Hook routes — Claude Code hook callbacks for approval, session lifecycle, and events."""

from __future__ import annotations

import os
from typing import Any

from aiohttp import web

from swarm.drones.log import DroneAction, LogCategory, SystemAction
from swarm.drones.rules import ALWAYS_ESCALATE
from swarm.logging import get_logger
from swarm.server.helpers import get_daemon, handle_errors, json_error

_log = get_logger("server.hooks")

# Safe tools that are always auto-approved — no need to query rules.
_ALWAYS_APPROVE_TOOLS = frozenset({"Read", "Glob", "Grep", "WebSearch", "WebFetch"})

# Tools that always need operator approval via the drone rules engine.
_ALWAYS_ESCALATE_TOOLS = frozenset({"Bash"})


def register(app: web.Application) -> None:
    app.router.add_post("/api/hooks/approval", handle_approval)
    app.router.add_post("/api/hooks/session-end", handle_session_end)
    app.router.add_post("/api/hooks/event", handle_event)


@handle_errors
async def handle_approval(request: web.Request) -> web.Response:
    """PreToolUse hook endpoint — evaluate tool use against drone approval rules.

    Receives Claude Code's PreToolUse hook input:
    ``{"tool_name": "Bash", "tool_input": {...}, "session_id": "...", ...}``

    Returns ``{"decision": "approve"|"block"|"passthrough", "reason": "..."}``
    """
    d = get_daemon(request)

    try:
        body: dict[str, Any] = await request.json()
    except Exception:
        return json_error("invalid JSON body", status=400)

    tool_name = body.get("tool_name", "")
    tool_input = body.get("tool_input", {})

    if not tool_name:
        return json_error("missing tool_name", status=400)

    # Fast path: always-approve safe read-only tools
    if tool_name in _ALWAYS_APPROVE_TOOLS:
        return web.json_response({"decision": "approve", "reason": "safe read-only tool"})

    # Build a text representation of the tool call for rules matching.
    # This mirrors what the drone sees in terminal output.
    tool_text = _build_tool_text(tool_name, tool_input)

    # Safety net: always block destructive patterns regardless of rules
    if ALWAYS_ESCALATE.search(tool_text):
        _log_hook_decision(d, tool_name, "block", "destructive pattern detected")
        return web.json_response(
            {
                "decision": "block",
                "reason": "Blocked — destructive operation requires manual approval",
            }
        )

    return _evaluate_rules(d, body, tool_name, tool_text)


@handle_errors
async def handle_session_end(request: web.Request) -> web.Response:
    """SessionEnd hook endpoint — notify daemon that a Claude session ended.

    This enables immediate STUNG detection without relying on /proc polling.
    """
    d = get_daemon(request)

    try:
        body: dict[str, Any] = await request.json()
    except Exception:
        return json_error("invalid JSON body", status=400)

    session_id = body.get("session_id", "")
    _log.info("session ended: %s", session_id or "(unknown)")

    # Find which worker this session belongs to and mark it
    worker = _identify_worker(d, body)
    if worker:
        _log.info("session end for worker %s — signaling STUNG", worker.name)
        # Emit event so pilot picks up the session end immediately
        d.broadcast(
            {
                "type": "hook_session_end",
                "worker": worker.name,
                "session_id": session_id,
            }
        )
    else:
        _log.debug("session end from unknown worker (session_id=%s)", session_id)

    return web.json_response({"status": "ok"})


@handle_errors
async def handle_event(request: web.Request) -> web.Response:
    """Generic hook event endpoint — forward Claude Code lifecycle events.

    Handles SubagentStart, SubagentStop, PreCompact, PostCompact, TeammateIdle.
    """
    d = get_daemon(request)

    try:
        body: dict[str, Any] = await request.json()
    except Exception:
        return json_error("invalid JSON body", status=400)

    hook_event = body.get("hook_event", "unknown")
    worker = _identify_worker(d, body)
    worker_name = worker.name if worker else "unknown"

    _log.debug("hook event %s from worker %s", hook_event, worker_name)

    # Track compaction state on workers
    if worker and hook_event in ("PreCompact", "preCompact"):
        worker.compacting = True
    elif worker and hook_event in ("PostCompact", "postCompact"):
        worker.compacting = False
        worker._context_warned = False  # reset warning after successful compact

    # Broadcast to dashboard subscribers
    d.broadcast(
        {
            "type": "hook_event",
            "hook_event": hook_event,
            "worker": worker_name,
            "data": body,
        }
    )

    return web.json_response({"status": "ok"})


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _evaluate_rules(d: Any, body: dict[str, Any], tool_name: str, tool_text: str) -> web.Response:
    """Evaluate tool use against drone approval rules and return a JSON response."""
    from swarm.drones.rules import dry_run_rules

    pilot = d.pilot
    if pilot is None or not pilot.enabled:
        return web.json_response({"decision": "passthrough", "reason": "drones disabled"})

    drone_config = d.config.drones
    worker = _identify_worker(d, body)
    worker_name = worker.name if worker else "unknown"

    # Collect per-worker + global rules
    worker_rules: list[Any] = []
    if worker and pilot._worker_configs:
        wc = pilot._worker_configs.get(worker.name)
        if wc is not None and wc.approval_rules:
            worker_rules = list(wc.approval_rules)
    all_rules = worker_rules + list(drone_config.approval_rules)

    if not all_rules and tool_name not in _ALWAYS_ESCALATE_TOOLS:
        _log_hook_decision(d, tool_name, "approve", "no rules configured", worker_name)
        return web.json_response({"decision": "approve", "reason": "no approval rules configured"})

    results = dry_run_rules(
        tool_text, all_rules, allowed_read_paths=drone_config.allowed_read_paths
    )
    if not results:
        return web.json_response({"decision": "passthrough", "reason": "no matching rule"})

    result = results[0]
    if result.decision == "approve":
        _log_hook_decision(d, tool_name, "approve", f"rule matched: {result.source}", worker_name)
        return web.json_response(
            {
                "decision": "approve",
                "reason": f"Approved by drone rule ({result.source})",
            }
        )

    # "escalate" → check if queen can handle this autonomously
    if _queen_can_approve(d, tool_name):
        _log_hook_decision(
            d, tool_name, "approve", f"queen-delegated: {result.source}", worker_name
        )
        return web.json_response(
            {
                "decision": "approve",
                "reason": f"Approved under queen oversight ({result.source})",
            }
        )

    # No queen → pass through so Claude Code shows the normal permission prompt
    _log_hook_decision(d, tool_name, "passthrough", f"escalated: {result.source}", worker_name)
    return web.json_response(
        {
            "decision": "passthrough",
            "reason": f"Requires operator approval ({result.source})",
        }
    )


def _queen_can_approve(d: Any, tool_name: str) -> bool:
    """Check if the queen is active and can handle this approval autonomously."""
    queen = getattr(d, "queen", None)
    if queen is None or not queen.enabled or not queen.can_call:
        return False
    # Don't auto-approve Bash under queen — too risky without explicit review
    if tool_name in _ALWAYS_ESCALATE_TOOLS:
        return False
    return True


def _build_tool_text(tool_name: str, tool_input: dict[str, Any]) -> str:
    """Build a text representation of a tool call for rules matching.

    Mimics the format the drone sees in terminal output so existing
    regex-based approval rules work unchanged.
    """
    parts = [tool_name]
    if tool_name == "Bash" and "command" in tool_input:
        parts.append(str(tool_input["command"]))
    elif tool_name == "Write" and "file_path" in tool_input:
        parts.append(str(tool_input["file_path"]))
    elif tool_name == "Edit" and "file_path" in tool_input:
        parts.append(str(tool_input["file_path"]))
    elif tool_name == "Read" and "file_path" in tool_input:
        parts.append(f"Read({tool_input['file_path']})")
    else:
        # Generic: include all input values
        for v in tool_input.values():
            parts.append(str(v))
    return "\n".join(parts)


def _identify_worker(d: Any, body: dict[str, Any]) -> Any:
    """Best-effort worker identification from hook input.

    Tries session_id first, then CWD matching against worker paths.
    """
    # Try session_id if present (future: map session IDs to workers)
    # For now, match by CWD — hooks inherit the Claude Code process CWD
    cwd = body.get("cwd", "")
    if not cwd:
        # Fall back to SWARM_WORKER env var if the hook script forwards it
        cwd = body.get("worker_cwd", "")

    if cwd:
        cwd_resolved = os.path.realpath(cwd)
        for w in d.workers:
            if hasattr(w, "path") and w.path:
                worker_path = os.path.realpath(str(w.path))
                if cwd_resolved == worker_path or cwd_resolved.startswith(worker_path + "/"):
                    return w

    # Fallback: if only one worker exists, it's probably that one
    if len(d.workers) == 1:
        return d.workers[0]

    return None


def _log_hook_decision(
    d: Any,
    tool_name: str,
    decision: str,
    reason: str,
    worker_name: str = "unknown",
) -> None:
    """Log a hook-based approval decision to the drone log."""
    if d.drone_log is not None:
        d.drone_log.add(
            DroneAction.CONTINUED if decision == "approve" else SystemAction.QUEEN_BLOCKED,
            worker_name,
            f"hook:{tool_name} → {decision} ({reason})",
            metadata={"source": "hook", "tool_name": tool_name},
            category=LogCategory.DRONE,
        )
