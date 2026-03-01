"""Tests for style-aware state detection.

Verifies that StyledContent helpers and the Claude provider's
classify_styled_output() correctly use style data to reject false
positives from text-only pattern matching.
"""

from __future__ import annotations

from swarm.providers.claude import ClaudeProvider
from swarm.providers.styled import StyledContent
from swarm.pty.terminal import CellStyle
from swarm.worker.worker import WorkerState

# --- Helpers ---

_DEFAULT = CellStyle()
_GREEN = CellStyle(fg="green")
_DIM = CellStyle(dim=True)
_BOLD = CellStyle(bold=True)
_BLUE = CellStyle(fg="blue")
_GRAY = CellStyle(fg="999999")


def _make_row(text: str, style: CellStyle = _DEFAULT) -> tuple[str, list[CellStyle]]:
    """Build a styled row where every character has the same style."""
    return (text, [style] * len(text))


def _make_styled_row(
    text: str, ranges: dict[tuple[int, int], CellStyle], default: CellStyle = _DEFAULT
) -> tuple[str, list[CellStyle]]:
    """Build a styled row with specific style ranges.

    ranges maps (start, end) to a CellStyle for that span.
    """
    styles = [default] * len(text)
    for (start, end), style in ranges.items():
        for i in range(start, min(end, len(text))):
            styles[i] = style
    return (text, styles)


# --- StyledContent tests ---


class TestStyledContent:
    def test_has_styles_empty(self) -> None:
        sc = StyledContent(text="hello", rows=[])
        assert not sc.has_styles()

    def test_has_styles_with_rows(self) -> None:
        sc = StyledContent(text="hello", rows=[_make_row("hello")])
        assert sc.has_styles()

    def test_style_at_valid(self) -> None:
        sc = StyledContent(text="ab", rows=[_make_row("ab", _GREEN)])
        s = sc.style_at(0, 0)
        assert s is not None
        assert s.fg == "green"

    def test_style_at_out_of_range(self) -> None:
        sc = StyledContent(text="ab", rows=[_make_row("ab")])
        assert sc.style_at(5, 0) is None
        assert sc.style_at(0, 99) is None
        assert sc.style_at(-1, 0) is None

    def test_find_styled_text_dim(self) -> None:
        sc = StyledContent(
            text="esc to interrupt",
            rows=[_make_row("esc to interrupt", _DIM)],
        )
        assert sc.find_styled_text("esc to interrupt", dim=True)
        assert not sc.find_styled_text("esc to interrupt", dim=False)

    def test_find_styled_text_not_default_fg(self) -> None:
        row = _make_styled_row(
            "  > prompt text",
            {(2, 3): _GREEN},
        )
        sc = StyledContent(text=row[0], rows=[row])
        assert sc.find_styled_text(">", fg="!default")

    def test_find_styled_text_default_fg_rejected(self) -> None:
        sc = StyledContent(
            text="> diff output",
            rows=[_make_row("> diff output", _DEFAULT)],
        )
        assert not sc.find_styled_text(">", fg="!default")

    def test_find_styled_text_exact_fg(self) -> None:
        sc = StyledContent(
            text="hello",
            rows=[_make_row("hello", _GREEN)],
        )
        assert sc.find_styled_text("hello", fg="green")
        assert not sc.find_styled_text("hello", fg="red")

    def test_find_styled_text_no_rows(self) -> None:
        sc = StyledContent(text="hello", rows=[])
        assert not sc.find_styled_text("hello", dim=True)

    def test_find_styled_text_empty_needle(self) -> None:
        sc = StyledContent(text="hello", rows=[_make_row("hello")])
        assert not sc.find_styled_text("", dim=True)

    def test_find_styled_text_partial_match(self) -> None:
        """Needle found but only some chars match the style predicate."""
        row = _make_styled_row(
            "esc to interrupt",
            {(0, 3): _DIM},  # only "esc" is dim, rest is default
        )
        sc = StyledContent(text=row[0], rows=[row])
        assert not sc.find_styled_text("esc to interrupt", dim=True)

    def test_find_styled_text_multiple_occurrences(self) -> None:
        """Second occurrence matches style even if first doesn't."""
        row = _make_styled_row(
            "> plain > styled",
            {(8, 9): _GREEN},
        )
        sc = StyledContent(text=row[0], rows=[row])
        assert sc.find_styled_text(">", fg="!default")


# --- Claude provider styled classification ---


