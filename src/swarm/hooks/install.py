"""Install Claude Code hooks for auto-approval and state notifications."""

from __future__ import annotations

import json
from pathlib import Path


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
    """Install hooks into Claude Code settings."""
    if global_install:
        settings_path = Path.home() / ".claude" / "settings.json"
    else:
        settings_path = Path.cwd() / ".claude" / "settings.json"

    settings_path.parent.mkdir(parents=True, exist_ok=True)

    # Load existing settings
    if settings_path.exists():
        settings = json.loads(settings_path.read_text())
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
