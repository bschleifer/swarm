"""Universal contract tests for all LLM providers.

Parametrized over claude/gemini/codex — each test runs once per provider.
Covers shared base-class behavior and universal classify_output contracts.
"""

import pytest

from swarm.providers import get_provider
from swarm.providers.base import LLMProvider
from swarm.worker.worker import TokenUsage, WorkerState


@pytest.fixture(params=["claude", "gemini", "codex"])
def provider(request: pytest.FixtureRequest) -> LLMProvider:
    return get_provider(request.param)


@pytest.fixture(params=["gemini", "codex"])
def non_claude_provider(request: pytest.FixtureRequest) -> LLMProvider:
    return get_provider(request.param)


# --- Universal classify_output contracts ---


class TestClassifyOutputUniversal:
    """Every provider must classify shell foreground as STUNG and unknown as BUZZING."""

    def test_shell_name_is_stung(self, provider: LLMProvider) -> None:
        for shell in ("bash", "zsh", "sh", "fish", "dash", "ksh", "csh", "tcsh"):
            assert provider.classify_output(shell, "$ ") == WorkerState.STUNG

    def test_shell_full_path_is_stung(self, provider: LLMProvider) -> None:
        assert provider.classify_output("/bin/bash", "$ ") == WorkerState.STUNG
        assert provider.classify_output("/usr/bin/zsh", "$ ") == WorkerState.STUNG

    def test_empty_content_defaults_to_buzzing(self, provider: LLMProvider) -> None:
        assert provider.classify_output(provider.name, "") == WorkerState.BUZZING

    def test_unknown_content_defaults_to_buzzing(self, provider: LLMProvider) -> None:
        assert (
            provider.classify_output(provider.name, "random stuff happening") == WorkerState.BUZZING
        )


# --- Universal base-class defaults (empty input) ---


class TestBaseDefaultsEmpty:
    """All providers return falsy/empty for empty content on optional methods."""

    def test_has_plan_prompt_empty(self, provider: LLMProvider) -> None:
        assert provider.has_plan_prompt("") is False

    def test_has_accept_edits_prompt_empty(self, provider: LLMProvider) -> None:
        assert provider.has_accept_edits_prompt("") is False

    def test_get_choice_summary_empty(self, provider: LLMProvider) -> None:
        assert provider.get_choice_summary("") == ""

    def test_is_user_question_empty(self, provider: LLMProvider) -> None:
        assert provider.is_user_question("") is False

    def test_has_choice_prompt_empty(self, provider: LLMProvider) -> None:
        assert provider.has_choice_prompt("") is False


# --- Non-Claude providers: plan/edits always False even with content ---


class TestNonClaudeDefaults:
    """Gemini and Codex never detect Claude-specific plan or accept-edits prompts."""

    def test_has_plan_prompt_with_content(self, non_claude_provider: LLMProvider) -> None:
        content = "Do you want me to proceed with this plan?\n> 1. Yes\n  2. No"
        assert non_claude_provider.has_plan_prompt(content) is False

    def test_has_accept_edits_prompt_with_content(self, non_claude_provider: LLMProvider) -> None:
        content = ">> accept edits on (shift+tab to cycle)"
        assert non_claude_provider.has_accept_edits_prompt(content) is False


# --- Universal approval_response contract ---


class TestApprovalResponseUniversal:
    """Gemini and Codex use y/n defaults; Claude overrides with Enter/Esc."""

    def test_non_claude_approve(self, non_claude_provider: LLMProvider) -> None:
        assert non_claude_provider.approval_response(approve=True) == "y\r"

    def test_non_claude_reject(self, non_claude_provider: LLMProvider) -> None:
        assert non_claude_provider.approval_response(approve=False) == "n\r"


# --- Universal session_dir contract ---


class TestSessionDirUniversal:
    """Gemini and Codex return None; Claude returns a Path."""

    def test_non_claude_returns_none(self, non_claude_provider: LLMProvider) -> None:
        assert non_claude_provider.session_dir("/some/path") is None


# --- display_name ---


class TestDisplayName:
    """All providers have a non-empty display_name; specific values for each."""

    def test_display_name_non_empty(self, provider: LLMProvider) -> None:
        assert isinstance(provider.display_name, str)
        assert len(provider.display_name) > 0

    def test_claude_display_name(self) -> None:
        assert get_provider("claude").display_name == "Claude Code"

    def test_gemini_display_name(self) -> None:
        assert get_provider("gemini").display_name == "Gemini CLI"

    def test_codex_display_name(self) -> None:
        assert get_provider("codex").display_name == "Codex"


# --- Feature flags ---


class TestFeatureFlags:
    """Claude supports max_turns and json_output; others don't."""

    def test_claude_supports_max_turns(self) -> None:
        assert get_provider("claude").supports_max_turns is True

    def test_claude_supports_json_output(self) -> None:
        assert get_provider("claude").supports_json_output is True

    def test_non_claude_no_max_turns(self, non_claude_provider: LLMProvider) -> None:
        assert non_claude_provider.supports_max_turns is False

    def test_non_claude_no_json_output(self, non_claude_provider: LLMProvider) -> None:
        assert non_claude_provider.supports_json_output is False


# --- parse_usage ---


class TestParseUsage:
    """Claude extracts TokenUsage; non-Claude returns None."""

    def test_claude_parse_usage(self) -> None:
        provider = get_provider("claude")
        result = {
            "usage": {
                "input_tokens": 100,
                "output_tokens": 50,
                "cache_read_input_tokens": 200,
                "cache_creation_input_tokens": 300,
            },
            "total_cost_usd": 0.05,
        }
        usage = provider.parse_usage(result)
        assert isinstance(usage, TokenUsage)
        assert usage.input_tokens == 100
        assert usage.output_tokens == 50
        assert usage.cache_read_tokens == 200
        assert usage.cache_creation_tokens == 300
        assert usage.cost_usd == pytest.approx(0.05)

    def test_claude_parse_usage_missing_fields(self) -> None:
        provider = get_provider("claude")
        result = {"usage": {"input_tokens": 42}}
        usage = provider.parse_usage(result)
        assert isinstance(usage, TokenUsage)
        assert usage.input_tokens == 42
        assert usage.output_tokens == 0

    def test_claude_parse_usage_bad_usage_type(self) -> None:
        provider = get_provider("claude")
        result = {"usage": "not a dict"}
        assert provider.parse_usage(result) is None

    def test_non_claude_parse_usage_returns_none(self, non_claude_provider: LLMProvider) -> None:
        assert non_claude_provider.parse_usage({"usage": {"input_tokens": 1}}) is None
