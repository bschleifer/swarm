"""Tests for the Ask Queen live activity ticker (extract_queen_activity_line).

The ticker shows what the interactive Queen is *doing* while the operator
waits on her reply, so the panel is never a dead spinner. The extractor
pulls the last meaningful line from her PTY tail, dropping ANSI, blank
lines, and pure spinner / box-drawing chrome.
"""

from __future__ import annotations

from swarm.server.routes.queen import extract_queen_activity_line


class TestExtractQueenActivityLine:
    def test_none_for_empty(self):
        assert extract_queen_activity_line("") is None
        assert extract_queen_activity_line("   \n\n  \n") is None

    def test_returns_last_meaningful_line(self):
        content = "thinking...\ncalling queen_view_task_board\nreading buzz log"
        assert extract_queen_activity_line(content) == "reading buzz log"

    def test_strips_ansi(self):
        # Colour codes around the real text must not leak into the ticker.
        content = "\x1b[2m dim noise \x1b[0m\n\x1b[32mqueen_view_task_board()\x1b[0m"
        assert extract_queen_activity_line(content) == "queen_view_task_board()"

    def test_skips_spinner_and_box_chrome(self):
        # Spinner + box-drawing decoration lines are not real activity —
        # the extractor must walk past them to the last prose line.
        content = (
            "running queen_view_buzz_log\n"
            "╭──────────────────────────────╮\n"
            "⠹ ⠹ ⠹\n"
            "│                              │\n"
            "╰──────────────────────────────╯"
        )
        assert extract_queen_activity_line(content) == "running queen_view_buzz_log"

    def test_skips_terminal_chrome_lines(self):
        content = "inspecting the task board\n? for shortcuts\nesc to interrupt"
        assert extract_queen_activity_line(content) == "inspecting the task board"

    def test_caps_length(self):
        line = "x" * 500
        out = extract_queen_activity_line(line)
        assert out is not None
        assert len(out) <= 140

    def test_none_when_only_chrome(self):
        content = "⠋ ⠙ ⠹\n╭────╮\n╰────╯\n   \n"
        assert extract_queen_activity_line(content) is None
