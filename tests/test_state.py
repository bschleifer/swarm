"""Tests for worker/state.py — state detection regex classification."""

from swarm.worker.state import (
    classify_pane_content,
    get_choice_summary,
    has_accept_edits_prompt,
    has_choice_prompt,
    has_empty_prompt,
    has_idle_prompt,
    has_plan_prompt,
    is_user_question,
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
            "esc to interrupt\n"  # old, from previous work — 32+ lines back
            + "more output\n"
            * 32
            + "done with task\n"
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

    def test_esc_to_interrupt_beyond_5_lines_still_buzzing(self):
        """'esc to interrupt' up to 20 lines from bottom should still be BUZZING.

        Regression: Claude Code produces long tool output (file reads, diffs)
        that can push 'esc to interrupt' well beyond the last 5 lines while
        the worker is still actively processing.
        """
        # "esc to interrupt" at line 12 from bottom — outside old 5-line window
        content = (
            "Working on task...\n"
            "⏳ Reading file src/swarm/server/daemon.py\n"
            "esc to interrupt\n" + "  line of file content\n" * 12 + "  more content"
        )
        assert classify_pane_content("claude", content) == WorkerState.BUZZING

    def test_esc_to_interrupt_in_scrollback_beyond_30_lines_is_not_buzzing(self):
        """Stale 'esc to interrupt' more than 30 lines back should NOT be BUZZING."""
        content = (
            "some output\n"
            "esc to interrupt\n"  # stale — 32+ lines from bottom
            + "more output\n" * 32
            + "> "  # current: idle prompt
        )
        assert classify_pane_content("claude", content) == WorkerState.WAITING

    def test_long_tool_output_with_gt_not_false_resting(self):
        """Code output containing '>' (diffs, markdown) should not false-positive as RESTING.

        Regression: git diff output with '>' in the last 5 lines was falsely
        matching the prompt regex, causing BUZZING workers to show as RESTING.
        """
        content = (
            "⏳ Running command\n"
            "esc to interrupt\n"
            "diff --git a/file.py b/file.py\n"
            "--- a/file.py\n"
            "+++ b/file.py\n"
            "@@ -10,3 +10,3 @@\n"
            "-old line\n"
            "+new line\n"
            "> some context from diff\n"
            "  more diff output\n"
        )
        assert classify_pane_content("claude", content) == WorkerState.BUZZING

    def test_very_long_tool_output_pushes_esc_beyond_20_lines(self):
        """Active worker with 25+ lines of tool output after 'esc to interrupt'.

        Regression: when a Read or Grep tool produces very long output,
        'esc to interrupt' is pushed beyond the 20-line tail window.  If
        the tool output contains '>' (diffs, markdown blockquotes), the
        prompt regex false-matches and the worker is marked RESTING.
        """
        content = (
            "⏳ Reading file src/swarm/server/daemon.py\n"
            "esc to interrupt\n"
            + "  line of file content\n" * 22  # push indicator beyond old 20-line window
            + "> some diff context\n"
            + "  more output\n"
        )
        assert classify_pane_content("claude", content) == WorkerState.BUZZING

    def test_choice_prompt_with_long_diff_is_waiting(self):
        """Permission prompt with a long file diff should be WAITING.

        Regression: long diffs in Edit permission prompts can push the numbered
        options above the 15-line window, causing WAITING to be missed.
        """
        diff_lines = "  | line of diff content\n" * 18
        content = (
            "Edit file src/swarm/config.py\n"
            + diff_lines
            + "Allow Edit for src/swarm/config.py?\n"
            + "> 1. Always allow\n"
            + "  2. Allow once\n"
            + "  3. Don't allow\n"
            + "Enter to select"
        )
        assert classify_pane_content("claude", content) == WorkerState.WAITING

    def test_long_choice_menu_cursor_above_tail(self):
        """Choice menu where cursor (❯/>) on option 1 is above the last 5 lines.

        Regression test: the prompt gate only checks the last 5 lines for ❯/>,
        but in long menus the cursor is much higher. The fallback must still
        detect the menu via has_choice_prompt (last 15 lines).
        """
        content = """\
staging-merge.service.spec.ts  | 144 +++++++++++++++++++
staging-merge.service.ts       |  12 +-
  2 files changed, 153 insertions(+), 3 deletions(-)

Staging verified. Swap to production?
❯ 1. Yes — swap to production
     Swap the staging slot to production.
  2. No — keep on staging only
     Stop here. The fix is deployed to staging only.
  3. Rollback staging
     Revert the staging deployment.
  4. Type something.

  5. Chat about this
Enter to select · ↑/↓ to navigate · Esc to cancel"""
        assert classify_pane_content("claude", content) == WorkerState.WAITING

    def test_accept_edits_prompt_is_waiting(self):
        """Accept-edits prompt from /check or /commit skills should be WAITING."""
        content = (
            "some output\n  src/swarm/worker/state.py\n>> accept edits on (shift+tab to cycle)\n"
        )
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


# --- is_user_question ---


class TestIsUserQuestion:
    def test_ask_user_question_with_chat_about_this(self):
        content = """\
How would you like to proceed?
> 1. Fix both issues
  2. File issues for later
  3. Done for now
  4. Type something.

  5. Chat about this
Enter to select · ↑/↓ to navigate · Esc to cancel"""
        assert is_user_question(content) is True

    def test_ask_user_question_type_something_only(self):
        content = """\
Which approach should we use?
> 1. Option A
  2. Option B
  3. Type something.
Enter to select"""
        assert is_user_question(content) is True

    def test_permission_prompt_not_user_question(self):
        content = """\
> 1. Always allow
  2. Yes
  3. No
Enter to select · ↑/↓ to navigate · Esc to cancel"""
        assert is_user_question(content) is False

    def test_yes_no_confirmation_not_user_question(self):
        content = """\
Do you want to proceed?
> 1. Yes
  2. No
Esc to cancel"""
        assert is_user_question(content) is False

    def test_empty(self):
        assert is_user_question("") is False


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


# --- has_plan_prompt ---


class TestHasPlanPrompt:
    def test_actual_plan_approval_prompt(self):
        """Genuine plan approval with 'proceed with this plan' detected."""
        content = """Here is my plan for implementing the feature:

## Plan
1. Create the new module
2. Add tests
3. Update docs

Do you want me to proceed with this plan?
> 1. Yes, proceed
  2. No, revise
  3. Cancel
Enter to select"""
        assert has_plan_prompt(content) is True

    def test_plan_file_reference(self):
        """Plan prompt mentioning a plan file is detected."""
        content = """A plan file exists from plan mode at: /home/user/.claude/plans/plan.md

Plan contents: ...

> 1. Yes, proceed
  2. No, edit
Enter to select"""
        assert has_plan_prompt(content) is True

    def test_false_positive_plan_in_conversation(self):
        """Regression: 'plan' in worker conversation should NOT trigger plan detection.

        When a worker is implementing a multi-phase plan, the word 'plan' appears
        naturally in its output. A subsequent permission prompt (e.g., grep) should
        NOT be classified as a plan approval prompt.
        """
        content = """Phase 1 of the plan is complete. Moving to Phase 2.
The plan was already approved by the operator.
Now executing the approved plan for type safety fixes.

Grep command
  grep -r "list\\[dict\\]" src/
> 1. Allow
  2. Allow always
  3. Deny
Enter to select"""
        assert has_plan_prompt(content) is False

    def test_false_positive_plan_mode_discussion(self):
        """Worker discussing plan mode features should not trigger plan detection."""
        content = """I'll implement the plan mode feature for the CLI.
Let me read the existing code first.

Read file
  Read(src/swarm/cli.py)
> 1. Allow
  2. Always allow
  3. Deny
Enter to select"""
        assert has_plan_prompt(content) is False

    def test_no_choice_menu_not_plan(self):
        """Content with plan text but no choice menu is not a plan prompt."""
        content = """Here is my plan:
1. Fix the bug
2. Add tests
> """
        assert has_plan_prompt(content) is False

    def test_empty(self):
        assert has_plan_prompt("") is False


# --- has_accept_edits_prompt ---


class TestHasAcceptEditsPrompt:
    def test_standard_accept_edits(self):
        content = ">> accept edits on (shift+tab to cycle)"
        assert has_accept_edits_prompt(content) is True

    def test_accept_edits_with_surrounding_content(self):
        content = (
            "Running /check...\n"
            "  src/swarm/worker/state.py\n"
            ">> accept edits on (shift+tab to cycle)\n"
        )
        assert has_accept_edits_prompt(content) is True

    def test_single_gt_not_accept_edits(self):
        """A normal '> accept' prompt should not match (needs '>>')."""
        content = "> accept edits on (shift+tab to cycle)"
        assert has_accept_edits_prompt(content) is False

    def test_empty(self):
        assert has_accept_edits_prompt("") is False

    def test_accept_edits_above_5_lines(self):
        """Accept-edits prompt more than 5 lines from bottom should not match."""
        content = ">> accept edits on (shift+tab to cycle)\n" + "other output\n" * 10
        assert has_accept_edits_prompt(content) is False
