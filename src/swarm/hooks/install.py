"""Install Claude Code permissions and hooks for Swarm workers."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from swarm.providers import get_provider

PERMISSIONS_CONFIG = {
    "permissions": {
        "allow": ["Edit", "Write", "WebFetch", "WebSearch"],
    }
}


def install(global_install: bool = False) -> None:
    """Install permissions into Claude Code settings.

    Only installs permissions for the Claude provider — other providers
    do not support the same settings mechanism.
    """
    provider = get_provider()
    if not provider.supports_hooks:
        return

    if global_install:
        settings_path = Path.home() / ".claude" / "settings.json"
    else:
        settings_path = Path.cwd() / ".claude" / "settings.json"

    settings_path.parent.mkdir(parents=True, exist_ok=True)

    # Load existing settings
    if settings_path.exists():
        try:
            settings = json.loads(settings_path.read_text())
        except json.JSONDecodeError:
            # Back up corrupt file and start fresh
            bak = settings_path.with_suffix(".json.bak")
            settings_path.rename(bak)
            settings = {}
    else:
        settings = {}

    # Merge permissions — add ours without duplicating
    existing_allow = settings.get("permissions", {}).get("allow", [])
    for perm in PERMISSIONS_CONFIG["permissions"]["allow"]:
        if perm not in existing_allow:
            existing_allow.append(perm)

    settings.setdefault("permissions", {})["allow"] = existing_allow

    # Remove broken legacy PreToolUse auto-allow hook if present
    _remove_legacy_hook(settings)

    # Remove legacy PostToolUse hooks (replaced by MCP tools)
    _remove_legacy_post_tool_hooks(settings)

    # Install PreToolUse approval hook (replaces PTY-injection approvals)
    _install_approval_hook(settings)

    # Install SessionEnd hook (immediate STUNG detection)
    _install_session_end_hook(settings)

    # Install SessionStart hook (worker bootstrap: assigned task + unread messages)
    _install_session_start_hook(settings)

    # Install lifecycle event hooks (SubagentStart/Stop, PreCompact/PostCompact, etc.)
    _install_event_hooks(settings)

    # Register Swarm MCP server for worker↔daemon communication
    _install_mcp_server(settings)

    settings_path.write_text(json.dumps(settings, indent=2) + "\n")


def _remove_legacy_hook(settings: dict) -> None:
    """Remove the old broken PreToolUse auto-allow hook that used the wrong JSON schema."""
    hooks = settings.get("hooks", {})
    pre_tool = hooks.get("PreToolUse", [])
    if not pre_tool:
        return

    legacy_matcher = "Read|Edit|Write|Glob|Grep|WebSearch|WebFetch"
    pre_tool[:] = [m for m in pre_tool if m.get("matcher") != legacy_matcher]

    if not pre_tool:
        del hooks["PreToolUse"]
    if not hooks:
        settings.pop("hooks", None)


_APPROVAL_HOOK_SRC = Path(__file__).parent / "approval_hook.sh"
_APPROVAL_HOOK_DST = Path.home() / ".swarm" / "hooks" / "approval-hook.sh"

_SESSION_END_HOOK_SRC = Path(__file__).parent / "session_end_hook.sh"
_SESSION_END_HOOK_DST = Path.home() / ".swarm" / "hooks" / "session-end-hook.sh"

_SESSION_START_HOOK_SRC = Path(__file__).parent / "session_start_hook.sh"
_SESSION_START_HOOK_DST = Path.home() / ".swarm" / "hooks" / "session-start-hook.sh"

_EVENT_HOOK_SRC = Path(__file__).parent / "event_hook.sh"
_EVENT_HOOK_DST = Path.home() / ".swarm" / "hooks" / "event-hook.sh"

# Legacy hook destinations (for cleanup only)
_CROSS_TASK_HOOK_DST = Path.home() / ".swarm" / "hooks" / "cross-task-hook.sh"
_COMPLETE_TASK_HOOK_DST = Path.home() / ".swarm" / "hooks" / "complete-task-hook.sh"


def _remove_legacy_post_tool_hooks(settings: dict) -> None:
    """Remove cross-task and complete-task PostToolUse hooks.

    These are replaced by MCP tools (swarm_create_task, swarm_complete_task).
    """
    hooks = settings.get("hooks", {})
    post_tool = hooks.get("PostToolUse", [])
    if not post_tool:
        return

    post_tool[:] = [
        m
        for m in post_tool
        if not any(
            h.get("command", "").endswith(("cross-task-hook.sh", "complete-task-hook.sh"))
            for h in m.get("hooks", [])
        )
    ]

    if not post_tool:
        hooks.pop("PostToolUse", None)
    if not hooks:
        settings.pop("hooks", None)


def _install_hook_script(
    src: Path,
    dst: Path,
    settings: dict,
    event_name: str,
    matcher: str | None,
    script_suffix: str,
    timeout: int = 5000,
) -> None:
    """Generic helper to copy a hook script and register it in settings."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    if src.exists():
        shutil.copy2(src, dst)
        dst.chmod(0o755)

    hooks = settings.setdefault("hooks", {})
    event_hooks = hooks.setdefault(event_name, [])

    hook_command = str(dst)
    already_installed = any(
        m.get("matcher") == matcher
        and any(h.get("command", "").endswith(script_suffix) for h in m.get("hooks", []))
        for m in event_hooks
    )
    if not already_installed:
        entry: dict = {
            "hooks": [{"type": "command", "command": hook_command, "timeout": timeout}],
        }
        if matcher is not None:
            entry["matcher"] = matcher
        event_hooks.append(entry)


