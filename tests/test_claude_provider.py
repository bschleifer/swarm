"""Tests for ClaudeProvider.classify_output — state detection from PTY content.

Covers RESTING, WAITING, BUZZING, and STUNG states, including prompt patterns,
choice menus, plan prompts, accept-edits, cursor options, and edge cases.
"""

from __future__ import annotations

import pytest

from swarm.providers.claude import ClaudeProvider
from swarm.worker.worker import WorkerState

_provider = ClaudeProvider()


# ---------------------------------------------------------------------------
# RESTING — idle prompt visible, no actionable choice/plan/empty prompt
# ---------------------------------------------------------------------------


class TestClassifyOutputResting:
    """RESTING: prompt detected in tail but no actionable prompt type."""

    def test_bare_arrow_prompt_with_suggestion_text(self):
        """'> Try ...' is a suggestion, not an empty prompt — should be RESTING."""
        content = 'Task complete.\n> Try "how does auth work"'
        assert _provider.classify_output("claude", content) == WorkerState.RESTING

    def test_chevron_prompt_with_suggestion_text(self):
        """Same as above but with the heavy chevron character."""
        content = 'All done.\n❯ Try "explain the state machine"'
        assert _provider.classify_output("claude", content) == WorkerState.RESTING

    def test_shortcuts_hint_alone_is_resting(self):
        """'? for shortcuts' without any choice/plan markers is RESTING."""
        content = "Finished refactoring.\n? for shortcuts"
        assert _provider.classify_output("claude", content) == WorkerState.RESTING

    def test_prompt_after_multiline_output_is_resting(self):
        """A prompt at the end of substantial output is RESTING."""
        lines = ["line of output"] * 20
        content = "\n".join(lines) + '\n> Try "what does config.py do"'
        assert _provider.classify_output("claude", content) == WorkerState.RESTING

    def test_indented_prompt_is_resting(self):
        """Prompts may have leading whitespace — _RE_PROMPT uses MULTILINE."""
        content = "Output done.\n  > some suggestion"
        assert _provider.classify_output("claude", content) == WorkerState.RESTING

    def test_ctrl_t_hint_with_prompt_is_resting(self):
        """ctrl+t hint alongside a prompt but no choice menu is RESTING."""
        content = "> some prompt text\nctrl+t to hide"
        # The '>' is in narrow tail, triggers prompt path.  No choice/plan/empty.
        assert _provider.classify_output("claude", content) == WorkerState.RESTING


# ---------------------------------------------------------------------------
# WAITING — actionable prompt requiring drone/user interaction
# ---------------------------------------------------------------------------


