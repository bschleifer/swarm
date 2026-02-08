"""State detection — ported from swarm.sh detect_pane_state()."""

from __future__ import annotations

import os
import re

from swarm.worker.worker import WorkerState


def classify_pane_content(command: str, content: str) -> WorkerState:
    """Classify a pane's state based on its foreground command and captured content.

    Logic (from swarm.sh):
    - If foreground process is a shell (bash/zsh/sh), Claude has exited -> STUNG
    - If content contains "esc to interrupt", Claude is processing -> BUZZING
    - If content shows a prompt (> or ❯) or shortcuts hint, Claude is idle -> RESTING
    - Default: BUZZING (assume working if unclear)
    """
    # Shell as foreground = Claude exited
    shell_name = os.path.basename(command)
    if shell_name in ("bash", "zsh", "sh", "fish", "dash", "ksh", "csh", "tcsh"):
        return WorkerState.STUNG

    # "esc to interrupt" only appears when Claude is actively processing
    if "esc to interrupt" in content:
        return WorkerState.BUZZING

    # Check for Claude's input prompt or shortcuts hint (last 5 lines only)
    tail = "\n".join(content.strip().splitlines()[-5:])
    if re.search(r"^\s*[>❯]", tail, re.MULTILINE) or "? for shortcuts" in tail:
        return WorkerState.RESTING

    # Default: assume working
    return WorkerState.BUZZING


def has_choice_prompt(content: str) -> bool:
    """Check if the pane is showing a Claude Code numbered choice menu.

    Detects patterns like:
        > 1. Always allow
          2. Yes
          3. No
        Enter to select · ↑/↓ to navigate
    """
    lines = content.strip().splitlines()
    if not lines:
        return False
    tail = "\n".join(lines[-15:])
    # Must have "Enter to select" or "to navigate" (Claude Code menu footer)
    has_menu_footer = bool(re.search(r"(enter to select|to navigate)", tail, re.IGNORECASE))
    # Must have numbered options like "> 1." or "  2."
    has_numbered = bool(re.search(r"^\s*[>❯]?\s*\d+\.", tail, re.MULTILINE))
    return has_menu_footer and has_numbered


def has_idle_prompt(content: str) -> bool:
    """Check if the pane shows a normal Claude Code input prompt.

    Matches both empty prompts and prompts with suggestion text:
        >                           (bare prompt)
        ❯                           (bare prompt)
        > Try "how does foo work"   (prompt with suggestion)
        ? for shortcuts             (shortcuts hint)
        ctrl+t to hide tasks        (task hint)
    """
    lines = content.strip().splitlines()
    if not lines:
        return False
    tail = "\n".join(lines[-5:])
    # Bare prompt or prompt with suggestion text
    if re.search(r"^\s*[>❯]", tail, re.MULTILINE):
        return True
    # Claude Code hints that appear at idle
    if re.search(r"(\? for shortcuts|ctrl\+t to hide)", tail, re.IGNORECASE):
        return True
    return False


def has_empty_prompt(content: str) -> bool:
    """Check if the pane shows an empty input prompt (ready for continuation)."""
    lines = content.strip().splitlines()
    if not lines:
        return False
    last_line = lines[-1].strip()
    return bool(re.match(r"^[>❯]\s*$", last_line))
