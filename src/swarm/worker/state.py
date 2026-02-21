"""State detection — classifies worker PTY output into lifecycle states.

These module-level functions delegate to :class:`ClaudeProvider` for backward
compatibility.  Provider-aware code paths (pilot, daemon) should call
``provider.classify_output()`` etc. directly.
"""

from __future__ import annotations

from swarm.worker.worker import WorkerState

# Lazy-loaded singleton — avoid circular imports and keep startup fast.
_claude: object = None


def _get_claude():
    global _claude
    if _claude is None:
        from swarm.providers.claude import ClaudeProvider

        _claude = ClaudeProvider()
    return _claude


def classify_worker_output(command: str, content: str) -> WorkerState:
    """Classify a worker's state from its foreground command and PTY output.

    Delegates to :meth:`ClaudeProvider.classify_output`.
    """
    return _get_claude().classify_output(command, content)


def has_choice_prompt(content: str) -> bool:
    """Check if the terminal is showing a Claude Code numbered choice menu.

    Detects the structural pattern shared by ALL Claude Code menus — a cursor
    (> or ❯) on one numbered option plus at least one other indented option:

        > 1. Always allow          ← cursor option
          2. Yes                   ← other option(s)
          3. No
        <any footer text>

    This is footer-agnostic: permission menus ("Enter to select"), confirmation
    prompts ("Esc to cancel"), and future prompt types all share this shape.

    Uses last 25 lines to handle long file diffs or content blocks in
    permission prompts that can push the numbered options higher.
    """
    return _get_claude().has_choice_prompt(content)


def get_choice_summary(content: str) -> str:
    """Extract a short summary of the choice menu being auto-selected.

    Returns something like ``"Do you want to proceed?" → 1. Yes``
    or just ``1. Yes`` if no question line is found above the options.
    """
    return _get_claude().get_choice_summary(content)


def has_idle_prompt(content: str) -> bool:
    """Check if the output shows a normal Claude Code input prompt.

    Matches both empty prompts and prompts with suggestion text:
        >                           (bare prompt)
        ❯                           (bare prompt)
        > Try "how does foo work"   (prompt with suggestion)
        ? for shortcuts             (shortcuts hint)
        ctrl+t to hide tasks        (task hint)
    """
    return _get_claude().has_idle_prompt(content)


def has_plan_prompt(content: str) -> bool:
    """Check if the terminal is showing a Claude Code plan approval prompt.

    Claude Code enters plan mode and then presents a plan for user approval.
    The prompt contains specific markers like "proceed with this plan",
    "plan mode", or "plan file" — not just the generic word "plan" which
    appears frequently in normal worker conversations.
    """
    return _get_claude().has_plan_prompt(content)


def is_user_question(content: str) -> bool:
    """Check if a choice menu is a Claude Code AskUserQuestion prompt.

    AskUserQuestion prompts require user decision-making and must NEVER be
    auto-continued by drones.  They always include "Type something" and/or
    "Chat about this" as trailing options — markers that never appear in
    standard tool-permission prompts.
    """
    return _get_claude().is_user_question(content)


def has_accept_edits_prompt(content: str) -> bool:
    """Check if the terminal shows a Claude Code 'accept edits' prompt.

    Claude Code shows this prompt after skills like /check or /commit
    generate file edits::

        >> accept edits on (shift+tab to cycle)

    The ``>>`` prefix distinguishes it from normal ``>`` input prompts.
    """
    return _get_claude().has_accept_edits_prompt(content)


def has_empty_prompt(content: str) -> bool:
    """Check if the output shows an empty input prompt (ready for continuation)."""
    return _get_claude().has_empty_prompt(content)
