"""Tests for swarm.pty.terminal — custom VT100 emulator."""

from __future__ import annotations

from swarm.pty.terminal import Cell, TerminalEmulator

# --- Helpers ---


def _visible(emu: TerminalEmulator) -> str:
    """Return visible (non-blank) content as a single string."""
    lines = emu.display
    result: list[str] = []
    for line in lines:
        stripped = line.rstrip()
        if stripped:
            result.append(stripped)
    return "\n".join(result)


def _last_n_lines(emu: TerminalEmulator, n: int) -> str:
    """Return last n non-empty lines, like RingBuffer.get_lines()."""
    lines = [line.rstrip() for line in emu.display]
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines[-n:])


# --- Basic text rendering ---


class TestBasicText:
    def test_simple_write(self) -> None:
        emu = TerminalEmulator(cols=80, rows=24)
        emu.feed("Hello, world!")
        assert "Hello, world!" in _visible(emu)

    def test_newline(self) -> None:
        emu = TerminalEmulator(cols=80, rows=24)
        emu.feed("line1\nline2")
        vis = _visible(emu)
        assert "line1" in vis
        assert "line2" in vis

    def test_carriage_return(self) -> None:
        emu = TerminalEmulator(cols=80, rows=24)
        emu.feed("old text\rnew")
        vis = _visible(emu)
        assert "new" in vis
        # "old" is partially overwritten
        assert "old text" not in vis

    def test_backspace(self) -> None:
        emu = TerminalEmulator(cols=80, rows=24)
        emu.feed("ab\bc")
        # Backspace moves left, then 'c' overwrites 'b'
        assert emu.display[0].startswith("ac")

    def test_tab(self) -> None:
        emu = TerminalEmulator(cols=80, rows=24)
        emu.feed("a\tb")
        # Tab goes to column 8
        line = emu.display[0]
        assert line[0] == "a"
        assert line[8] == "b"

    def test_line_wrap(self) -> None:
        emu = TerminalEmulator(cols=10, rows=5)
        emu.feed("1234567890X")  # 11 chars in 10-col terminal
        assert emu.display[0].rstrip() == "1234567890"
        assert emu.display[1].startswith("X")

    def test_empty_input(self) -> None:
        emu = TerminalEmulator(cols=80, rows=24)
        emu.feed("")
        assert _visible(emu) == ""

    def test_bel_ignored(self) -> None:
        emu = TerminalEmulator(cols=80, rows=24)
        emu.feed("hello\x07world")
        assert "helloworld" in _visible(emu)


# --- LNM (newline mode) ---


class TestLNM:
    def test_lnm_off_lf_no_cr(self) -> None:
        emu = TerminalEmulator(cols=80, rows=24)
        emu.feed("ab")
        emu.feed("\n")
        emu.feed("c")
        # Without LNM, LF doesn't return to column 0
        assert emu.cursor == (1, 3)

    def test_lnm_on_lf_implies_cr(self) -> None:
        emu = TerminalEmulator(cols=80, rows=24)
        emu.set_mode_lnm(True)
        emu.feed("ab")
        emu.feed("\n")
        emu.feed("c")
        # With LNM, LF implies CR — column resets to 0
        assert emu.cursor == (1, 1)


# --- SGR (colors and styles) ---


