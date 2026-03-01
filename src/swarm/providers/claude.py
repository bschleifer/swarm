"""Claude Code provider — extracts all Claude-specific CLI behavior."""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any

from swarm.providers.base import (
    SAFE_GIT_SUBCMDS,
    SAFE_SHELL_CMDS,
    TAIL_LAST_LINE,
    TAIL_MEDIUM,
    TAIL_NARROW,
    TAIL_WIDE,
    LLMProvider,
)
from swarm.providers.events import EventType, TerminalEvent
from swarm.providers.styled import StyledContent
from swarm.worker.worker import TokenUsage, WorkerState

_style_log = logging.getLogger("swarm.style_discovery")
_STYLE_DISCOVERY = os.environ.get("SWARM_STYLE_DISCOVERY", "") == "1"

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

# Tool name extraction — captures the tool name from approval prompts.
_RE_TOOL_NAME = re.compile(
    r"(Bash|Edit|Write|Read|Glob|Grep|NotebookEdit|WebSearch|WebFetch|Agent|Skill)"
    r"(?:\s+\w|\()",
    re.MULTILINE,
)

_BUILTIN_SAFE_PATTERNS = re.compile(
    # Old format: Bash(ls ...) — tool-call style
    rf"Bash\(.*({SAFE_SHELL_CMDS})\b"
    rf"|Bash\(.*git\s+({SAFE_GIT_SUBCMDS})\b"
    r"|Bash\(.*uv\s+run\s+(pytest|ruff)\b"
    # New format: "Bash command\n  ls ..." — indented command on next line
    rf"|Bash command\s+({SAFE_SHELL_CMDS})\b"
    rf"|Bash command\s+git\s+({SAFE_GIT_SUBCMDS})\b"
    r"|Bash command\s+uv\s+run\s+(pytest|ruff)\b"
    # Tool patterns — both old Foo(...) and new "Foo " header formats
    r"|Glob\(|Glob "
    r"|Grep\(|Grep "
    r"|Read\(|Read file"
    r"|WebSearch\(|WebSearch "
    r"|WebFetch\(|WebFetch ",
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

        tail_wide = self._get_tail(content, TAIL_WIDE)
        tail_narrow = self._get_tail(content, TAIL_NARROW)

        # When "esc to interrupt" is in the wide tail but NOT the narrow tail,
        # it may be stale (from before an interruption).  Check the very last
        # line and actionable prompts — a real prompt on the last line or an
        # accept-edits/choice prompt means the worker has transitioned.
        if "esc to interrupt" in tail_wide and "esc to interrupt" not in tail_narrow:
            tail_last = self._get_tail(content, TAIL_LAST_LINE)
            if _RE_PROMPT.search(tail_last) or "? for shortcuts" in tail_last:
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
        tail = self._get_tail(content, TAIL_WIDE)
        if not tail:
            return False
        return bool(_RE_CURSOR_OPTION.search(tail)) and bool(_RE_OTHER_OPTION.search(tail))

    def is_user_question(self, content: str) -> bool:
        tail_lower = self._get_tail(content, TAIL_MEDIUM).lower()
        return "chat about this" in tail_lower or "type something" in tail_lower

    def get_choice_summary(self, content: str) -> str:
        tail_str = self._get_tail(content, TAIL_WIDE)
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
        tail = self._get_tail(content, TAIL_WIDE)
        if not tail:
            return False
        if not (bool(_RE_CURSOR_OPTION.search(tail)) and bool(_RE_OTHER_OPTION.search(tail))):
            return False
        return bool(_RE_PLAN_MARKERS.search(tail))

    def has_accept_edits_prompt(self, content: str) -> bool:
        tail = self._get_tail(content, TAIL_NARROW)
        if not tail:
            return False
        return bool(_RE_ACCEPT_EDITS.search(tail))

    def has_idle_prompt(self, content: str) -> bool:
        tail = self._get_tail(content, TAIL_NARROW)
        if not tail:
            return False
        if _RE_PROMPT.search(tail):
            return True
        if _RE_HINTS.search(tail):
            return True
        return False

    def has_empty_prompt(self, content: str) -> bool:
        tail = self._get_tail(content, TAIL_LAST_LINE)
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

    # --- Structured event parsing ---

    def parse_events(self, content: str) -> list[TerminalEvent]:
        """Extract structured events from Claude Code terminal output.

        Returns ALL matching events — content can contain multiple signals
        (e.g. a THINKING indicator + a TOOL_CALL + a CHOICE prompt).
        """
        events: list[TerminalEvent] = []
        tail_wide = self._get_tail(content, TAIL_WIDE)
        tail_narrow = self._get_tail(content, TAIL_NARROW)

        # Thinking indicator
        if "esc to interrupt" in tail_wide:
            events.append(TerminalEvent(EventType.THINKING, "esc to interrupt"))

        # Choice menu (numbered options with cursor)
        if self.has_choice_prompt(content):
            summary = self.get_choice_summary(content)
            meta: dict[str, object] = {"summary": summary} if summary else {}
            events.append(TerminalEvent(EventType.CHOICE, metadata=meta))

        # Plan approval
        if self.has_plan_prompt(content):
            events.append(TerminalEvent(EventType.PLAN))

        # Accept edits
        if self.has_accept_edits_prompt(content):
            has_bash = "bash" in tail_narrow.lower()
            events.append(TerminalEvent(EventType.ACCEPT_EDITS, metadata={"has_bash": has_bash}))

        # User question (AskUserQuestion prompt)
        if self.is_user_question(content):
            events.append(TerminalEvent(EventType.USER_QUESTION))

        # Tool call detection
        tool_match = _RE_TOOL_NAME.search(tail_wide)
        if tool_match:
            events.append(TerminalEvent(EventType.TOOL_CALL, tool_name=tool_match.group(1)))

        # Idle prompt
        if self.has_idle_prompt(content):
            meta_prompt: dict[str, object] = {}
            if self.has_empty_prompt(content):
                meta_prompt["empty"] = True
            events.append(TerminalEvent(EventType.PROMPT, metadata=meta_prompt))

        # Fallback: if nothing matched, emit UNKNOWN
        if not events:
            events.append(TerminalEvent(EventType.UNKNOWN, content))

        return events

    def classify_with_events(
        self, command: str, content: str
    ) -> tuple[WorkerState, list[TerminalEvent]]:
        """Classify worker state and parse events in one pass.

        Calls parse_events() once, then derives state from the events
        combined with the existing classify_output() logic to avoid
        double-parsing the content.
        """
        state = self.classify_output(command, content)
        events = self.parse_events(content)
        return state, events

    # --- Style-aware classification ---

    def classify_styled_output(self, command: str, styled: StyledContent) -> WorkerState:
        """Classify worker state using style data to reduce false positives.

        Style checks can only *tighten* detection (reject a text match),
        never *loosen* it.  If no style signal matches, falls back to
        the text-only ``classify_output()``.
        """
        if self._is_shell_exited(command):
            return WorkerState.STUNG

        if not styled.has_styles():
            return self.classify_output(command, styled.text)

        text = styled.text
        tail_wide = self._get_tail(text, TAIL_WIDE)

        # BUZZING: require "esc to interrupt" to be dim-styled
        if "esc to interrupt" in tail_wide:
            if styled.find_styled_text("esc to interrupt", dim=True):
                if _STYLE_DISCOVERY:
                    self._log_style_discovery(styled, "esc to interrupt", "BUZZING (dim)")
                return self._classify_after_buzzing(text)
            # Text matches but style doesn't — don't trust it as BUZZING,
            # fall through to prompt/choice checks

        # Prompt: require styled (non-default fg) prompt character
        if self._has_styled_prompt(styled):
            if (
                self.has_choice_prompt(text)
                or self.has_plan_prompt(text)
                or self.has_empty_prompt(text)
                or self.has_accept_edits_prompt(text)
            ):
                return WorkerState.WAITING
            return WorkerState.RESTING

        # Choice cursor: styled cursor character
        if self._has_styled_choice(styled):
            return WorkerState.WAITING

        # Accept edits (text-only, no style confirmation needed)
        if self.has_accept_edits_prompt(text):
            return WorkerState.WAITING

        # No styled signal matched — fall back to text-only
        return self.classify_output(command, text)

    def classify_styled_with_events(
        self, command: str, styled: StyledContent
    ) -> tuple[WorkerState, list[TerminalEvent]]:
        """Classify state and parse events from styled content."""
        state = self.classify_styled_output(command, styled)
        events = self.parse_events(styled.text)
        return state, events

    def _classify_after_buzzing(self, text: str) -> WorkerState:
        """Determine state when dim 'esc to interrupt' confirms BUZZING.

        Mirrors the stale-buzzing logic in classify_output — if "esc to
        interrupt" is only in the wide tail (not the narrow tail), check
        for prompts that indicate the worker has actually transitioned.
        """
        tail_wide = self._get_tail(text, TAIL_WIDE)
        tail_narrow = self._get_tail(text, TAIL_NARROW)

        if "esc to interrupt" in tail_wide and "esc to interrupt" not in tail_narrow:
            tail_last = self._get_tail(text, TAIL_LAST_LINE)
            if _RE_PROMPT.search(tail_last) or "? for shortcuts" in tail_last:
                if (
                    self.has_choice_prompt(text)
                    or self.has_plan_prompt(text)
                    or self.has_empty_prompt(text)
                    or self.has_accept_edits_prompt(text)
                ):
                    return WorkerState.WAITING
                return WorkerState.RESTING
            if (
                self.has_choice_prompt(text)
                or self.has_plan_prompt(text)
                or self.has_accept_edits_prompt(text)
            ):
                return WorkerState.WAITING

        return WorkerState.BUZZING

    def _has_styled_prompt(self, styled: StyledContent) -> bool:
        """Check for styled Claude Code prompt area (hint line with non-default fg).

        Claude Code's ``❯`` prompt character uses default fg, so we can't
        distinguish it from ``>`` in diff output by color alone.  Instead we
        look for the *hint line* that always accompanies the idle prompt —
        ``? for shortcuts`` or ``ctrl+t to hide`` — which is rendered in a
        non-default fg (typically gray/``999999``).
        """
        if not styled.rows:
            return False
        found = styled.find_styled_text(
            "? for shortcuts", fg="!default"
        ) or styled.find_styled_text("ctrl+t to hide", fg="!default")
        if found and _STYLE_DISCOVERY:
            self._log_style_discovery(styled, "? for shortcuts", "prompt-hint")
        return found

    def _has_styled_choice(self, styled: StyledContent) -> bool:
        """Check for a choice cursor (> N.) with non-default fg color."""
        if not styled.rows:
            return False
        # Check last 25 rows for styled choice cursor
        check_rows = styled.rows[-25:]
        has_other = False
        has_cursor = False
        for row_text, row_styles in check_rows:
            if _RE_OTHER_OPTION.search(row_text):
                has_other = True
            m = _RE_CURSOR_OPTION.search(row_text)
            if m:
                cursor_pos = m.start()
                # Skip whitespace to find the > or ❯
                while cursor_pos < len(row_text) and row_text[cursor_pos] == " ":
                    cursor_pos += 1
                if cursor_pos < len(row_styles) and row_styles[cursor_pos].fg != "default":
                    has_cursor = True
                    if _STYLE_DISCOVERY:
                        ch = row_text[cursor_pos]
                        s = row_styles[cursor_pos]
                        _style_log.info(
                            "STYLE_DISCOVERY [choice] %r at col %d: fg=%s bg=%s bold=%s dim=%s",
                            ch,
                            cursor_pos,
                            s.fg,
                            s.bg,
                            s.bold,
                            s.dim,
                        )
        return has_cursor and has_other

    @staticmethod
    def _log_style_discovery(styled: StyledContent, needle: str, context: str) -> None:
        """Log style values at detection points for discovery."""
        for row_text, row_styles in styled.rows:
            idx = row_text.find(needle)
            if idx < 0 or idx >= len(row_styles):
                continue
            s = row_styles[idx]
            _style_log.info(
                "STYLE_DISCOVERY [%s] %r at col %d: fg=%s bg=%s bold=%s dim=%s",
                context,
                needle,
                idx,
                s.fg,
                s.bg,
                s.bold,
                s.dim,
            )
            break
