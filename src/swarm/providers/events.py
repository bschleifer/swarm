"""Structured terminal events — typed signals extracted from PTY output.

Instead of matching raw text with regexes, the event layer provides typed
events (tool calls, prompts, choices, etc.) that drones and the queen can
use for decision-making.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class EventType(Enum):
    TOOL_CALL = "tool_call"
    THINKING = "thinking"  # "esc to interrupt" / spinner
    PROMPT = "prompt"  # idle input prompt
    CHOICE = "choice"  # numbered choice menu
    PLAN = "plan"  # plan approval prompt
    ACCEPT_EDITS = "accept_edits"  # batch edit acceptance
    USER_QUESTION = "user_question"
    ERROR = "error"
    UNKNOWN_PROMPT = "unknown_prompt"  # unrecognized prompt state
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class TerminalEvent:
    """A single structured event parsed from terminal output."""

    event_type: EventType
    content: str = ""
    tool_name: str | None = None  # "Bash", "Edit", "Read", etc.
    metadata: dict[str, object] = field(default_factory=dict)