class TestClassifyOutputWaiting:
    """WAITING: choice menu, plan approval, empty prompt, or accept-edits."""

    def test_simple_yes_no_choice(self):
        """Standard two-option choice menu."""
        content = "Do you want to proceed?\n> 1. Yes\n  2. No\nEsc to cancel"
        assert _provider.classify_output("claude", content) == WorkerState.WAITING

    def test_three_option_permission_menu(self):
        """Claude permission menu with Always/Yes/No."""
        content = (
            "Allow Bash for this command?\n"
            "> 1. Always allow\n"
            "  2. Yes\n"
            "  3. No\n"
            "Enter to select · ↑/↓ to navigate"
        )
        assert _provider.classify_output("claude", content) == WorkerState.WAITING

    def test_empty_arrow_prompt_is_waiting(self):
        """A bare '> ' at the end means Claude finished and awaits input."""
        content = "Done implementing the feature.\n\n> "
        assert _provider.classify_output("claude", content) == WorkerState.WAITING

    def test_empty_chevron_prompt_is_waiting(self):
        """Same as above with heavy right-pointing angle bracket."""
        content = "All tests pass.\n\n❯ "
        assert _provider.classify_output("claude", content) == WorkerState.WAITING

    def test_empty_prompt_no_trailing_space(self):
        """Bare '>' without trailing space is still an empty prompt."""
        content = "Finished.\n>"
        assert _provider.classify_output("claude", content) == WorkerState.WAITING

    def test_accept_edits_prompt(self):
        """'>> accept edits on ...' is a WAITING state."""
        content = (
            "Running /check...\n  src/swarm/config.py\n>> accept edits on (shift+tab to cycle)\n"
        )
        assert _provider.classify_output("claude", content) == WorkerState.WAITING

    def test_plan_approval_with_proceed(self):
        """Plan approval prompt with 'proceed with this plan' is WAITING."""
        content = (
            "Here is my plan:\n"
            "1. Refactor module\n"
            "2. Add tests\n"
            "\n"
            "Do you want me to proceed with this plan?\n"
            "> 1. Yes, proceed\n"
            "  2. No, revise\n"
            "Enter to select"
        )
        assert _provider.classify_output("claude", content) == WorkerState.WAITING

    def test_plan_saved_prompt(self):
        """Plan prompt with 'plan saved' marker is WAITING."""
        content = (
            "Plan saved to /home/user/.claude/plans/fix.md\n"
            "Shall I execute it?\n"
            "> 1. Yes\n"
            "  2. No\n"
            "Enter to select"
        )
        assert _provider.classify_output("claude", content) == WorkerState.WAITING

    def test_approve_the_plan_prompt(self):
        """Plan prompt with 'approve the plan' marker is WAITING."""
        content = "Would you like to approve the plan?\n> 1. Approve\n  2. Reject\nEnter to select"
        assert _provider.classify_output("claude", content) == WorkerState.WAITING

    def test_choice_menu_without_prompt_in_narrow_tail(self):
        """Choice menu cursor (❯) outside narrow 5-line tail still detected via fallback."""
        content = (
            "Some context line\n"
            "❯ 1. Option A — long description here\n"
            "     Subtitle for option A\n"
            "  2. Option B — another description\n"
            "     Subtitle for option B\n"
            "  3. Option C\n"
            "  4. Type something.\n"
            "\n"
            "  5. Chat about this\n"
            "Enter to select · ↑/↓ to navigate · Esc to cancel"
        )
        assert _provider.classify_output("claude", content) == WorkerState.WAITING

    def test_choice_after_long_diff_output(self):
        """Permission prompt following a large diff — cursor may be far from tail."""
        diff = "  | diff line\n" * 20
        content = (
            "Edit file src/swarm/server/api.py\n"
            + diff
            + "Allow Edit for src/swarm/server/api.py?\n"
            + "> 1. Always allow\n"
            + "  2. Allow once\n"
            + "  3. Don't allow\n"
            + "Enter to select"
        )
        assert _provider.classify_output("claude", content) == WorkerState.WAITING


# ---------------------------------------------------------------------------
# BUZZING — worker actively processing
# ---------------------------------------------------------------------------


class TestClassifyOutputBuzzing:
    """BUZZING: 'esc to interrupt' in tail, or no recognizable prompt."""

    def test_esc_to_interrupt_in_recent_output(self):
        """Standard active processing indicator."""
        content = "Working...\nesc to interrupt\nsome tool output"
        assert _provider.classify_output("claude", content) == WorkerState.BUZZING

    def test_esc_to_interrupt_at_line_15(self):
        """'esc to interrupt' 15 lines from bottom is within the 30-line window."""
        content = "Reading file...\nesc to interrupt\n" + "  file content line\n" * 15
        assert _provider.classify_output("claude", content) == WorkerState.BUZZING

    def test_esc_to_interrupt_at_line_29(self):
        """'esc to interrupt' at line 29 from bottom — still inside the 30-line tail."""
        content = "Processing...\nesc to interrupt\n" + "  output\n" * 28
        assert _provider.classify_output("claude", content) == WorkerState.BUZZING

    def test_esc_to_interrupt_beyond_30_lines_not_buzzing(self):
        """Stale 'esc to interrupt' beyond the 30-line window should NOT be BUZZING."""
        content = "Old processing...\nesc to interrupt\n" + "  output\n" * 35 + "> "
        # The '> ' at the end is an empty prompt — WAITING, not BUZZING
        assert _provider.classify_output("claude", content) == WorkerState.WAITING

    def test_unknown_content_defaults_to_buzzing(self):
        """Content with no recognizable pattern defaults to BUZZING."""
        content = "Compiling assets... please wait"
        assert _provider.classify_output("claude", content) == WorkerState.BUZZING

    def test_empty_content_is_buzzing(self):
        """Completely empty output defaults to BUZZING (worker just started)."""
        assert _provider.classify_output("claude", "") == WorkerState.BUZZING

    def test_whitespace_only_content_is_buzzing(self):
        """Content with only whitespace/newlines is not a prompt."""
        assert _provider.classify_output("claude", "   \n\n  \n") == WorkerState.BUZZING

    def test_esc_to_interrupt_takes_priority_over_prompt(self):
        """If both 'esc to interrupt' and a prompt marker exist in tail, BUZZING wins.

        'esc to interrupt' is checked first — it means work is still happening
        even if some prompt-like characters appear in tool output.
        """
        content = "Processing diff...\nesc to interrupt\n> line from diff context\n  more diff\n"
        assert _provider.classify_output("claude", content) == WorkerState.BUZZING

    def test_numbered_list_in_output_not_choice_menu(self):
        """Numbered list in markdown output should not false-positive as WAITING.

        The choice-menu detector requires both a cursor (❯/>) on a numbered
        option AND separate indented numbered options.  Plain markdown
        numbered lists lack the cursor prefix.
        """
        content = (
            "Here are the steps:\n1. First do this\n2. Then do that\n3. Finally, check results\n"
        )
        assert _provider.classify_output("claude", content) == WorkerState.BUZZING

    def test_gt_in_markdown_blockquote_not_resting(self):
        """'>' in a markdown blockquote should not trigger prompt detection.

        The prompt regex requires '^\\s*[>❯]' which a blockquote can match.
        But since 'esc to interrupt' appears in the 30-line tail, BUZZING
        takes priority.
        """
        content = (
            "Reading documentation...\n"
            "esc to interrupt\n"
            "> Note: this is a blockquote in the file\n"
            "> It continues on the next line\n"
        )
        assert _provider.classify_output("claude", content) == WorkerState.BUZZING


