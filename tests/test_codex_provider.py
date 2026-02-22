"""Tests for CodexProvider — state detection and CLI command generation."""

import json

from swarm.providers.codex import CodexProvider
from swarm.worker.worker import WorkerState

_provider = CodexProvider()


# --- classify_output ---


class TestCodexClassifyOutput:
    def test_busy_triangle_right_filled_is_buzzing(self):
        content = "Working on task...\n▶ Running command"
        assert _provider.classify_output("codex", content) == WorkerState.BUZZING

    def test_busy_triangle_right_outline_is_buzzing(self):
        content = "Processing...\n▷ Thinking"
        assert _provider.classify_output("codex", content) == WorkerState.BUZZING

    def test_idle_diamond_is_resting(self):
        content = "Task complete.\n◇ Ready for input"
        assert _provider.classify_output("codex", content) == WorkerState.RESTING

    def test_idle_square_is_resting(self):
        content = "Done.\n□ Waiting"
        assert _provider.classify_output("codex", content) == WorkerState.RESTING

    def test_busy_takes_priority_over_idle(self):
        """When both busy and idle icons in tail, busy wins (checked first)."""
        content = "◇ previous idle\n▶ now running"
        assert _provider.classify_output("codex", content) == WorkerState.BUZZING

    def test_idle_in_scrollback_busy_near_bottom(self):
        """Idle icon in scrollback should not override busy icon near bottom."""
        content = "◇ old idle\n" + "processing...\n" * 5 + "▶ active"
        assert _provider.classify_output("codex", content) == WorkerState.BUZZING

    def test_non_shell_command_with_idle_icon(self):
        """Non-shell foreground command with idle icon should still be RESTING."""
        content = "output\n◇ ready"
        assert _provider.classify_output("node", content) == WorkerState.RESTING

    def test_idle_icon_beyond_30_line_tail(self):
        """Idle icon more than 30 lines from bottom should not be detected."""
        content = "◇ old idle\n" + "other output\n" * 35
        assert _provider.classify_output("codex", content) == WorkerState.BUZZING


# --- has_choice_prompt ---


class TestCodexHasChoicePrompt:
    def test_always_false(self):
        """Codex uses Ratatui widgets — PTY text detection TBD."""
        content = "Approve this action? [y/n]"
        assert _provider.has_choice_prompt(content) is False


# --- get_choice_summary ---


class TestCodexGetChoiceSummary:
    def test_always_empty(self):
        content = "Some approval prompt"
        assert _provider.get_choice_summary(content) == ""


# --- is_user_question ---


class TestCodexIsUserQuestion:
    def test_always_false(self):
        content = "How would you like to proceed?"
        assert _provider.is_user_question(content) is False


# --- has_idle_prompt (base class default) ---


class TestCodexHasIdlePrompt:
    def test_always_false(self):
        """Codex inherits base class default — always False."""
        assert _provider.has_idle_prompt("◇ ready") is False


# --- has_empty_prompt (base class default) ---


class TestCodexHasEmptyPrompt:
    def test_always_false(self):
        """Codex inherits base class default — always False."""
        assert _provider.has_empty_prompt("◇") is False


# --- worker_command ---


class TestCodexWorkerCommand:
    def test_always_includes_no_alt_screen(self):
        """--no-alt-screen is critical for PTY text detection."""
        assert _provider.worker_command(resume=True) == ["codex", "--no-alt-screen"]

    def test_resume_flag_ignored(self):
        """Codex doesn't support --resume, command is the same either way."""
        assert _provider.worker_command(resume=True) == _provider.worker_command(resume=False)


# --- headless_command ---


class TestCodexHeadlessCommand:
    def test_basic(self):
        cmd = _provider.headless_command("hello world")
        assert cmd == ["codex", "exec", "hello world"]

    def test_with_json_format(self):
        cmd = _provider.headless_command("check status", output_format="json")
        assert cmd == ["codex", "exec", "check status", "--json"]

    def test_text_format_no_flag(self):
        cmd = _provider.headless_command("do stuff", output_format="text")
        assert "--json" not in cmd

    def test_session_id_ignored(self):
        """Codex doesn't support --resume."""
        cmd = _provider.headless_command("do stuff", session_id="abc123")
        assert "--resume" not in cmd
        assert "abc123" not in cmd

    def test_max_turns_ignored(self):
        """Codex doesn't support --max-turns."""
        cmd = _provider.headless_command("do stuff", max_turns=10)
        assert "--max-turns" not in cmd


# --- parse_headless_response ---


class TestCodexParseHeadlessResponse:
    def test_extracts_last_agent_message(self):
        events = [
            {"type": "item.completed", "item": {"type": "agent_message", "text": "First response"}},
            {"type": "item.completed", "item": {"type": "tool_call", "text": "ls -la"}},
            {"type": "item.completed", "item": {"type": "agent_message", "text": "Final answer"}},
        ]
        stdout = "\n".join(json.dumps(e) for e in events).encode()
        text, session_id = _provider.parse_headless_response(stdout)
        assert text == "Final answer"
        assert session_id is None

    def test_falls_back_to_raw_text(self):
        """Non-JSONL output returns raw text."""
        text, session_id = _provider.parse_headless_response(b"plain text output")
        assert text == "plain text output"
        assert session_id is None

    def test_skips_non_agent_messages(self):
        events = [
            {"type": "item.completed", "item": {"type": "tool_call", "text": "git status"}},
        ]
        stdout = "\n".join(json.dumps(e) for e in events).encode()
        text, session_id = _provider.parse_headless_response(stdout)
        # No agent_message found — falls back to raw text
        assert "tool_call" in text

    def test_handles_mixed_valid_invalid_jsonl(self):
        lines = [
            '{"type": "item.completed", "item": {"type": "agent_message", "text": "good"}}',
            "not valid json",
            '{"type": "item.completed", "item": {"type": "agent_message", "text": "last"}}',
        ]
        stdout = "\n".join(lines).encode()
        text, _ = _provider.parse_headless_response(stdout)
        assert text == "last"

    def test_empty_stdout(self):
        text, session_id = _provider.parse_headless_response(b"")
        assert text == ""
        assert session_id is None

    def test_handles_invalid_utf8(self):
        text, _ = _provider.parse_headless_response(b"valid \xff invalid")
        assert "valid" in text

    def test_session_id_always_none(self):
        """Codex doesn't support session continuity."""
        _, session_id = _provider.parse_headless_response(b"anything")
        assert session_id is None


# --- misc properties ---


class TestCodexMiscProperties:
    def test_name(self):
        assert _provider.name == "codex"

    def test_env_strip_prefixes(self):
        assert _provider.env_strip_prefixes() == ("OPENAI",)

    def test_supports_resume(self):
        assert _provider.supports_resume is False

    def test_supports_hooks(self):
        assert _provider.supports_hooks is False

    def test_supports_slash_commands(self):
        assert _provider.supports_slash_commands is False

    def test_safe_tool_patterns_matches_read_only(self):
        pattern = _provider.safe_tool_patterns()
        assert pattern.search("shell(ls -la)")
        assert pattern.search("file_read(foo.py)")
        assert pattern.search("file_search(query)")
        assert not pattern.search("shell(rm -rf /)")
