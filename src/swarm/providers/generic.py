"""Generic CLI provider for user-defined custom LLMs.

Conservative state detection: shell-exit → STUNG, all else → BUZZING.
No regex patterns — custom providers get minimal, safe defaults.
"""

from __future__ import annotations

import re

from swarm.providers.base import LLMProvider
from swarm.worker.worker import WorkerState

_NEVER_MATCH = re.compile(r"(?!)")


class GenericProvider(LLMProvider):
    """Generic provider for custom CLI-based LLM tools."""

    def __init__(self, name: str, command: list[str], display: str = "") -> None:
        self._name = name
        self._command = list(command)
        self._display = display or name.title()

    @property
    def name(self) -> str:
        return self._name

    def worker_command(self, resume: bool = True) -> list[str]:
        return list(self._command)

    def headless_command(
        self,
        prompt: str,
        output_format: str = "text",
        max_turns: int | None = None,
        session_id: str | None = None,
    ) -> list[str]:
        return [*self._command, prompt]

    def parse_headless_response(self, stdout: bytes) -> tuple[str, str | None]:
        return stdout.decode(errors="replace").strip(), None

    def classify_output(self, command: str, content: str) -> WorkerState:
        if self._is_shell_exited(command):
            return WorkerState.STUNG
        return WorkerState.BUZZING

    def has_choice_prompt(self, content: str) -> bool:
        return False

    def is_user_question(self, content: str) -> bool:
        return False

    def get_choice_summary(self, content: str) -> str:
        return ""

    def safe_tool_patterns(self) -> re.Pattern[str]:
        return _NEVER_MATCH

    def env_strip_prefixes(self) -> tuple[str, ...]:
        return ()

    @property
    def display_name(self) -> str:
        return self._display
