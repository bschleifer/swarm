"""Tests for GeminiProvider — state detection and CLI command generation."""

from swarm.providers.gemini import GeminiProvider
from swarm.worker.worker import WorkerState

_provider = GeminiProvider()


# --- classify_output ---


class TestGeminiClassifyOutput:
    def test_esc_to_cancel_is_buzzing(self):
        content = "Working on task...\nesc to cancel\n"
        assert _provider.classify_output("gemini", content) == WorkerState.BUZZING

    def test_spinner_characters_are_buzzing(self):
        for char in ("⠏", "💬"):
            content = f"Processing {char} thinking..."
            assert _provider.classify_output("gemini", content) == WorkerState.BUZZING

    def test_approve_prompt_is_waiting(self):
        content = "Run shell command: rm -rf /tmp/junk\nApprove? (y/n/always)"
        assert _provider.classify_output("gemini", content) == WorkerState.WAITING

    def test_awaiting_direction_is_waiting(self):
        content = "Task complete.\nAwaiting Further Direction"
        assert _provider.classify_output("gemini", content) == WorkerState.WAITING

    def test_gemini_prompt_is_resting(self):
        content = "Some previous output\ngemini>"
        assert _provider.classify_output("gemini", content) == WorkerState.RESTING

    def test_gemini_prompt_with_whitespace_is_resting(self):
        content = "Output done.\ngemini>   "
        assert _provider.classify_output("gemini", content) == WorkerState.RESTING

    def test_gemini_prompt_in_scrollback_busy_near_bottom(self):
        """gemini> in scrollback should not cause RESTING if busy text is near bottom."""
        content = "gemini>\nuser prompt\nProcessing your request...\n⠏ thinking...\nmore output\n"
        assert _provider.classify_output("gemini", content) == WorkerState.BUZZING

    def test_non_shell_command_with_gemini_prompt(self):
        """Non-shell foreground command with gemini> prompt should still be RESTING."""
        content = "previous output\ngemini>"
        assert _provider.classify_output("node", content) == WorkerState.RESTING

    def test_esc_to_cancel_in_scrollback_beyond_30_lines_not_buzzing(self):
        """Stale 'esc to cancel' more than 30 lines back should not be BUZZING."""
        content = "some output\nesc to cancel\n" + "more output\n" * 32 + "gemini>"
        assert _provider.classify_output("gemini", content) == WorkerState.RESTING

    def test_approve_prompt_case_insensitive(self):
        content = "approve? (Y/N/Always)"
        assert _provider.classify_output("gemini", content) == WorkerState.WAITING


# --- has_choice_prompt ---


class TestGeminiHasChoicePrompt:
    def test_approve_prompt_present(self):
        content = "Run command: ls -la\nApprove? (y/n/always)"
        assert _provider.has_choice_prompt(content) is True

    def test_no_approve_prompt(self):
        content = "Just normal output\ngemini>"
        assert _provider.has_choice_prompt(content) is False

    def test_approve_in_scrollback_above_15_lines(self):
        """Approve prompt more than 15 lines from bottom should not match."""
        content = "Approve? (y/n/always)\n" + "other output\n" * 20
        assert _provider.has_choice_prompt(content) is False


# --- get_choice_summary ---


class TestGeminiGetChoiceSummary:
    def test_with_approve_prompt(self):
        content = "Run: rm -rf /tmp/junk\nApprove? (y/n/always)"
        assert _provider.get_choice_summary(content) == "Approve? (y/n/always)"

    def test_no_prompt(self):
        content = "Just normal output"
        assert _provider.get_choice_summary(content) == ""


# --- is_user_question ---


class TestGeminiIsUserQuestion:
    def test_awaiting_further_direction(self):
        content = "Task done.\nAwaiting Further Direction"
        assert _provider.is_user_question(content) is True

    def test_normal_output(self):
        content = "Processing files...\nDone."
        assert _provider.is_user_question(content) is False

    def test_awaiting_in_scrollback_above_15_lines(self):
        """Awaiting direction more than 15 lines from bottom should not match."""
        content = "Awaiting Further Direction\n" + "other output\n" * 20
        assert _provider.is_user_question(content) is False


# --- has_idle_prompt ---


class TestGeminiHasIdlePrompt:
    def test_gemini_bare(self):
        assert _provider.has_idle_prompt("gemini>") is True

    def test_gemini_with_whitespace(self):
        assert _provider.has_idle_prompt("gemini>   ") is True

    def test_processing_text(self):
        assert _provider.has_idle_prompt("processing...") is False

    def test_gemini_prompt_in_multiline(self):
        content = "previous output\ngemini>"
        assert _provider.has_idle_prompt(content) is True


# --- has_empty_prompt ---


class TestGeminiHasEmptyPrompt:
    def test_gemini_prompt(self):
        assert _provider.has_empty_prompt("gemini>") is True

    def test_delegates_to_has_idle(self):
        """has_empty_prompt delegates to has_idle_prompt for Gemini."""
        content = "output\ngemini>"
        assert _provider.has_empty_prompt(content) is True
        assert _provider.has_empty_prompt(content) == _provider.has_idle_prompt(content)


# --- worker_command ---


class TestGeminiWorkerCommand:
    def test_resume_true(self):
        assert _provider.worker_command(resume=True) == ["gemini", "--resume"]

    def test_resume_false(self):
        assert _provider.worker_command(resume=False) == ["gemini"]


# --- headless_command ---


class TestGeminiHeadlessCommand:
    def test_basic(self):
        cmd = _provider.headless_command("hello world")
        assert cmd == ["gemini", "-p", "hello world"]

    def test_with_format_and_session(self):
        cmd = _provider.headless_command(
            "check status",
            output_format="json",
            session_id="abc123",
        )
        assert cmd == [
            "gemini",
            "-p",
            "check status",
            "--output-format",
            "json",
            "--resume",
            "abc123",
        ]

    def test_max_turns_ignored(self):
        """Gemini CLI has no --max-turns flag; it should be silently ignored."""
        cmd = _provider.headless_command("do stuff", max_turns=10)
        assert cmd == ["gemini", "-p", "do stuff"]
        assert "--max-turns" not in cmd


# --- parse_headless_response ---


class TestGeminiParseHeadlessResponse:
    def test_returns_text_and_none_session(self):
        text, session_id = _provider.parse_headless_response(b"Hello from Gemini")
        assert text == "Hello from Gemini"
        assert session_id is None

    def test_strips_whitespace(self):
        text, session_id = _provider.parse_headless_response(b"  output  \n")
        assert text == "output"
        assert session_id is None

    def test_handles_invalid_utf8(self):
        text, _ = _provider.parse_headless_response(b"valid \xff invalid")
        assert "valid" in text


# --- misc properties ---


class TestGeminiMiscProperties:
    def test_name(self):
        assert _provider.name == "gemini"

    def test_env_strip_prefixes(self):
        assert _provider.env_strip_prefixes() == ("GEMINI", "GOOGLE_API")

    def test_supports_resume(self):
        assert _provider.supports_resume is True

    def test_supports_hooks(self):
        assert _provider.supports_hooks is False

    def test_supports_slash_commands(self):
        assert _provider.supports_slash_commands is False

    def test_safe_tool_patterns_matches_read_only(self):
        pattern = _provider.safe_tool_patterns()
        assert pattern.search("run_shell_command(ls -la)")
        assert pattern.search("ReadFile(foo.py)")
        assert pattern.search("GoogleSearch(query)")
        assert not pattern.search("run_shell_command(rm -rf /)")