class TestSGR:
    def test_bold(self) -> None:
        emu = TerminalEmulator(cols=80, rows=24)
        emu.feed("\x1b[1mBOLD\x1b[0m")
        cell = emu._buffer[0][0]
        assert cell.char == "B"
        assert cell.bold is True

    def test_italic(self) -> None:
        emu = TerminalEmulator(cols=80, rows=24)
        emu.feed("\x1b[3mtext\x1b[0m")
        assert emu._buffer[0][0].italic is True

    def test_underline(self) -> None:
        emu = TerminalEmulator(cols=80, rows=24)
        emu.feed("\x1b[4mtext\x1b[0m")
        assert emu._buffer[0][0].underline is True

    def test_dim(self) -> None:
        emu = TerminalEmulator(cols=80, rows=24)
        emu.feed("\x1b[2mtext\x1b[0m")
        assert emu._buffer[0][0].dim is True

    def test_reverse(self) -> None:
        emu = TerminalEmulator(cols=80, rows=24)
        emu.feed("\x1b[7mtext\x1b[0m")
        assert emu._buffer[0][0].reverse is True

    def test_strikethrough(self) -> None:
        emu = TerminalEmulator(cols=80, rows=24)
        emu.feed("\x1b[9mtext\x1b[0m")
        assert emu._buffer[0][0].strikethrough is True

    def test_fg_color(self) -> None:
        emu = TerminalEmulator(cols=80, rows=24)
        emu.feed("\x1b[31mred\x1b[0m")
        assert emu._buffer[0][0].fg == "red"

    def test_bg_color(self) -> None:
        emu = TerminalEmulator(cols=80, rows=24)
        emu.feed("\x1b[42mtext\x1b[0m")
        assert emu._buffer[0][0].bg == "green"

    def test_bright_color(self) -> None:
        emu = TerminalEmulator(cols=80, rows=24)
        emu.feed("\x1b[91mtext\x1b[0m")
        assert emu._buffer[0][0].fg == "brightred"

    def test_256_color(self) -> None:
        emu = TerminalEmulator(cols=80, rows=24)
        emu.feed("\x1b[38;5;208mtext\x1b[0m")
        # 208 = color cube index → some hex color
        assert emu._buffer[0][0].fg != "default"

    def test_truecolor_rgb(self) -> None:
        emu = TerminalEmulator(cols=80, rows=24)
        emu.feed("\x1b[38;2;255;128;0mtext\x1b[0m")
        assert emu._buffer[0][0].fg == "ff8000"

    def test_sgr_reset(self) -> None:
        emu = TerminalEmulator(cols=80, rows=24)
        emu.feed("\x1b[1;31mred bold\x1b[0mnormal")
        last_normal = emu._buffer[0][8]  # 'n' in 'normal'
        assert last_normal.bold is False
        assert last_normal.fg == "default"

    def test_reset_dim_with_22(self) -> None:
        emu = TerminalEmulator(cols=80, rows=24)
        emu.feed("\x1b[2mfoo\x1b[22mbar")
        assert emu._buffer[0][0].dim is True
        assert emu._buffer[0][3].dim is False

    def test_default_fg_reset(self) -> None:
        emu = TerminalEmulator(cols=80, rows=24)
        emu.feed("\x1b[31mred\x1b[39mdef")
        assert emu._buffer[0][3].fg == "default"

    def test_default_bg_reset(self) -> None:
        emu = TerminalEmulator(cols=80, rows=24)
        emu.feed("\x1b[42mgreen\x1b[49mdef")
        assert emu._buffer[0][5].bg == "default"


# --- Cursor movement ---


class TestCursorMovement:
    def test_cursor_up(self) -> None:
        emu = TerminalEmulator(cols=80, rows=24)
        emu.feed("line1\nline2\x1b[1A")
        assert emu.cursor[0] == 0

    def test_cursor_down(self) -> None:
        emu = TerminalEmulator(cols=80, rows=24)
        emu.feed("\x1b[3B")
        assert emu.cursor[0] == 3

    def test_cursor_forward(self) -> None:
        emu = TerminalEmulator(cols=80, rows=24)
        emu.feed("\x1b[5C")
        assert emu.cursor[1] == 5

    def test_cursor_backward(self) -> None:
        emu = TerminalEmulator(cols=80, rows=24)
        emu.feed("hello\x1b[3D")
        assert emu.cursor[1] == 2

    def test_cursor_position(self) -> None:
        emu = TerminalEmulator(cols=80, rows=24)
        emu.feed("\x1b[5;10H")
        assert emu.cursor == (4, 9)  # 1-based → 0-based

    def test_cursor_home(self) -> None:
        emu = TerminalEmulator(cols=80, rows=24)
        emu.feed("some text\x1b[H")
        assert emu.cursor == (0, 0)

    def test_cursor_horizontal_absolute(self) -> None:
        emu = TerminalEmulator(cols=80, rows=24)
        emu.feed("hello\x1b[3G")
        assert emu.cursor[1] == 2  # 1-based → 0-based

    def test_cursor_vertical_absolute(self) -> None:
        emu = TerminalEmulator(cols=80, rows=24)
        emu.feed("\x1b[5d")
        assert emu.cursor[0] == 4  # 1-based → 0-based

    def test_cursor_clamped_to_bounds(self) -> None:
        emu = TerminalEmulator(cols=10, rows=5)
        emu.feed("\x1b[999;999H")
        assert emu.cursor == (4, 9)


