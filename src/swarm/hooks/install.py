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

    # Install PostToolUse hooks
    _install_cross_task_hook(settings)
    _install_complete_task_hook(settings)

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


_CROSS_TASK_HOOK_SRC = Path(__file__).parent / "cross_task_hook.sh"
_CROSS_TASK_HOOK_DST = Path.home() / ".swarm" / "hooks" / "cross-task-hook.sh"

_COMPLETE_TASK_HOOK_SRC = Path(__file__).parent / "complete_task_hook.sh"
_COMPLETE_TASK_HOOK_DST = Path.home() / ".swarm" / "hooks" / "complete-task-hook.sh"


def _install_cross_task_hook(settings: dict) -> None:
    """Copy the cross-task hook script and register it in settings."""
    # Copy script to ~/.swarm/hooks/
    _CROSS_TASK_HOOK_DST.parent.mkdir(parents=True, exist_ok=True)
    if _CROSS_TASK_HOOK_SRC.exists():
        shutil.copy2(_CROSS_TASK_HOOK_SRC, _CROSS_TASK_HOOK_DST)
        _CROSS_TASK_HOOK_DST.chmod(0o755)

    # Add PostToolUse hook entry if not present
    hooks = settings.setdefault("hooks", {})
    post_tool = hooks.setdefault("PostToolUse", [])

    hook_command = str(_CROSS_TASK_HOOK_DST)
    already_installed = any(
        matcher.get("matcher") == "Write"
        and any(
            h.get("command", "").endswith("cross-task-hook.sh") for h in matcher.get("hooks", [])
        )
        for matcher in post_tool
    )
    if not already_installed:
        post_tool.append(
            {
                "matcher": "Write",
                "hooks": [
                    {
                        "type": "command",
                        "command": hook_command,
                        "timeout": 5000,
                    }
                ],
            }
        )


def _install_complete_task_hook(settings: dict) -> None:
    """Copy the complete-task hook script and register it in settings."""
    _COMPLETE_TASK_HOOK_DST.parent.mkdir(parents=True, exist_ok=True)
    if _COMPLETE_TASK_HOOK_SRC.exists():
        shutil.copy2(_COMPLETE_TASK_HOOK_SRC, _COMPLETE_TASK_HOOK_DST)
        _COMPLETE_TASK_HOOK_DST.chmod(0o755)

    hooks = settings.setdefault("hooks", {})
    post_tool = hooks.setdefault("PostToolUse", [])

    hook_command = str(_COMPLETE_TASK_HOOK_DST)
    already_installed = any(
        matcher.get("matcher") == "Write"
        and any(
            h.get("command", "").endswith("complete-task-hook.sh") for h in matcher.get("hooks", [])
        )
        for matcher in post_tool
    )
    if not already_installed:
        post_tool.append(
            {
                "matcher": "Write",
                "hooks": [
                    {
                        "type": "command",
                        "command": hook_command,
                        "timeout": 5000,
                    }
                ],
            }
        )


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