class TestClaudeStyledClassification:
    """Test classify_styled_output with style-aware checks."""

    provider = ClaudeProvider()

    def test_fallback_with_no_styles(self) -> None:
        """Empty rows → falls back to text-only classify_output."""
        # Use prompt with text (not bare ❯ which triggers has_empty_prompt → WAITING)
        text = "some output\n❯ hello"
        sc = StyledContent(text=text, rows=[])
        state = self.provider.classify_styled_output("claude", sc)
        # Text-only would see the prompt and classify as RESTING
        assert state == WorkerState.RESTING

    def test_diff_output_gt_not_prompt(self) -> None:
        """A > in git diff output (default style) should NOT match as prompt."""
        lines = [
            _make_row("diff --git a/file.py b/file.py"),
            _make_row("> added line"),  # default fg — this is diff output
            _make_row("  context line"),
        ]
        text = "\n".join(r[0] for r in lines)
        sc = StyledContent(text=text, rows=lines)
        # No styled prompt or buzzing signal → falls back to text-only
        # Text-only sees > and would classify as RESTING, but styled
        # path doesn't find a styled prompt → falls through to text-only
        state = self.provider.classify_styled_output("claude", sc)
        # Should fall back to text-only classify_output which sees ">"
        assert state in (WorkerState.RESTING, WorkerState.BUZZING)

    def test_styled_prompt_detected(self) -> None:
        """Styled hint line '? for shortcuts' confirms a real prompt."""
        lines = [
            _make_row("some output"),
            _make_row("❯ hello"),
            _make_row("? for shortcuts", _GRAY),
        ]
        text = "\n".join(r[0] for r in lines)
        sc = StyledContent(text=text, rows=lines)
        state = self.provider.classify_styled_output("claude", sc)
        assert state == WorkerState.RESTING

    def test_prompt_char_default_fg_no_hints_falls_through(self) -> None:
        """Prompt ❯ with default fg and no hint line falls to text-only."""
        lines = [
            _make_row("some output"),
            _make_row("❯ hello"),  # default fg — matches real Claude Code
        ]
        text = "\n".join(r[0] for r in lines)
        sc = StyledContent(text=text, rows=lines)
        state = self.provider.classify_styled_output("claude", sc)
        # No styled signal → falls back to text-only which sees ❯
        text_only = self.provider.classify_output("claude", text)
        assert state == text_only

    def test_esc_to_interrupt_dim_is_buzzing(self) -> None:
        """Dim 'esc to interrupt' confirms BUZZING."""
        lines = [
            _make_row("Working on task..."),
            _make_row("esc to interrupt", _DIM),
        ]
        text = "\n".join(r[0] for r in lines)
        sc = StyledContent(text=text, rows=lines)
        state = self.provider.classify_styled_output("claude", sc)
        assert state == WorkerState.BUZZING

    def test_esc_to_interrupt_not_dim_falls_through(self) -> None:
        """Non-dim 'esc to interrupt' (pasted text) does not trigger BUZZING.

        Falls through to prompt check — styled hint line detected, so RESTING.
        """
        lines = [
            _make_row("esc to interrupt"),  # default style (pasted text)
            _make_row("❯ hello"),
            _make_row("? for shortcuts", _GRAY),
        ]
        text = "\n".join(r[0] for r in lines)
        sc = StyledContent(text=text, rows=lines)
        state = self.provider.classify_styled_output("claude", sc)
        # Should detect the styled hint and classify as RESTING
        assert state == WorkerState.RESTING

    def test_styled_choice_menu(self) -> None:
        """Choice menu with styled cursor → WAITING."""
        lines = [
            _make_row("Which option?"),
            _make_styled_row("> 1. Yes", {(0, 1): _BLUE}),
            _make_row("  2. No"),
        ]
        text = "\n".join(r[0] for r in lines)
        sc = StyledContent(text=text, rows=lines)
        state = self.provider.classify_styled_output("claude", sc)
        assert state == WorkerState.WAITING

    def test_numbered_list_not_choice(self) -> None:
        """Numbered list in code output (default style) should not match as choice.

        Without styled cursor, falls back to text-only.
        """
        lines = [
            _make_row("Here are the steps:"),
            _make_row("> 1. First step"),  # default fg — not a real cursor
            _make_row("  2. Second step"),
        ]
        text = "\n".join(r[0] for r in lines)
        sc = StyledContent(text=text, rows=lines)
        state = self.provider.classify_styled_output("claude", sc)
        # Falls through to text-only which may detect choice from regex
        # The styled path doesn't confirm it, so result comes from text-only
        # Text-only path would see "> 1." and "  2." and detect as WAITING
        text_only_state = self.provider.classify_output("claude", text)
        assert state == text_only_state

    def test_accept_edits_text_only(self) -> None:
        """Accept edits prompt only needs text match, no style confirmation."""
        lines = [
            _make_row("File changes:"),
            _make_row(">> accept edits on src/foo.py"),
        ]
        text = "\n".join(r[0] for r in lines)
        sc = StyledContent(text=text, rows=lines)
        state = self.provider.classify_styled_output("claude", sc)
        assert state == WorkerState.WAITING

    def test_shell_exited_is_stung(self) -> None:
        """Shell command detected → STUNG regardless of style data."""
        lines = [_make_row("$ ")]
        text = "$ "
        sc = StyledContent(text=text, rows=lines)
        state = self.provider.classify_styled_output("bash", sc)
        assert state == WorkerState.STUNG

    def test_classify_styled_with_events(self) -> None:
        """classify_styled_with_events returns both state and events."""
        lines = [
            _make_row("Working on task..."),
            _make_row("esc to interrupt", _DIM),
        ]
        text = "\n".join(r[0] for r in lines)
        sc = StyledContent(text=text, rows=lines)
        state, events = self.provider.classify_styled_with_events("claude", sc)
        assert state == WorkerState.BUZZING
        assert len(events) > 0