# --- Erase ---


class TestErase:
    def test_erase_to_end_of_screen(self) -> None:
        emu = TerminalEmulator(cols=80, rows=24)
        emu.feed("hello\nworld\x1b[1;3H\x1b[0J")
        # Row 0 col 2 onwards and all subsequent rows should be cleared
        assert emu.display[0][:2] == "he"
        assert emu.display[0][2:5].strip() == ""

    def test_erase_to_start_of_screen(self) -> None:
        emu = TerminalEmulator(cols=80, rows=24)
        emu.feed("hello\nworld\x1b[2;3H\x1b[1J")
        # All of row 0 and row 1 up to col 2 should be cleared
        assert emu.display[0].strip() == ""

    def test_erase_entire_screen(self) -> None:
        emu = TerminalEmulator(cols=80, rows=24)
        emu.feed("content\x1b[2J")
        assert _visible(emu) == ""

    def test_erase_to_end_of_line(self) -> None:
        emu = TerminalEmulator(cols=80, rows=24)
        emu.feed("hello world\x1b[1;6H\x1b[0K")
        assert emu.display[0][:5] == "hello"
        assert emu.display[0][5:11].strip() == ""

    def test_erase_to_start_of_line(self) -> None:
        emu = TerminalEmulator(cols=80, rows=24)
        emu.feed("hello world\x1b[1;6H\x1b[1K")
        assert emu.display[0][:6].strip() == ""
        assert "world" in emu.display[0]

    def test_erase_entire_line(self) -> None:
        emu = TerminalEmulator(cols=80, rows=24)
        emu.feed("hello\x1b[2K")
        assert emu.display[0].strip() == ""


# --- Alternate screen ---


class TestAlternateScreen:
    def test_enter_alternate_screen(self) -> None:
        emu = TerminalEmulator(cols=80, rows=24)
        emu.feed("main content\x1b[?1049h")
        assert emu.in_alternate_screen is True
        # Alt screen is blank
        assert _visible(emu) == ""

    def test_leave_alternate_screen_restores(self) -> None:
        emu = TerminalEmulator(cols=80, rows=24)
        emu.feed("main content\x1b[?1049halt stuff\x1b[?1049l")
        assert emu.in_alternate_screen is False
        assert "main content" in _visible(emu)

    def test_mode_47(self) -> None:
        emu = TerminalEmulator(cols=80, rows=24)
        emu.feed("main\x1b[?47h")
        assert emu.in_alternate_screen is True
        emu.feed("\x1b[?47l")
        assert emu.in_alternate_screen is False

    def test_mode_1047(self) -> None:
        emu = TerminalEmulator(cols=80, rows=24)
        emu.feed("main\x1b[?1047h")
        assert emu.in_alternate_screen is True
        emu.feed("\x1b[?1047l")
        assert emu.in_alternate_screen is False

    def test_double_enter_no_crash(self) -> None:
        emu = TerminalEmulator(cols=80, rows=24)
        emu.feed("\x1b[?1049h\x1b[?1049h")
        assert emu.in_alternate_screen is True


# --- OSC (terminal title) ---


class TestOSC:
    def test_osc_bel_consumed(self) -> None:
        emu = TerminalEmulator(cols=80, rows=24)
        emu.feed("\x1b]0;My Title\x07visible text")
        assert "visible text" in _visible(emu)
        assert "My Title" not in _visible(emu)

    def test_osc_st_consumed(self) -> None:
        emu = TerminalEmulator(cols=80, rows=24)
        emu.feed("\x1b]0;Title\x1b\\text after")
        assert "text after" in _visible(emu)


# --- Scroll region ---


