"""Tests for worker/state.py — state detection regex classification."""

from swarm.worker.state import (
    classify_pane_content,
    has_choice_prompt,
    has_empty_prompt,
    has_idle_prompt,
    has_yn_prompt,
)
from swarm.worker.worker import WorkerState


# --- classify_pane_content ---


class TestClassifyPaneContent:
    def test_shell_foreground_is_stung(self):
        for shell in ("bash", "zsh", "sh", "fish"):
            assert classify_pane_content(shell, "$ ") == WorkerState.STUNG

    def test_esc_to_interrupt_is_buzzing(self):
        content = "Working on task...\n  esc to interrupt\nProcessing files..."
        assert classify_pane_content("claude", content) == WorkerState.BUZZING

    def test_prompt_arrow_is_resting(self):
        content = "Done.\n\n> "
        assert classify_pane_content("claude", content) == WorkerState.RESTING

    def test_prompt_chevron_is_resting(self):
        content = "Done.\n\n❯ "
        assert classify_pane_content("claude", content) == WorkerState.RESTING

    def test_shortcuts_hint_is_resting(self):
        content = "Some output\n? for shortcuts"
        assert classify_pane_content("claude", content) == WorkerState.RESTING

    def test_unknown_content_defaults_to_buzzing(self):
        content = "random stuff happening"
        assert classify_pane_content("claude", content) == WorkerState.BUZZING

    def test_empty_content_defaults_to_buzzing(self):
        assert classify_pane_content("claude", "") == WorkerState.BUZZING

    def test_node_command_not_stung(self):
        content = "> some prompt"
        assert classify_pane_content("node", content) == WorkerState.RESTING

    def test_prompt_with_suggestion_text(self):
        content = '> Try "how does the auth module work"'
        assert classify_pane_content("claude", content) == WorkerState.RESTING


# --- has_yn_prompt ---


class TestHasYnPrompt:
    def test_allow_yn(self):
        content = "Allow this operation? (y/n)"
        assert has_yn_prompt(content) is True

    def test_approve_prompt(self):
        content = "Some context\nDo you approve this?"
        assert has_yn_prompt(content) is True

    def test_no_prompt(self):
        content = "Just some normal output"
        assert has_yn_prompt(content) is False

    def test_empty(self):
        assert has_yn_prompt("") is False
        assert has_yn_prompt("   ") is False


# --- has_choice_prompt ---


class TestHasChoicePrompt:
    def test_standard_choice_menu(self):
        content = """> 1. Always allow
  2. Yes
  3. No
Enter to select · ↑/↓ to navigate"""
        assert has_choice_prompt(content) is True

    def test_no_menu_footer(self):
        content = """> 1. Option A
  2. Option B"""
        assert has_choice_prompt(content) is False

    def test_no_numbered_options(self):
        content = "Enter to select · ↑/↓ to navigate"
        assert has_choice_prompt(content) is False

    def test_empty(self):
        assert has_choice_prompt("") is False


# --- has_idle_prompt ---


class TestHasIdlePrompt:
    def test_bare_arrow_prompt(self):
        assert has_idle_prompt("> ") is True

    def test_bare_chevron_prompt(self):
        assert has_idle_prompt("❯ ") is True

    def test_prompt_with_suggestion(self):
        assert has_idle_prompt('> Try "how does foo work"') is True

    def test_shortcuts_hint(self):
        assert has_idle_prompt("? for shortcuts") is True

    def test_task_hint(self):
        assert has_idle_prompt("ctrl+t to hide tasks") is True

    def test_no_prompt(self):
        assert has_idle_prompt("processing...") is False

    def test_empty(self):
        assert has_idle_prompt("") is False


# --- has_empty_prompt ---


class TestHasEmptyPrompt:
    def test_bare_arrow(self):
        assert has_empty_prompt("> ") is True

    def test_bare_chevron(self):
        assert has_empty_prompt("❯") is True

    def test_prompt_with_text(self):
        assert has_empty_prompt("> some text") is False

    def test_empty(self):
        assert has_empty_prompt("") is False

    def test_multiline_last_line_empty_prompt(self):
        content = "previous output\n> "
        assert has_empty_prompt(content) is True
