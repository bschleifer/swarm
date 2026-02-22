"""Abstract base class for LLM CLI providers."""

from __future__ import annotations

import os
import re
from abc import ABC, abstractmethod
from pathlib import Path

from swarm.worker.worker import WorkerState

_SHELLS = frozenset(("bash", "zsh", "sh", "fish", "dash", "ksh", "csh", "tcsh"))


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

    @abstractmethod
    def approval_response(self, approve: bool = True) -> str:
        """What to send to the PTY to approve/reject."""

    @abstractmethod
    def session_dir(self, worker_path: str) -> Path | None:
        """Path to session/usage data for this worker, or None if unsupported."""

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
