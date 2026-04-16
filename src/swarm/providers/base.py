"""Abstract base class for LLM CLI providers."""

from __future__ import annotations

import os
import re
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

from swarm.providers.events import EventType, TerminalEvent
from swarm.providers.styled import StyledContent
from swarm.worker.worker import TokenUsage, WorkerState

_SHELLS = frozenset(("bash", "zsh", "sh", "fish", "dash", "ksh", "csh", "tcsh"))

# Shared safe command lists — referenced by each provider's safe_tool_patterns
SAFE_SHELL_CMDS = r"ls|cat|head|tail|find|wc|stat|file|which|pwd|echo|date"
SAFE_GIT_SUBCMDS = r"status|log|diff|show|branch|remote|tag"

# Canonical tail-window sizes for _get_tail() — prevents magic-number drift.
TAIL_LAST_LINE = 1  # Single line: empty prompt check
TAIL_NARROW = 5  # Narrow: accept-edits, idle prompt, hints
TAIL_MEDIUM = 15  # Medium: user rules, user question detection
TAIL_WIDE = 30  # Wide: safe patterns, choice menus, plan markers


class LLMProvider(ABC):
    """Abstract base for LLM CLI provider implementations.

    Each provider encapsulates all CLI-specific behavior: startup commands,
    state detection patterns, headless invocation, approval handling, etc.
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Short identifier for this provider (e.g. 'claude', 'gemini')."""

    @abstractmethod
    def worker_command(self, resume: bool = True) -> list[str]:
        """Command to launch an interactive worker session."""

    @abstractmethod
    def headless_command(
        self,
        prompt: str,
        output_format: str = "text",
        max_turns: int | None = None,
        session_id: str | None = None,
    ) -> list[str]:
        """Command for non-interactive headless prompt."""

    @abstractmethod
    def parse_headless_response(self, stdout: bytes) -> tuple[str, str | None]:
        """Parse headless output -> (text_result, session_id_or_none)."""

    @abstractmethod
    def classify_output(self, command: str, content: str) -> WorkerState:
        """Classify worker state from foreground command name and PTY output."""

    @abstractmethod
    def has_choice_prompt(self, content: str) -> bool:
        """Detect approval/choice prompts that drones can auto-handle."""

    @abstractmethod
    def is_user_question(self, content: str) -> bool:
        """Detect prompts requiring human input (never auto-approve)."""

    @abstractmethod
    def get_choice_summary(self, content: str) -> str:
        """Extract a short summary of the choice/approval prompt."""

    @abstractmethod
    def safe_tool_patterns(self) -> re.Pattern[str]:
        """Regex for tool invocations safe to auto-approve."""

    @abstractmethod
    def env_strip_prefixes(self) -> tuple[str, ...]:
        """Env var prefixes to strip when running headless."""

    def approval_response(self, approve: bool = True) -> str:
        """What to send to the PTY to approve/reject.

        Default: y/n (used by Gemini, Codex). Claude overrides with Enter/Esc.
        """
        return "y\r" if approve else "n\r"

    def session_dir(self, worker_path: str) -> Path | None:
        """Path to session/usage data for this worker, or None if unsupported."""
        return None

    # --- Shared helpers for subclasses ---

    def _is_shell_exited(self, command: str) -> bool:
        """Check if the foreground command is a shell (worker has exited)."""
        return os.path.basename(command) in _SHELLS

    def _get_tail(self, content: str, lines: int = 30) -> str:
        """Extract the last N lines from content for pattern matching."""
        all_lines = content.strip().splitlines()
        return "\n".join(all_lines[-lines:])

    # --- Optional methods with sensible defaults ---

    def has_plan_prompt(self, content: str) -> bool:
        """Detect plan approval prompts. Default: False (only Claude has this)."""
        return False

    def has_accept_edits_prompt(self, content: str) -> bool:
        """Detect edit acceptance prompts. Default: False (only Claude has this)."""
        return False

    def has_idle_prompt(self, content: str) -> bool:
        """Check if output shows a normal idle input prompt."""
        return False

    def has_empty_prompt(self, content: str) -> bool:
        """Check if output shows an empty input prompt ready for continuation."""
        return False

    @property
    def supports_slash_commands(self) -> bool:
        """Whether the CLI supports slash commands (/fix-and-ship, etc.)."""
        return False

    @property
    def supports_hooks(self) -> bool:
        """Whether the CLI supports installable hooks."""
        return False

    @property
    def supports_resume(self) -> bool:
        """Whether the headless CLI supports --resume for session continuity."""
        return False

    @property
    def display_name(self) -> str:
        """Human-readable name for prompts (e.g. 'Claude Code', 'Gemini CLI')."""
        return self.name.title()

    @property
    def supports_max_turns(self) -> bool:
        """Whether the headless CLI supports --max-turns."""
        return False

    @property
    def supports_json_output(self) -> bool:
        """Whether the headless CLI supports --output-format json."""
        return False

    def parse_usage(self, result: dict[str, Any]) -> TokenUsage | None:
        """Extract token usage from a headless response. None if unsupported."""
        return None

    def parse_events(self, content: str) -> list[TerminalEvent]:
        """Parse structured events from terminal output.

        Default returns a single UNKNOWN event wrapping the content.
        Providers override to extract typed events (tool calls, prompts, etc.).
        """
        return [TerminalEvent(EventType.UNKNOWN, content)]

    def classify_with_events(
        self, command: str, content: str
    ) -> tuple[WorkerState, list[TerminalEvent]]:
        """Classify worker state and parse events in one pass.

        Default calls classify_output() and parse_events() independently.
        Providers can override to avoid double-parsing.
        """
        state = self.classify_output(command, content)
        events = self.parse_events(content)
        return state, events

    # --- Style-aware classification (backward-compatible defaults) ---

    def classify_styled_output(self, command: str, styled: StyledContent) -> WorkerState:
        """Classify worker state using styled terminal content.

        Default falls back to text-only ``classify_output()``.
        Providers override to use style data as a secondary signal.
        """
        return self.classify_output(command, styled.text)

    def classify_styled_with_events(
        self, command: str, styled: StyledContent
    ) -> tuple[WorkerState, list[TerminalEvent]]:
        """Classify state and parse events from styled content.

        Default falls back to ``classify_with_events()`` using text only.
        """
        return self.classify_with_events(command, styled.text)