# ---------------------------------------------------------------------------
# STUNG — foreground process is a shell (Claude has exited)
# ---------------------------------------------------------------------------


class TestClassifyOutputStung:
    """STUNG: the foreground command is a shell — Claude has exited."""

    @pytest.mark.parametrize(
        "shell",
        ["bash", "zsh", "sh", "fish", "dash", "ksh", "csh", "tcsh"],
    )
    def test_shell_names_are_stung(self, shell: str):
        """All known shell names should produce STUNG regardless of content."""
        assert _provider.classify_output(shell, "> prompt") == WorkerState.STUNG

    def test_shell_full_path_is_stung(self):
        """Shells invoked via full path are also STUNG."""
        assert _provider.classify_output("/bin/bash", "$ ") == WorkerState.STUNG
        assert _provider.classify_output("/usr/bin/zsh", "% ") == WorkerState.STUNG

    def test_shell_stung_overrides_esc_to_interrupt(self):
        """STUNG check runs first — even 'esc to interrupt' in content is irrelevant."""
        content = "esc to interrupt\nstill processing..."
        assert _provider.classify_output("bash", content) == WorkerState.STUNG

    def test_non_shell_command_not_stung(self):
        """Commands like 'node', 'python', 'claude' are NOT shells."""
        assert _provider.classify_output("claude", "> ") != WorkerState.STUNG
        assert _provider.classify_output("node", "> ") != WorkerState.STUNG
        assert _provider.classify_output("python", "> ") != WorkerState.STUNG

    def test_shell_with_empty_content_is_stung(self):
        """Shell command with no output is still STUNG."""
        assert _provider.classify_output("bash", "") == WorkerState.STUNG


# ---------------------------------------------------------------------------
# Cursor option patterns (❯ prefix)
# ---------------------------------------------------------------------------


class TestClassifyOutputCursorOptions:
    """Cursor option patterns using heavy right-pointing angle bracket (❯)."""

    def test_heavy_chevron_cursor_on_option(self):
        """❯ on a numbered option with other options is a choice menu (WAITING)."""
        content = (
            "Some question?\n"
            "❯ 1. First option\n"
            "  2. Second option\n"
            "  3. Third option\n"
            "Enter to select"
        )
        assert _provider.classify_output("claude", content) == WorkerState.WAITING

    def test_heavy_chevron_cursor_not_on_numbered_option(self):
        """❯ without a numbered option after it is just a prompt marker, not a choice."""
        content = "❯ some command suggestion"
        assert _provider.classify_output("claude", content) == WorkerState.RESTING

    def test_gt_cursor_on_numbered_option(self):
        """'>' on a numbered option (standard prompt character) is a choice menu."""
        content = "> 1. Allow\n  2. Deny\nEnter to select"
        assert _provider.classify_output("claude", content) == WorkerState.WAITING


# ---------------------------------------------------------------------------
# Plan detection edge cases
# ---------------------------------------------------------------------------