def _install_approval_hook(settings: dict) -> None:
    """Register PreToolUse hook for drone-based tool approval."""
    _install_hook_script(
        _APPROVAL_HOOK_SRC,
        _APPROVAL_HOOK_DST,
        settings,
        event_name="PreToolUse",
        matcher=None,
        script_suffix="approval-hook.sh",
    )


def _install_session_end_hook(settings: dict) -> None:
    """Register SessionEnd hook for immediate STUNG detection."""
    _install_hook_script(
        _SESSION_END_HOOK_SRC,
        _SESSION_END_HOOK_DST,
        settings,
        event_name="SessionEnd",
        matcher=None,
        script_suffix="session-end-hook.sh",
        timeout=3000,
    )


def _install_session_start_hook(settings: dict) -> None:
    """Register SessionStart hook for worker bootstrap (task + unread messages)."""
    _install_hook_script(
        _SESSION_START_HOOK_SRC,
        _SESSION_START_HOOK_DST,
        settings,
        event_name="SessionStart",
        matcher=None,
        script_suffix="session-start-hook.sh",
        timeout=3000,
    )


def _install_event_hooks(settings: dict) -> None:
    """Register lifecycle event hooks (SubagentStart/Stop, PreCompact/PostCompact)."""
    events = ["SubagentStart", "SubagentStop", "PreCompact", "PostCompact"]
    for event_name in events:
        _install_hook_script(
            _EVENT_HOOK_SRC,
            _EVENT_HOOK_DST,
            settings,
            event_name=event_name,
            matcher=None,
            script_suffix="event-hook.sh",
            timeout=3000,
        )


def _install_mcp_server(settings: dict) -> None:
    """Register Swarm as an MCP server via project-level .mcp.json.

    Project-level .mcp.json files are visible in Claude Code's /mcp dialog,
    whereas mcpServers in global settings.json are not.  We also clean up
    any legacy mcpServers entry from the global settings dict.

    MCP connections are always local (Claude Code CLI runs on the same
    machine as the daemon), so the URL is always ``http://localhost:<port>``.
    The HTTPS domain is only for the browser dashboard.
    """
    # Remove legacy global entry (was invisible in /mcp dialog)
    settings.pop("mcpServers", None)

    url = _resolve_mcp_url()

    # Write project-level .mcp.json in cwd
    mcp_path = Path.cwd() / ".mcp.json"
    mcp_config = {
        "mcpServers": {
            "swarm": {
                "type": "http",
                "url": url,
            }
        }
    }
    mcp_path.write_text(json.dumps(mcp_config, indent=2) + "\n")


def _resolve_mcp_url() -> str:
    """Determine the MCP SSE URL from the swarm config file.

    Always uses localhost — MCP is a local connection between Claude Code
    CLI and the daemon on the same machine.  Only the port is configurable.
    """
    config_path = Path.home() / ".config" / "swarm" / "config.yaml"
    if config_path.exists():
        try:
            import yaml

            data = yaml.safe_load(config_path.read_text()) or {}
            port = data.get("port", 9090)
            return f"http://localhost:{port}/mcp"
        except Exception:
            pass
    return "http://localhost:9090/mcp"


def uninstall(global_install: bool = False) -> None:
    """Remove swarm-installed permissions from Claude Code settings."""
    if global_install:
        settings_path = Path.home() / ".claude" / "settings.json"
    else:
        settings_path = Path.cwd() / ".claude" / "settings.json"

    if not settings_path.exists():
        return

    try:
        settings = json.loads(settings_path.read_text())
    except json.JSONDecodeError:
        return

    existing_allow = settings.get("permissions", {}).get("allow", [])
    swarm_perms = set(PERMISSIONS_CONFIG["permissions"]["allow"])

    before = len(existing_allow)
    existing_allow[:] = [p for p in existing_allow if p not in swarm_perms]

    if len(existing_allow) < before:
        if existing_allow:
            settings["permissions"]["allow"] = existing_allow
        else:
            settings.get("permissions", {}).pop("allow", None)
            if not settings.get("permissions"):
                settings.pop("permissions", None)
        settings_path.write_text(json.dumps(settings, indent=2) + "\n")
