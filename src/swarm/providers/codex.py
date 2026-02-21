"""Codex CLI (OpenAI) provider — stub implementation based on research.

HIGH RISK: Codex uses Ratatui alternate screen buffer by default.
PTY text detection may not work — may need --no-alt-screen or JSONL monitoring.
Install: npm i -g @openai/codex
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

from swarm.providers.base import LLMProvider
from swarm.worker.worker import WorkerState

# Codex uses Ratatui icons — these may not survive ANSI stripping
_RE_CODEX_IDLE = re.compile(r"[◇□]")
_RE_CODEX_BUSY = re.compile(r"[▶▷]")

_SAFE_PATTERNS = re.compile(
    r"shell\(.*(ls|cat|head|tail|find|wc|stat|file|which|pwd|echo|date)\b"
    r"|shell\(.*git\s+(status|log|diff|show|branch|remote|tag)\b"
    r"|file_read\("
    r"|file_search\(",
    re.IGNORECASE,
)


class CodexProvider(LLMProvider):
    """Codex CLI provider (stub — requires empirical alternate screen testing)."""

    @property
    def name(self) -> str:
        return "codex"

    def worker_command(self, resume: bool = True) -> list[str]:
        # --no-alt-screen is critical for PTY text detection
        return ["codex", "--no-alt-screen"]

    def headless_command(
        self,
        prompt: str,
        output_format: str = "text",
        max_turns: int | None = None,
        session_id: str | None = None,
    ) -> list[str]:
        args = ["codex", "exec", prompt]
        if output_format == "json":
            args.append("--json")
        # Codex doesn't support --resume or --max-turns
        return args

    def parse_headless_response(self, stdout: bytes) -> tuple[str, str | None]:
        """Parse Codex JSONL event stream, extract last agent_message."""
        text = stdout.decode(errors="replace")
        last_message = ""
        for line in text.strip().splitlines():
            try:
                event = json.loads(line)
                if event.get("type") == "item.completed":
                    item = event.get("item", {})
                    if item.get("type") == "agent_message":
                        last_message = item.get("text", "")
            except json.JSONDecodeError:
                continue
        return last_message or text, None

    def classify_output(self, command: str, content: str) -> WorkerState:
        shell_name = os.path.basename(command)
        if shell_name in ("bash", "zsh", "sh", "fish", "dash", "ksh", "csh", "tcsh"):
            return WorkerState.STUNG

        lines = content.strip().splitlines()
        tail = "\n".join(lines[-30:])

        # Ratatui icons (may not survive ANSI stripping)
        if _RE_CODEX_BUSY.search(tail):
            return WorkerState.BUZZING

        if _RE_CODEX_IDLE.search(tail):
            return WorkerState.RESTING

        return WorkerState.BUZZING

    def has_choice_prompt(self, content: str) -> bool:
        # Codex uses Ratatui widgets for approval — TBD how they render in raw PTY
        return False

    def is_user_question(self, content: str) -> bool:
        return False

    def get_choice_summary(self, content: str) -> str:
        return ""

    def safe_tool_patterns(self) -> re.Pattern[str]:
        return _SAFE_PATTERNS

    def env_strip_prefixes(self) -> tuple[str, ...]:
        return ("OPENAI",)

    def approval_response(self, approve: bool = True) -> str:
        # TBD: depends on Ratatui widget behavior in raw PTY
        return "y\r" if approve else "n\r"

    def session_dir(self, worker_path: str) -> Path | None:
        # Codex stores sessions in ~/.codex/sessions/YYYY/MM/DD/
        return None