class TestClassifyOutputPlanEdgeCases:
    """Plan-related output that should NOT trigger WAITING false positives."""

    def test_plan_word_in_conversation_with_permission_prompt(self):
        """'plan' in conversation text + unrelated permission prompt is NOT a plan prompt."""
        content = (
            "Phase 1 of the plan is complete.\n"
            "Executing the approved plan now.\n"
            "\n"
            "Bash command\n"
            "  npm run build\n"
            "> 1. Allow\n"
            "  2. Always allow\n"
            "  3. Deny\n"
            "Enter to select"
        )
        # has_choice_prompt is True, but has_plan_prompt is False because
        # 'plan' without the specific markers ('proceed with this plan', etc.)
        # does not trigger plan detection.
        assert _provider.classify_output("claude", content) == WorkerState.WAITING

    def test_plan_prefix_in_output_without_choice_menu(self):
        """Mentioning 'plan' without a choice menu should not be WAITING."""
        content = "Here is my plan:\n1. Fix the bug\n2. Add regression test\n"
        # No prompt at all — defaults to BUZZING
        assert _provider.classify_output("claude", content) == WorkerState.BUZZING


# ---------------------------------------------------------------------------
# Edge cases and mixed content
# ---------------------------------------------------------------------------


class TestClassifyOutputEdgeCases:
    """Edge cases: partial prompts, very long content, mixed signals."""

    def test_only_newlines(self):
        """Content that is only newlines — no meaningful content."""
        assert _provider.classify_output("claude", "\n\n\n\n") == WorkerState.BUZZING

    def test_prompt_buried_in_long_output(self):
        """A prompt character far from the tail (beyond 5 lines) without a choice
        menu should not trigger RESTING — defaults to BUZZING.
        """
        content = "> old prompt from earlier\n" + "processing line\n" * 10 + "still working on it"
        assert _provider.classify_output("claude", content) == WorkerState.BUZZING

    def test_mixed_esc_and_prompt_historical_esc(self):
        """Historical 'esc to interrupt' (beyond 30 lines) with current prompt."""
        content = "esc to interrupt\n" + "output line\n" * 35 + '\n> Try "explain the module"'
        assert _provider.classify_output("claude", content) == WorkerState.RESTING

    def test_accept_edits_without_prompt(self):
        """'>> accept edits on' triggers WAITING even without a '>' prompt in tail."""
        content = ">> accept edits on (shift+tab to cycle)"
        assert _provider.classify_output("claude", content) == WorkerState.WAITING

    def test_accept_edits_too_far_from_tail(self):
        """'>> accept edits' more than 5 lines from bottom should not be WAITING."""
        content = ">> accept edits on (shift+tab to cycle)\n" + "other output\n" * 10 + "done"
        # accept_edits_prompt is False (too far), no other prompt markers, defaults BUZZING
        assert _provider.classify_output("claude", content) == WorkerState.BUZZING

    def test_plan_file_marker_with_choice_menu(self):
        """'plan file' marker + choice menu = plan prompt WAITING."""
        content = (
            "A plan file exists at /home/user/.claude/plans/fix.md\n"
            "\n"
            "> 1. Execute plan\n"
            "  2. Edit plan\n"
            "  3. Cancel\n"
            "Enter to select"
        )
        assert _provider.classify_output("claude", content) == WorkerState.WAITING

    def test_very_long_content_only_tail_matters(self):
        """With 1000 lines, only the last 30 are checked for 'esc to interrupt'."""
        content = "line of output\n" * 1000 + '\n> Try "something"'
        assert _provider.classify_output("claude", content) == WorkerState.RESTING

    def test_command_name_claude_is_not_stung(self):
        """Sanity check: 'claude' is not a shell name."""
        content = "random output"
        result = _provider.classify_output("claude", content)
        assert result != WorkerState.STUNG

    def test_prompt_with_ansi_like_characters(self):
        """Content with special characters near a prompt should still detect the prompt."""
        content = "Done \x1b[32m✓\x1b[0m\n> "
        assert _provider.classify_output("claude", content) == WorkerState.WAITING

    def test_choice_menu_with_descriptions_on_separate_lines(self):
        """Claude menus sometimes have description lines under each option."""
        content = (
            "What should I do?\n"
            "❯ 1. Fix the bug\n"
            "     Patch the null pointer in auth.py\n"
            "  2. Skip for now\n"
            "     Move to the next task\n"
            "  3. Type something.\n"
            "\n"
            "  4. Chat about this\n"
            "Enter to select · ↑/↓ to navigate · Esc to cancel"
        )
        assert _provider.classify_output("claude", content) == WorkerState.WAITING
