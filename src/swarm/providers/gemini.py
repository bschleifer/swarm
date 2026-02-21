"""Gemini CLI provider — stub implementation based on research.

Requires empirical PTY capture to finalize state detection patterns.
Install: npm install -g @google/gemini-cli
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from swarm.providers.base import LLMProvider
from swarm.worker.worker import WorkerState

_RE_GEMINI_PROMPT = re.compile(r"^gemini>\s*$", re.MULTILINE)
_RE_APPROVE_PROMPT = re.compile(r"Approve\?\s*\(y/n/always\)", re.IGNORECASE)
_RE_AWAITING = re.compile(r"Awaiting Further Direction", re.IGNORECASE)

_SAFE_PATTERNS = re.compile(
    r"run_shell_command\(.*(ls|cat|head|tail|find|wc|stat|file|which|pwd|echo|date)\b"
    r"|run_shell_command\(.*git\s+(status|log|diff|show|branch|remote|tag)\b"
    r"|FindFiles\("
    r"|SearchText\("
    r"|ReadFile\("
    r"|GoogleSearch\("
    r"|WebFetch\(",
    re.IGNORECASE,
)


class GeminiProvider(LLMProvider):
    """Gemini CLI provider (stub — patterns need empirical validation)."""

    @property
    def name(self) -> str:
        return "gemini"

    def worker_command(self, resume: bool = True) -> list[str]:
        if resume:
            return ["gemini", "--resume"]
        return ["gemini"]

    def headless_command(
        self,
        prompt: str,
        output_format: str = "text",
        max_turns: int | None = None,
        session_id: str | None = None,
    ) -> list[str]:
        args = ["gemini", "-p", prompt]
        if output_format != "text":
            args.extend(["--output-format", output_format])
        if session_id:
            args.extend(["--resume", session_id])
        # Gemini doesn't support --max-turns as a CLI flag
        return args

    def parse_headless_response(self, stdout: bytes) -> tuple[str, str | None]:
        text = stdout.decode(errors="replace").strip()
        # Gemini headless output format TBD — return raw text for now
        return text, None

    def classify_output(self, command: str, content: str) -> WorkerState:
        shell_name = os.path.basename(command)
        if shell_name in ("bash", "zsh", "sh", "fish", "dash", "ksh", "csh", "tcsh"):
            return WorkerState.STUNG

        lines = content.strip().splitlines()
        tail = "\n".join(lines[-30:])

        # Busy: spinner or "esc to cancel" text
        if "esc to cancel" in tail or "⠏" in tail or "💬" in tail:
            return WorkerState.BUZZING

        # Approval prompt
        if _RE_APPROVE_PROMPT.search(tail):
            return WorkerState.WAITING

        # Awaiting user direction
        if _RE_AWAITING.search(tail):
            return WorkerState.WAITING

        # Idle prompt
        if _RE_GEMINI_PROMPT.search("\n".join(lines[-5:])):
            return WorkerState.RESTING

        return WorkerState.BUZZING

    def has_choice_prompt(self, content: str) -> bool:
        lines = content.strip().splitlines()
        tail = "\n".join(lines[-15:])
        return bool(_RE_APPROVE_PROMPT.search(tail))

    def is_user_question(self, content: str) -> bool:
        lines = content.strip().splitlines()
        tail = "\n".join(lines[-15:])
        return bool(_RE_AWAITING.search(tail))

    def get_choice_summary(self, content: str) -> str:
        if _RE_APPROVE_PROMPT.search(content):
            return "Approve? (y/n/always)"
        return ""

    def safe_tool_patterns(self) -> re.Pattern[str]:
        return _SAFE_PATTERNS

    def env_strip_prefixes(self) -> tuple[str, ...]:
        return ("GEMINI", "GOOGLE_API")

    def approval_response(self, approve: bool = True) -> str:
        return "y\r" if approve else "n\r"

    def session_dir(self, worker_path: str) -> Path | None:
        # Gemini stores sessions in ~/.gemini/tmp/<project_hash>/
        # Exact hash algorithm TBD
        return None

    def has_idle_prompt(self, content: str) -> bool:
        lines = content.strip().splitlines()
        if not lines:
            return False
        return bool(_RE_GEMINI_PROMPT.search("\n".join(lines[-5:])))

    def has_empty_prompt(self, content: str) -> bool:
        return self.has_idle_prompt(content)

    @property
    def supports_resume(self) -> bool:
        return True
