"""Universal contract tests for all LLM providers.

Parametrized over claude/gemini/codex — each test runs once per provider.
Covers shared base-class behavior and universal classify_output contracts.
"""

import pytest

from swarm.providers import get_provider
from swarm.providers.base import LLMProvider
from swarm.worker.worker import WorkerState


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
