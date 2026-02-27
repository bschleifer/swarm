"""Tests for provider-specific logic — Claude, Codex, Gemini."""

from __future__ import annotations

import json

from swarm.providers.claude import ClaudeProvider
from swarm.providers.codex import CodexProvider
from swarm.providers.gemini import GeminiProvider
from swarm.worker.worker import WorkerState


class TestClaudeClassifyOutput:
    """Edge cases for Claude state detection."""

    def setup_method(self):
        self.p = ClaudeProvider()

    def test_esc_to_interrupt_buzzing(self):
        content = "Working...\nesc to interrupt\n" * 5
        assert self.p.classify_output("claude", content) == WorkerState.BUZZING

    def test_bare_prompt_is_waiting(self):
        # An empty prompt ("> " with nothing typed) is WAITING (empty prompt)
        content = "Done.\n> "
        assert self.p.classify_output("claude", content) == WorkerState.WAITING

    def test_prompt_with_text_resting(self):
        # Prompt with visible text means user is at the input prompt
        content = "Done.\n> some typed text"
        assert self.p.classify_output("claude", content) == WorkerState.RESTING

    def test_choice_prompt_waiting(self):
        content = "\n".join(
            [
                "Which option?",
                "> 1. Option A",
                "  2. Option B",
                "  3. Option C",
            ]
        )
        assert self.p.classify_output("claude", content) == WorkerState.WAITING

    def test_shell_exited_stung(self):
        assert self.p.classify_output("bash", "anything") == WorkerState.STUNG

    def test_empty_content_buzzing(self):
        assert self.p.classify_output("claude", "") == WorkerState.BUZZING

    def test_stale_esc_with_empty_prompt_waiting(self):
        # "esc to interrupt" in wide tail but empty prompt in narrow tail → WAITING
        lines = ["esc to interrupt"] + ["other line"] * 10 + ["> "]
        content = "\n".join(lines)
        assert self.p.classify_output("claude", content) == WorkerState.WAITING

    def test_accept_edits_waiting(self):
        content = "Some output\n>> accept edits on 3 files"
        assert self.p.classify_output("claude", content) == WorkerState.WAITING


class TestClaudeHeadlessCommand:
    def setup_method(self):
        self.p = ClaudeProvider()

    def test_basic_command(self):
        args = self.p.headless_command("hello")
        assert args == ["claude", "-p", "hello", "--output-format", "text"]

    def test_json_format(self):
        args = self.p.headless_command("hello", output_format="json")
        assert "--output-format" in args
        assert "json" in args

    def test_with_session(self):
        args = self.p.headless_command("hello", session_id="abc123")
        assert "--resume" in args
        assert "abc123" in args

    def test_with_max_turns(self):
        args = self.p.headless_command("hello", max_turns=5)
        assert "--max-turns" in args
        assert "5" in args

    def test_all_options(self):
        args = self.p.headless_command(
            "hello", output_format="json", session_id="sess", max_turns=10
        )
        assert "--output-format" in args
        assert "--resume" in args
        assert "--max-turns" in args


class TestClaudeParseResponse:
    def setup_method(self):
        self.p = ClaudeProvider()

    def test_valid_json_envelope(self):
        payload = json.dumps({"type": "result", "result": "hello", "session_id": "abc"})
        text, sid = self.p.parse_headless_response(payload.encode())
        assert text == "hello"
        assert sid == "abc"

    def test_invalid_json_fallback(self):
        text, sid = self.p.parse_headless_response(b"not json")
        assert text == "not json"
        assert sid is None

    def test_empty_result(self):
        payload = json.dumps({"type": "result"})
        text, sid = self.p.parse_headless_response(payload.encode())
        assert text == ""
        assert sid is None