class TestScrollRegion:
    def test_set_scroll_region(self) -> None:
        emu = TerminalEmulator(cols=80, rows=24)
        emu.feed("\x1b[5;20r")
        assert emu._scroll_top == 4
        assert emu._scroll_bottom == 19

    def test_scroll_up_at_bottom(self) -> None:
        emu = TerminalEmulator(cols=20, rows=5)
        emu.set_mode_lnm(True)
        for i in range(6):
            emu.feed(f"line{i}\n")
        # First line should have scrolled off
        assert "line0" not in _visible(emu)
        assert "line5" in _visible(emu)

    def test_esc_d_index(self) -> None:
        emu = TerminalEmulator(cols=80, rows=5)
        emu.feed("\x1b[5;1H")  # Move to last row
        emu.feed("\x1bD")  # Index (scroll up)
        # Cursor should still be on last row, content scrolled
        assert emu.cursor[0] == 4

    def test_esc_m_reverse_index(self) -> None:
        emu = TerminalEmulator(cols=80, rows=5)
        emu.feed("\x1b[1;1H")  # Move to first row
        emu.feed("\x1bM")  # Reverse index (scroll down)
        assert emu.cursor[0] == 0


# --- Resize ---


class TestResize:
    def test_resize_preserves_content(self) -> None:
        emu = TerminalEmulator(cols=80, rows=24)
        emu.feed("hello")
        emu.resize(48, 120)
        assert "hello" in _visible(emu)

    def test_resize_clamps_cursor(self) -> None:
        emu = TerminalEmulator(cols=80, rows=24)
        emu.feed("\x1b[20;70H")  # row 19, col 69
        emu.resize(10, 40)
        assert emu.cursor[0] <= 9
        assert emu.cursor[1] <= 39

    def test_resize_smaller(self) -> None:
        emu = TerminalEmulator(cols=80, rows=24)
        emu.resize(12, 40)
        assert len(emu._buffer) == 12
        assert len(emu._buffer[0]) == 40

    def test_resize_larger(self) -> None:
        emu = TerminalEmulator(cols=80, rows=24)
        emu.resize(48, 200)
        assert len(emu._buffer) == 48
        assert len(emu._buffer[0]) == 200


# --- render_ansi ---


class TestRenderAnsi:
    def test_empty_screen(self) -> None:
        emu = TerminalEmulator(cols=80, rows=24)
        parts = emu.render_ansi()
        # Should at least have cursor position
        joined = "".join(parts)
        assert "\x1b[" in joined

    def test_plain_text(self) -> None:
        emu = TerminalEmulator(cols=80, rows=24)
        emu.feed("Hello world")
        joined = "".join(emu.render_ansi())
        assert "Hello world" in joined

    def test_colored_text_has_sgr(self) -> None:
        emu = TerminalEmulator(cols=80, rows=24)
        emu.feed("\x1b[31mred text\x1b[0m")
        joined = "".join(emu.render_ansi())
        assert "red text" in joined
        assert "\x1b[31m" in joined

    def test_bold_text_has_sgr(self) -> None:
        emu = TerminalEmulator(cols=80, rows=24)
        emu.feed("\x1b[1mbold\x1b[0m")
        joined = "".join(emu.render_ansi())
        assert "bold" in joined
        assert "\x1b[1m" in joined

    def test_reset_emitted(self) -> None:
        emu = TerminalEmulator(cols=80, rows=24)
        emu.feed("\x1b[1mbold\x1b[0m normal")
        joined = "".join(emu.render_ansi())
        assert "\x1b[0m" in joined


# --- Reset ---


class TestReset:
    def test_reset_clears_screen(self) -> None:
        emu = TerminalEmulator(cols=80, rows=24)
        emu.feed("some content")
        emu.reset()
        assert _visible(emu) == ""
        assert emu.cursor == (0, 0)

    def test_reset_clears_alt_screen(self) -> None:
        emu = TerminalEmulator(cols=80, rows=24)
        emu.feed("\x1b[?1049h")
        emu.reset()
        assert emu.in_alternate_screen is False


# --- Cell dataclass ---


