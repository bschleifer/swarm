"""State detection — ported from swarm.sh detect_pane_state()."""

from __future__ import annotations

import os
import re

from swarm.worker.worker import WorkerState

# Pre-compiled patterns — these run every poll cycle for every worker
_RE_PROMPT = re.compile(r"^\s*[>❯]", re.MULTILINE)
# Cursor on a numbered option: "> 1." or "❯ 1." (the selected choice)
_RE_CURSOR_OPTION = re.compile(r"^\s*[>❯]\s*\d+\.", re.MULTILINE)
# Indented numbered option without cursor: "  2." (other choices)
_RE_OTHER_OPTION = re.compile(r"^\s+\d+\.", re.MULTILINE)
_RE_HINTS = re.compile(r"(\? for shortcuts|ctrl\+t to hide)", re.IGNORECASE)
_RE_EMPTY_PROMPT = re.compile(r"^[>❯]\s*$")


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

    # Check the last 5 lines — "esc to interrupt" in scrollback history
    # should NOT prevent RESTING detection (it lingers after previous work)
    tail = "\n".join(content.strip().splitlines()[-5:])

    if "esc to interrupt" in tail:
        return WorkerState.BUZZING
    if _RE_PROMPT.search(tail) or "? for shortcuts" in tail:
        # Actionable prompts (choice menu, plan approval, empty prompt) → WAITING
        # Plain idle prompt (with suggestion text or hints) → RESTING
        if has_choice_prompt(content) or has_plan_prompt(content) or has_empty_prompt(content):
            return WorkerState.WAITING
        return WorkerState.RESTING

    # Broader check — the prompt cursor (❯/>) may be above the last 5 lines
    # when a long choice menu is displayed (has_choice_prompt checks last 15 lines)
    if has_choice_prompt(content) or has_plan_prompt(content):
        return WorkerState.WAITING

    # Default: assume working
    return WorkerState.BUZZING


def has_choice_prompt(content: str) -> bool:
    """Check if the pane is showing a Claude Code numbered choice menu.

    Detects the structural pattern shared by ALL Claude Code menus — a cursor
    (> or ❯) on one numbered option plus at least one other indented option:

        > 1. Always allow          ← cursor option
          2. Yes                   ← other option(s)
          3. No
        <any footer text>

    This is footer-agnostic: permission menus ("Enter to select"), confirmation
    prompts ("Esc to cancel"), and future prompt types all share this shape.
    """
    lines = content.strip().splitlines()
    if not lines:
        return False
    tail = "\n".join(lines[-15:])
    return bool(_RE_CURSOR_OPTION.search(tail)) and bool(_RE_OTHER_OPTION.search(tail))


def get_choice_summary(content: str) -> str:
    """Extract a short summary of the choice menu being auto-selected.

    Returns something like ``"Do you want to proceed?" → 1. Yes``
    or just ``1. Yes`` if no question line is found above the options.
    """
    lines = content.strip().splitlines()
    tail = lines[-15:]
    cursor_idx = None
    selected = ""
    for i in range(len(tail) - 1, -1, -1):
        if _RE_CURSOR_OPTION.match(tail[i]):
            cursor_idx = i
            selected = tail[i].lstrip().lstrip(">❯").strip()
            break
    if not selected:
        return ""
    # Walk backwards from the cursor option to find the question/context line
    question = ""
    for i in range(cursor_idx - 1, -1, -1):
        stripped = tail[i].strip()
        if stripped and not _RE_OTHER_OPTION.match(tail[i]):
            question = stripped
            break
    if question:
        return f'"{question}" → {selected}'
    return selected


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
    if _RE_PROMPT.search(tail):
        return True
    # Claude Code hints that appear at idle
    if _RE_HINTS.search(tail):
        return True
    return False


_RE_PLAN_MARKERS = re.compile(
    r"plan file|plan saved|"
    r"proceed with (?:this|the) plan|"
    r"approve (?:this|the) plan",
    re.IGNORECASE,
)


def has_plan_prompt(content: str) -> bool:
    """Check if the pane is showing a Claude Code plan approval prompt.

    Claude Code enters plan mode and then presents a plan for user approval.
    The prompt contains specific markers like "proceed with this plan",
    "plan mode", or "plan file" — not just the generic word "plan" which
    appears frequently in normal worker conversations.
    """
    lines = content.strip().splitlines()
    if not lines:
        return False
    tail = "\n".join(lines[-30:])
    # Must be a choice prompt with plan-specific markers
    if not (bool(_RE_CURSOR_OPTION.search(tail)) and bool(_RE_OTHER_OPTION.search(tail))):
        return False
    return bool(_RE_PLAN_MARKERS.search(tail))


def is_user_question(content: str) -> bool:
    """Check if a choice menu is a Claude Code AskUserQuestion prompt.

    AskUserQuestion prompts require user decision-making and must NEVER be
    auto-continued by drones.  They always include "Type something" and/or
    "Chat about this" as trailing options — markers that never appear in
    standard tool-permission prompts.
    """
    lines = content.strip().splitlines()
    tail_lower = "\n".join(lines[-15:]).lower()
    return "chat about this" in tail_lower or "type something" in tail_lower


def has_empty_prompt(content: str) -> bool:
    """Check if the pane shows an empty input prompt (ready for continuation)."""
    lines = content.strip().splitlines()
    if not lines:
        return False
    last_line = lines[-1].strip()
    return bool(_RE_EMPTY_PROMPT.match(last_line))