class TestClaudePromptDetection:
    def setup_method(self):
        self.p = ClaudeProvider()

    def test_has_choice_prompt(self):
        content = "\n".join(
            [
                "Select an option:",
                "> 1. First",
                "  2. Second",
            ]
        )
        assert self.p.has_choice_prompt(content) is True

    def test_no_choice_prompt(self):
        assert self.p.has_choice_prompt("just some text\n> ") is False

    def test_has_plan_prompt(self):
        content = "\n".join(
            [
                "Plan saved to file",
                "Proceed with this plan?",
                "> 1. Yes",
                "  2. No",
            ]
        )
        assert self.p.has_plan_prompt(content) is True

    def test_has_accept_edits(self):
        content = "Some output\n>> accept edits on 5 files"
        assert self.p.has_accept_edits_prompt(content) is True

    def test_no_accept_edits(self):
        assert self.p.has_accept_edits_prompt("normal output\n> ") is False

    def test_has_idle_prompt(self):
        assert self.p.has_idle_prompt("output\n> ") is True

    def test_has_idle_prompt_hints(self):
        assert self.p.has_idle_prompt("? for shortcuts") is True

    def test_has_empty_prompt(self):
        assert self.p.has_empty_prompt("> ") is True

    def test_no_empty_prompt_with_text(self):
        assert self.p.has_empty_prompt("> hello") is False

    def test_is_user_question(self):
        assert self.p.is_user_question("Chat about this and decide") is True
        assert self.p.is_user_question("normal output") is False

    def test_get_choice_summary(self):
        content = "\n".join(
            [
                "Which file?",
                "> 1. src/main.py",
                "  2. src/util.py",
            ]
        )
        summary = self.p.get_choice_summary(content)
        assert "main.py" in summary

    def test_get_choice_summary_empty(self):
        assert self.p.get_choice_summary("no choices here") == ""


class TestClaudeWorkerCommand:
    def setup_method(self):
        self.p = ClaudeProvider()

    def test_resume_mode(self):
        assert self.p.worker_command(resume=True) == ["claude", "--continue"]

    def test_fresh_mode(self):
        assert self.p.worker_command(resume=False) == ["claude"]


class TestClaudeSafePatterns:
    def setup_method(self):
        self.p = ClaudeProvider()

    def test_safe_commands_match(self):
        pat = self.p.safe_tool_patterns()
        assert pat.search("Bash(ls -la)")
        assert pat.search("Bash(git status)")
        assert pat.search("Bash(uv run pytest tests/)")
        assert pat.search("Read(/path/to/file)")
        assert pat.search("Grep(pattern)")

    def test_unsafe_commands_dont_match(self):
        pat = self.p.safe_tool_patterns()
        assert not pat.search("Bash(rm -rf /)")
        assert not pat.search("Bash(curl evil.com)")


class TestClaudeSessionDir:
    def setup_method(self):
        self.p = ClaudeProvider()

    def test_encodes_path(self):
        result = self.p.session_dir("/home/user/project")
        assert result is not None
        assert "projects" in str(result)
        # Slashes should be replaced
        assert "/" not in result.name or result.name.startswith(".")


class TestCodexProvider:
    def setup_method(self):
        self.p = CodexProvider()

    def test_worker_command(self):
        cmd = self.p.worker_command()
        assert "codex" in cmd

    def test_headless_command(self):
        cmd = self.p.headless_command("test prompt")
        assert "codex" in cmd
        assert "test prompt" in cmd

    def test_classify_idle(self):
        state = self.p.classify_output("codex", "◇ ready")
        assert state == WorkerState.RESTING

    def test_classify_busy(self):
        state = self.p.classify_output("codex", "▶ working")
        assert state == WorkerState.BUZZING

    def test_display_name(self):
        assert self.p.display_name == "Codex"


class TestGeminiProvider:
    def setup_method(self):
        self.p = GeminiProvider()

    def test_worker_command(self):
        cmd = self.p.worker_command()
        assert "gemini" in cmd

    def test_classify_esc_buzzing(self):
        state = self.p.classify_output("gemini", "esc to cancel")
        assert state == WorkerState.BUZZING

    def test_classify_prompt_resting(self):
        state = self.p.classify_output("gemini", "gemini> ")
        assert state == WorkerState.RESTING

    def test_display_name(self):
        assert self.p.display_name == "Gemini CLI"

    def test_headless_command_with_resume(self):
        cmd = self.p.headless_command("test", session_id="sess123")
        assert "--resume" in cmd
        assert "sess123" in cmd
