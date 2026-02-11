"""Tests for worker/state.py — state detection regex classification."""

from swarm.worker.state import (
    classify_pane_content,
    get_choice_summary,
    has_choice_prompt,
    has_empty_prompt,
    has_idle_prompt,
)
from swarm.worker.worker import WorkerState


# --- classify_pane_content ---


class TestClassifyPaneContent:
    def test_shell_foreground_is_stung(self):
        for shell in ("bash", "zsh", "sh", "fish", "dash", "ksh", "csh", "tcsh"):
            assert classify_pane_content(shell, "$ ") == WorkerState.STUNG

    def test_shell_full_path_is_stung(self):
        assert classify_pane_content("/bin/bash", "$ ") == WorkerState.STUNG
        assert classify_pane_content("/usr/bin/zsh", "$ ") == WorkerState.STUNG

    def test_esc_to_interrupt_in_tail_is_buzzing(self):
        content = "Working on task...\nesc to interrupt\n"
        assert classify_pane_content("claude", content) == WorkerState.BUZZING

    def test_old_esc_to_interrupt_in_scrollback_doesnt_prevent_idle(self):
        """Historical 'esc to interrupt' in scrollback should not prevent idle detection."""
        content = (
            "some output\n"
            "esc to interrupt\n"  # old, from previous work
            "more output line 1\n"
            "more output line 2\n"
            "more output line 3\n"
            "more output line 4\n"
            "done with task\n"
            "\n"
            "> "  # current: empty prompt → WAITING
        )
        assert classify_pane_content("claude", content) == WorkerState.WAITING

    def test_empty_prompt_arrow_is_waiting(self):
        content = "Done.\n\n> "
        assert classify_pane_content("claude", content) == WorkerState.WAITING

    def test_empty_prompt_chevron_is_waiting(self):
        content = "Done.\n\n❯ "
        assert classify_pane_content("claude", content) == WorkerState.WAITING

    def test_shortcuts_hint_is_resting(self):
        content = "Some output\n? for shortcuts"
        assert classify_pane_content("claude", content) == WorkerState.RESTING

    def test_unknown_content_defaults_to_buzzing(self):
        content = "random stuff happening"
        assert classify_pane_content("claude", content) == WorkerState.BUZZING

    def test_empty_content_defaults_to_buzzing(self):
        assert classify_pane_content("claude", "") == WorkerState.BUZZING

    def test_node_command_not_stung(self):
        """Non-shell commands with a prompt are idle (suggestion text → RESTING)."""
        content = "> some prompt"
        assert classify_pane_content("node", content) == WorkerState.RESTING

    def test_prompt_with_suggestion_text_is_resting(self):
        """Prompt with suggestion text is RESTING (not actionable)."""
        content = '> Try "how does the auth module work"'
        assert classify_pane_content("claude", content) == WorkerState.RESTING

    def test_choice_prompt_is_waiting(self):
        """Choice menu prompts should be classified as WAITING."""
        content = """> 1. Always allow
  2. Yes
  3. No
Enter to select · ↑/↓ to navigate"""
        assert classify_pane_content("claude", content) == WorkerState.WAITING

    def test_plan_prompt_is_waiting(self):
        """Plan approval prompts should be classified as WAITING."""
        content = """Here is my plan:
Do you want me to proceed with this plan?
> 1. Yes, proceed
  2. No, revise
Enter to select"""
        assert classify_pane_content("claude", content) == WorkerState.WAITING


# --- has_choice_prompt ---


class TestHasChoicePrompt:
    def test_standard_choice_menu(self):
        content = """> 1. Always allow
  2. Yes
  3. No
Enter to select · ↑/↓ to navigate"""
        assert has_choice_prompt(content) is True

    def test_confirmation_prompt(self):
        """'Do you want to proceed?' uses a different footer than permission menus."""
        content = """\
Do you want to proceed?
> 1. Yes
   2. No

Esc to cancel · Tab to amend · ctrl+e to explain"""
        assert has_choice_prompt(content) is True

    def test_cursor_plus_options_without_footer(self):
        """Cursor on a numbered option + other options = menu, regardless of footer."""
        content = """> 1. Option A
  2. Option B"""
        assert has_choice_prompt(content) is True

    def test_no_numbered_options(self):
        content = "Enter to select · ↑/↓ to navigate"
        assert has_choice_prompt(content) is False

    def test_single_numbered_line_not_a_menu(self):
        """A lone numbered item without other options is not a menu."""
        content = "> 1. Only option"
        assert has_choice_prompt(content) is False

    def test_empty(self):
        assert has_choice_prompt("") is False


# --- get_choice_summary ---


class TestGetChoiceSummary:
    def test_permission_menu_no_question(self):
        content = """> 1. Always allow
  2. Yes
  3. No
Enter to select · ↑/↓ to navigate"""
        assert get_choice_summary(content) == "1. Always allow"

    def test_confirmation_prompt_with_question(self):
        content = """\
Do you want to proceed?
> 1. Yes
   2. No

Esc to cancel · Tab to amend · ctrl+e to explain"""
        assert get_choice_summary(content) == '"Do you want to proceed?" → 1. Yes'

    def test_tool_approval_with_context(self):
        content = """\
Bash command
  TOKEN=$(curl -s "https://example.com/api/token")
Do you want to proceed?
> 1. Yes
   2. No
Esc to cancel"""
        assert get_choice_summary(content) == '"Do you want to proceed?" → 1. Yes'

    def test_no_cursor(self):
        assert get_choice_summary("just some text") == ""

    def test_empty(self):
        assert get_choice_summary("") == ""


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
