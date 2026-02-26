"""Install Claude Code hooks for auto-approval and state notifications."""

from __future__ import annotations

import json
from pathlib import Path

from swarm.providers import get_provider

HOOKS_CONFIG = {
    "hooks": {
        "PreToolUse": [
            {
                "matcher": "Read|Edit|Write|Glob|Grep|WebSearch|WebFetch",
                "hooks": [
                    {
                        "type": "command",
                        "command": 'echo \'{"decision": "allow"}\'',
                    }
                ],
            }
        ],
    }
}


def install(global_install: bool = False) -> None:
    """Install hooks into Claude Code settings.

    Only installs hooks for the Claude provider — other providers
    do not support the same hook mechanism.
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

    # Merge hooks
    existing_hooks = settings.get("hooks", {})
    for event, matchers in HOOKS_CONFIG["hooks"].items():
        if event not in existing_hooks:
            existing_hooks[event] = []
        # Avoid duplicates by checking matcher
        existing_matchers = {m.get("matcher") for m in existing_hooks[event]}
        for matcher in matchers:
            if matcher.get("matcher") not in existing_matchers:
                existing_hooks[event].append(matcher)

    settings["hooks"] = existing_hooks
    settings_path.write_text(json.dumps(settings, indent=2) + "\n")


def uninstall(global_install: bool = False) -> None:
    """Remove swarm-installed hooks from Claude Code settings."""
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

    existing_hooks = settings.get("hooks", {})
    swarm_matchers = {m.get("matcher") for ms in HOOKS_CONFIG["hooks"].values() for m in ms}

    changed = False
    for event in list(existing_hooks):
        before = len(existing_hooks[event])
        existing_hooks[event] = [
            m for m in existing_hooks[event] if m.get("matcher") not in swarm_matchers
        ]
        if len(existing_hooks[event]) < before:
            changed = True
        if not existing_hooks[event]:
            del existing_hooks[event]

    if changed:
        if existing_hooks:
            settings["hooks"] = existing_hooks
        else:
            settings.pop("hooks", None)
        settings_path.write_text(json.dumps(settings, indent=2) + "\n")
