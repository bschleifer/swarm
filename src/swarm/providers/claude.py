"""Claude Code provider — extracts all Claude-specific CLI behavior."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from swarm.providers.base import SAFE_GIT_SUBCMDS, SAFE_SHELL_CMDS, LLMProvider
from swarm.worker.worker import TokenUsage, WorkerState

# Pre-compiled patterns — these run every poll cycle for every worker
_RE_PROMPT = re.compile(r"^\s*[>❯]", re.MULTILINE)
_RE_CURSOR_OPTION = re.compile(r"^\s*[>❯]\s*\d+\.", re.MULTILINE)
_RE_OTHER_OPTION = re.compile(r"^\s+\d+\.", re.MULTILINE)
_RE_HINTS = re.compile(r"(\? for shortcuts|ctrl\+t to hide)", re.IGNORECASE)
_RE_EMPTY_PROMPT = re.compile(r"^[>❯]\s*$")
_RE_ACCEPT_EDITS = re.compile(r">>\s*accept edits on", re.IGNORECASE)
_RE_PLAN_MARKERS = re.compile(
    r"plan file|plan saved|"
    r"proceed with (?:this|the) plan|"
    r"approve (?:this|the) plan",
    re.IGNORECASE,
)

_BUILTIN_SAFE_PATTERNS = re.compile(
    rf"Bash\(.*({SAFE_SHELL_CMDS})\b"
    rf"|Bash\(.*git\s+({SAFE_GIT_SUBCMDS})\b"
    r"|Bash\(.*uv\s+run\s+(pytest|ruff)\b"
    r"|Glob\("
    r"|Grep\("
    r"|Read\("
    r"|WebSearch\("
    r"|WebFetch\(",
    re.IGNORECASE,
)


class ClaudeProvider(LLMProvider):
    """Claude Code CLI provider."""

    @property
    def name(self) -> str:
        return "claude"

    def worker_command(self, resume: bool = True) -> list[str]:
        if resume:
            return ["claude", "--continue"]
        return ["claude"]

    def headless_command(
        self,
        prompt: str,
        output_format: str = "text",
        max_turns: int | None = None,
        session_id: str | None = None,
    ) -> list[str]:
        args = ["claude", "-p", prompt, "--output-format", output_format]
        if session_id:
            args.extend(["--resume", session_id])
        if max_turns is not None:
            args.extend(["--max-turns", str(max_turns)])
        return args

    def parse_headless_response(self, stdout: bytes) -> tuple[str, str | None]:
        """Parse Claude's JSON envelope: {"type":"result","result":"...","session_id":"..."}."""
        try:
            result = json.loads(stdout.decode())
            if isinstance(result, dict):
                text = result.get("result", "")
                session_id = result.get("session_id")
                return str(text), session_id
        except (json.JSONDecodeError, UnicodeDecodeError):
            pass
        return stdout.decode(errors="replace"), None

    def classify_output(self, command: str, content: str) -> WorkerState:
        if self._is_shell_exited(command):
            return WorkerState.STUNG

        tail_wide = self._get_tail(content, 30)
        tail_narrow = self._get_tail(content, 5)

        if "esc to interrupt" in tail_wide:
            return WorkerState.BUZZING

        if _RE_PROMPT.search(tail_narrow) or "? for shortcuts" in tail_narrow:
            if (
                self.has_choice_prompt(content)
                or self.has_plan_prompt(content)
                or self.has_empty_prompt(content)
                or self.has_accept_edits_prompt(content)
            ):
                return WorkerState.WAITING
            return WorkerState.RESTING

        if (
            self.has_choice_prompt(content)
            or self.has_plan_prompt(content)
            or self.has_accept_edits_prompt(content)
        ):
            return WorkerState.WAITING

        return WorkerState.BUZZING

    def has_choice_prompt(self, content: str) -> bool:
        tail = self._get_tail(content, 25)
        if not tail:
            return False
        return bool(_RE_CURSOR_OPTION.search(tail)) and bool(_RE_OTHER_OPTION.search(tail))

    def is_user_question(self, content: str) -> bool:
        tail_lower = self._get_tail(content, 15).lower()
        return "chat about this" in tail_lower or "type something" in tail_lower

    def get_choice_summary(self, content: str) -> str:
        tail_str = self._get_tail(content, 25)
        if not tail_str:
            return ""
        tail = tail_str.splitlines()
        cursor_idx = None
        selected = ""
        for i in range(len(tail) - 1, -1, -1):
            if _RE_CURSOR_OPTION.match(tail[i]):
                cursor_idx = i
                selected = tail[i].lstrip().lstrip(">❯").strip()
                break
        if not selected:
            return ""
        question = ""
        for i in range(cursor_idx - 1, -1, -1):
            stripped = tail[i].strip()
            if stripped and not _RE_OTHER_OPTION.match(tail[i]):
                question = stripped
                break
        if question:
            return f'"{question}" → {selected}'
        return selected

    def safe_tool_patterns(self) -> re.Pattern[str]:
        return _BUILTIN_SAFE_PATTERNS

    def env_strip_prefixes(self) -> tuple[str, ...]:
        return ("CLAUDE",)

    def approval_response(self, approve: bool = True) -> str:
        return "\r" if approve else "\x1b"  # Enter to approve, Esc to reject

    def session_dir(self, worker_path: str) -> Path | None:
        encoded = worker_path.replace("/", "-")
        return Path.home() / ".claude" / "projects" / encoded

    def has_plan_prompt(self, content: str) -> bool:
        tail = self._get_tail(content, 30)
        if not tail:
            return False
        if not (bool(_RE_CURSOR_OPTION.search(tail)) and bool(_RE_OTHER_OPTION.search(tail))):
            return False
        return bool(_RE_PLAN_MARKERS.search(tail))

    def has_accept_edits_prompt(self, content: str) -> bool:
        tail = self._get_tail(content, 5)
        if not tail:
            return False
        return bool(_RE_ACCEPT_EDITS.search(tail))

    def has_idle_prompt(self, content: str) -> bool:
        tail = self._get_tail(content, 5)
        if not tail:
            return False
        if _RE_PROMPT.search(tail):
            return True
        if _RE_HINTS.search(tail):
            return True
        return False

    def has_empty_prompt(self, content: str) -> bool:
        tail = self._get_tail(content, 1)
        if not tail:
            return False
        return bool(_RE_EMPTY_PROMPT.match(tail.strip()))

    @property
    def supports_slash_commands(self) -> bool:
        return True

    @property
    def supports_hooks(self) -> bool:
        return True

    @property
    def supports_resume(self) -> bool:
        return True

    @property
    def display_name(self) -> str:
        return "Claude Code"

    @property
    def supports_max_turns(self) -> bool:
        return True

    @property
    def supports_json_output(self) -> bool:
        return True

    def parse_usage(self, result: dict[str, Any]) -> TokenUsage | None:
        usage = result.get("usage", {})
        if not isinstance(usage, dict):
            return None
        return TokenUsage(
            input_tokens=usage.get("input_tokens", 0),
            output_tokens=usage.get("output_tokens", 0),
            cache_read_tokens=usage.get("cache_read_input_tokens", 0),
            cache_creation_tokens=usage.get("cache_creation_input_tokens", 0),
            cost_usd=result.get("total_cost_usd", 0.0) or 0.0,
        )