class TestCell:
    def test_default_cell(self) -> None:
        cell = Cell()
        assert cell.char == " "
        assert cell.fg == "default"
        assert cell.bg == "default"
        assert cell.is_default() is True

    def test_non_default_cell(self) -> None:
        cell = Cell(char="A")
        assert cell.is_default() is False

    def test_styled_cell_not_default(self) -> None:
        cell = Cell(bold=True)
        assert cell.is_default() is False


# --- Edge cases ---


class TestEdgeCases:
    def test_control_chars_ignored(self) -> None:
        emu = TerminalEmulator(cols=80, rows=24)
        emu.feed("hello\x01\x02\x03world")
        # Control chars < 0x20 (except recognized ones) should be ignored
        vis = _visible(emu)
        assert "hello" in vis
        assert "world" in vis

    def test_overlong_line_wraps(self) -> None:
        emu = TerminalEmulator(cols=10, rows=3)
        emu.feed("A" * 25)
        # Should fill row 0, wrap to row 1, wrap to row 2
        assert emu.display[0].rstrip() == "A" * 10
        assert emu.display[1].rstrip() == "A" * 10
        assert emu.display[2].rstrip() == "A" * 5

    def test_charset_designator_consumed(self) -> None:
        emu = TerminalEmulator(cols=80, rows=24)
        emu.feed("\x1b(Bhello")
        assert "hello" in _visible(emu)

    def test_unknown_escape_returns_to_ground(self) -> None:
        emu = TerminalEmulator(cols=80, rows=24)
        emu.feed("\x1bZhello")  # ESC Z is not handled
        assert "hello" in _visible(emu)

    def test_keypad_modes_ignored(self) -> None:
        emu = TerminalEmulator(cols=80, rows=24)
        emu.feed("\x1b=text\x1b>")
        assert "text" in _visible(emu)

    def test_esc_c_full_reset(self) -> None:
        emu = TerminalEmulator(cols=80, rows=24)
        emu.feed("content\x1bc")
        assert _visible(emu) == ""


# --- get_styled_rows ---


class TestGetStyledRows:
    def test_basic_styled_rows(self) -> None:
        from swarm.pty.terminal import CellStyle

        emu = TerminalEmulator(cols=20, rows=5)
        emu.feed("hello")
        rows = emu.get_styled_rows()
        assert len(rows) == 1
        text, styles = rows[0]
        assert "hello" in text
        assert len(styles) == 20  # full row width
        assert styles[0] == CellStyle()  # default style

    def test_colored_text_styles(self) -> None:
        from swarm.pty.terminal import CellStyle

        emu = TerminalEmulator(cols=20, rows=5)
        emu.feed("\x1b[32mgreen\x1b[0m")
        rows = emu.get_styled_rows()
        assert len(rows) == 1
        text, styles = rows[0]
        assert "green" in text
        assert styles[0].fg == "green"
        assert styles[0] == CellStyle(fg="green")

    def test_dim_text_styles(self) -> None:
        from swarm.pty.terminal import CellStyle

        emu = TerminalEmulator(cols=30, rows=5)
        emu.feed("\x1b[2mesc to interrupt\x1b[0m")
        rows = emu.get_styled_rows()
        text, styles = rows[0]
        assert "esc to interrupt" in text
        assert styles[0].dim is True
        assert styles[0] == CellStyle(dim=True)

    def test_last_n_limits_rows(self) -> None:
        emu = TerminalEmulator(cols=20, rows=10)
        emu.feed("line1\nline2\nline3")
        rows = emu.get_styled_rows(last_n=2)
        assert len(rows) == 2
        assert "line2" in rows[0][0]
        assert "line3" in rows[1][0]

    def test_empty_screen_returns_empty(self) -> None:
        emu = TerminalEmulator(cols=20, rows=5)
        rows = emu.get_styled_rows()
        assert rows == []

    def test_bold_and_fg_combined(self) -> None:
        from swarm.pty.terminal import CellStyle

        emu = TerminalEmulator(cols=20, rows=5)
        emu.feed("\x1b[1;34mblue bold\x1b[0m")
        rows = emu.get_styled_rows()
        text, styles = rows[0]
        assert "blue bold" in text
        assert styles[0] == CellStyle(fg="blue", bold=True)
