"""Lightweight VT100 terminal emulator — replaces pyte.

Handles the escape sequences that Claude Code, Gemini CLI, and Codex
actually emit.  Designed for speed: flat list-of-lists screen buffer,
no per-cell object allocation for default cells.

Supported sequences
-------------------
Text:      printable chars, LF, CR, BS, TAB, BEL (ignored)
SGR:       CSI ... m  (colors, bold, italic, underline, dim, reverse, strike)
Erase:     CSI J, CSI K
Cursor:    CSI A/B/C/D/H/f/G/d
Alt screen: CSI ?1049h/l, CSI ?47h/l, CSI ?1047h/l
Scroll:    CSI r (set region), ESC D (index), ESC M (reverse index)
Modes:     CSI ?h/l (DEC private), LNM (newline mode)
OSC:       ESC ] ... BEL/ST  (consumed, ignored)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import NamedTuple


class CellStyle(NamedTuple):
    """Lightweight style snapshot for state detection.

    Only the four attributes needed for style-aware classification.
    """

    fg: str = "default"
    bg: str = "default"
    bold: bool = False
    dim: bool = False


def cell_style(cell: Cell) -> CellStyle:
    """Extract a CellStyle from a Cell."""
    return CellStyle(fg=cell.fg, bg=cell.bg, bold=cell.bold, dim=cell.dim)


@dataclass(slots=True)
class Cell:
    """A single terminal cell with character and style attributes."""

    char: str = " "
    fg: str = "default"
    bg: str = "default"
    bold: bool = False
    dim: bool = False
    italic: bool = False
    underline: bool = False
    reverse: bool = False
    strikethrough: bool = False

    def is_default(self) -> bool:
        """True if this cell has default appearance (space, no style)."""
        return (
            self.char == " "
            and self.fg == "default"
            and self.bg == "default"
            and not self.bold
            and not self.dim
            and not self.italic
            and not self.underline
            and not self.reverse
            and not self.strikethrough
        )


# SGR code → (attribute_name, value) for simple on/off attributes.
_SGR_ATTRS: dict[int, tuple[str, bool]] = {
    1: ("bold", True),
    2: ("dim", True),
    3: ("italic", True),
    4: ("underline", True),
    7: ("reverse", True),
    9: ("strikethrough", True),
    21: ("bold", False),  # doubly underlined or bold off (varies)
    22: ("bold", False),
    23: ("italic", False),
    24: ("underline", False),
    27: ("reverse", False),
    29: ("strikethrough", False),
}

# Named colors for SGR 30-37 / 40-47 / 90-97 / 100-107.
_FG_COLORS: dict[int, str] = {
    30: "black",
    31: "red",
    32: "green",
    33: "brown",
    34: "blue",
    35: "magenta",
    36: "cyan",
    37: "white",
    90: "brightblack",
    91: "brightred",
    92: "brightgreen",
    93: "brightbrown",
    94: "brightblue",
    95: "brightmagenta",
    96: "brightcyan",
    97: "brightwhite",
}
_BG_COLORS: dict[int, str] = {k + 10: v for k, v in _FG_COLORS.items()}

# Named color → SGR foreground code mapping (for render_ansi).
_COLOR_TO_FG_SGR: dict[str, str] = {
    "black": "30",
    "red": "31",
    "green": "32",
    "brown": "33",
    "blue": "34",
    "magenta": "35",
    "cyan": "36",
    "white": "37",
    "brightblack": "90",
    "brightred": "91",
    "brightgreen": "92",
    "brightbrown": "93",
    "brightblue": "94",
    "brightmagenta": "95",
    "brightcyan": "96",
    "brightwhite": "97",
}
_COLOR_TO_BG_SGR: dict[str, str] = {k: str(int(v) + 10) for k, v in _COLOR_TO_FG_SGR.items()}


def _color_sgr(color: str, table: dict[str, str], rgb_prefix: str) -> str:
    """Convert a named/hex color to an SGR parameter string."""
    if color == "default":
        return "39" if rgb_prefix == "38" else "49"
    if color in table:
        return table[color]
    if len(color) == 6:
        try:
            r, g, b = int(color[:2], 16), int(color[2:4], 16), int(color[4:6], 16)
            return f"{rgb_prefix};2;{r};{g};{b}"
        except ValueError:
            pass
    return ""


def _make_row(cols: int) -> list[Cell]:
    """Create a new row of default cells."""
    return [Cell() for _ in range(cols)]


@dataclass(slots=True)
class _SavedScreen:
    """Saved main screen state when entering alternate screen."""

    buffer: list[list[Cell]] = field(default_factory=list)
    cursor_row: int = 0
    cursor_col: int = 0


class TerminalEmulator:
    """Lightweight VT100 terminal emulator.

    Parameters
    ----------
    cols:
        Number of columns (default 200, matching PTY default).
    rows:
        Number of rows (default 50).
    """

    __slots__ = (
        "_attrs",
        "_buffer",
        "_cols",
        "_cursor_col",
        "_cursor_row",
        "_esc_buf",
        "_in_alt_screen",
        "_lnm",
        "_parse_state",
        "_rows",
        "_saved_screen",
        "_scroll_bottom",
        "_scroll_top",
    )

    def __init__(self, cols: int = 200, rows: int = 50) -> None:
        self._cols = cols
        self._rows = rows
        self._buffer: list[list[Cell]] = [_make_row(cols) for _ in range(rows)]
        self._cursor_row = 0
        self._cursor_col = 0
        self._attrs = Cell()  # current style template
        self._in_alt_screen = False
        self._saved_screen: _SavedScreen | None = None
        self._lnm = False  # Line feed/Newline mode (LF implies CR)
        self._scroll_top = 0
        self._scroll_bottom = rows - 1
        # Parser state: "ground", "escape", "csi", "osc"
        self._parse_state = "ground"
        self._esc_buf = ""

    @property
    def in_alternate_screen(self) -> bool:
        """True when the terminal is in alternate screen buffer mode."""
        return self._in_alt_screen

    @property
    def cursor(self) -> tuple[int, int]:
        """Current cursor position as (row, col)."""
        return self._cursor_row, self._cursor_col

    @property
    def display(self) -> list[str]:
        """Plain text lines — same interface as pyte.Screen.display."""
        result: list[str] = []
        for row in self._buffer:
            result.append("".join(cell.char for cell in row))
        return result

    def get_styled_rows(self, last_n: int | None = None) -> list[tuple[str, list[CellStyle]]]:
        """Return rows as (text, styles) pairs.

        Each row is a tuple of the plain-text line and a list of
        ``CellStyle`` values, one per character.  Trailing empty rows
        are trimmed.  If *last_n* is given, only the last *last_n*
        non-empty rows are returned.
        """
        rows: list[tuple[str, list[CellStyle]]] = []
        for row in self._buffer:
            text = "".join(c.char for c in row)
            styles = [cell_style(c) for c in row]
            rows.append((text, styles))
        # Trim trailing empty rows (all spaces, default style)
        while rows and not rows[-1][0].rstrip():
            rows.pop()
        if last_n is not None and last_n < len(rows):
            rows = rows[-last_n:]
        return rows

    def set_mode_lnm(self, enabled: bool = True) -> None:
        """Set LNM (line feed/newline mode): LF implies CR."""
        self._lnm = enabled

    def feed(self, text: str) -> None:  # noqa: C901
        """Process decoded text through the terminal emulator."""
        for ch in text:
            state = self._parse_state

            if state == "ground":
                if ch == "\x1b":
                    self._parse_state = "escape"
                    self._esc_buf = ""
                elif ch == "\n":
                    self._linefeed()
                elif ch == "\r":
                    self._cursor_col = 0
                elif ch == "\b":
                    if self._cursor_col > 0:
                        self._cursor_col -= 1
                elif ch == "\t":
                    # Tab to next 8-column stop
                    self._cursor_col = min((self._cursor_col // 8 + 1) * 8, self._cols - 1)
                elif ch == "\x07":
                    pass  # BEL — ignore
                elif ch >= " ":
                    self._put_char(ch)

            elif state == "escape":
                if ch == "[":
                    self._parse_state = "csi"
                    self._esc_buf = ""
                elif ch == "]":
                    self._parse_state = "osc"
                    self._esc_buf = ""
                elif ch == "D":
                    # Index (scroll up)
                    self._index()
                    self._parse_state = "ground"
                elif ch == "M":
                    # Reverse index (scroll down)
                    self._reverse_index()
                    self._parse_state = "ground"
                elif ch in ("(", ")"):
                    # Charset designator — consume one more char
                    self._parse_state = "charset"
                elif ch == "c":
                    # Full reset
                    self.reset()
                elif ch == "=":
                    # Application keypad mode — ignore
                    self._parse_state = "ground"
                elif ch == ">":
                    # Normal keypad mode — ignore
                    self._parse_state = "ground"
                else:
                    # Unknown escape — return to ground
                    self._parse_state = "ground"

            elif state == "charset":
                # Consume the charset designation character and return
                self._parse_state = "ground"

            elif state == "csi":
                if 0x20 <= ord(ch) <= 0x3F:
                    # Parameter byte or intermediate byte
                    self._esc_buf += ch
                elif 0x40 <= ord(ch) <= 0x7E:
                    # Final byte — dispatch
                    self._dispatch_csi(self._esc_buf, ch)
                    self._parse_state = "ground"
                else:
                    # Invalid — abort sequence
                    self._parse_state = "ground"

            elif state == "osc":
                if ch == "\x07":
                    # BEL terminates OSC
                    self._parse_state = "ground"
                elif ch == "\x1b":
                    # Possible ST (ESC \)
                    self._parse_state = "osc_st"
                else:
                    self._esc_buf += ch

            elif state == "osc_st":
                # Expecting backslash for ST
                self._parse_state = "ground"  # consume regardless

    def _put_char(self, ch: str) -> None:
        """Write a printable character at the cursor, advancing it."""
        if self._cursor_col >= self._cols:
            # Auto-wrap
            self._cursor_col = 0
            self._linefeed()

        row = self._buffer[self._cursor_row]
        cell = row[self._cursor_col]
        cell.char = ch
        cell.fg = self._attrs.fg
        cell.bg = self._attrs.bg
        cell.bold = self._attrs.bold
        cell.dim = self._attrs.dim
        cell.italic = self._attrs.italic
        cell.underline = self._attrs.underline
        cell.reverse = self._attrs.reverse
        cell.strikethrough = self._attrs.strikethrough
        self._cursor_col += 1

    def _linefeed(self) -> None:
        """Handle LF: move down, scroll if at bottom of scroll region."""
        if self._lnm:
            self._cursor_col = 0
        if self._cursor_row == self._scroll_bottom:
            self._scroll_up()
        elif self._cursor_row < self._rows - 1:
            self._cursor_row += 1

    def _index(self) -> None:
        """ESC D — move cursor down, scroll if at scroll bottom."""
        if self._cursor_row == self._scroll_bottom:
            self._scroll_up()
        elif self._cursor_row < self._rows - 1:
            self._cursor_row += 1

    def _reverse_index(self) -> None:
        """ESC M — move cursor up, scroll down if at scroll top."""
        if self._cursor_row == self._scroll_top:
            self._scroll_down()
        elif self._cursor_row > 0:
            self._cursor_row -= 1

    def _scroll_up(self) -> None:
        """Scroll the scroll region up by one line."""
        del self._buffer[self._scroll_top]
        self._buffer.insert(self._scroll_bottom, _make_row(self._cols))

    def _scroll_down(self) -> None:
        """Scroll the scroll region down by one line."""
        del self._buffer[self._scroll_bottom]
        self._buffer.insert(self._scroll_top, _make_row(self._cols))

    def _dispatch_csi(self, params_str: str, final: str) -> None:  # noqa: C901
        """Dispatch a CSI sequence."""
        # Check for DEC private mode prefix
        private = params_str.startswith("?")
        if private:
            params_str = params_str[1:]

        # Parse semicolon-separated params
        params = self._parse_params(params_str)

        if private:
            self._handle_dec_mode(params, final)
            return

        if final == "m":
            self._handle_sgr(params)
        elif final == "H" or final == "f":
            # Cursor position: CSI row;col H
            row = (params[0] if params else 1) - 1
            col = (params[1] if len(params) > 1 else 1) - 1
            self._cursor_row = max(0, min(row, self._rows - 1))
            self._cursor_col = max(0, min(col, self._cols - 1))
        elif final == "A":
            # Cursor up
            n = params[0] if params else 1
            self._cursor_row = max(0, self._cursor_row - n)
        elif final == "B":
            # Cursor down
            n = params[0] if params else 1
            self._cursor_row = min(self._rows - 1, self._cursor_row + n)
        elif final == "C":
            # Cursor forward
            n = params[0] if params else 1
            self._cursor_col = min(self._cols - 1, self._cursor_col + n)
        elif final == "D":
            # Cursor backward
            n = params[0] if params else 1
            self._cursor_col = max(0, self._cursor_col - n)
        elif final == "G":
            # Cursor horizontal absolute
            col = (params[0] if params else 1) - 1
            self._cursor_col = max(0, min(col, self._cols - 1))
        elif final == "d":
            # Cursor vertical absolute
            row = (params[0] if params else 1) - 1
            self._cursor_row = max(0, min(row, self._rows - 1))
        elif final == "J":
            self._erase_display(params[0] if params else 0)
        elif final == "K":
            self._erase_line(params[0] if params else 0)
        elif final == "r":
            # Set scrolling region
            top = (params[0] if params else 1) - 1
            bottom = (params[1] if len(params) > 1 else self._rows) - 1
            self._scroll_top = max(0, min(top, self._rows - 1))
            self._scroll_bottom = max(0, min(bottom, self._rows - 1))
            # Cursor moves to home after setting scroll region
            self._cursor_row = 0
            self._cursor_col = 0
        elif final == "L":
            # Insert lines
            n = params[0] if params else 1
            for _ in range(n):
                if self._scroll_bottom < len(self._buffer):
                    del self._buffer[self._scroll_bottom]
                self._buffer.insert(self._cursor_row, _make_row(self._cols))
        elif final == "M":
            # Delete lines
            n = params[0] if params else 1
            for _ in range(n):
                if self._cursor_row < len(self._buffer):
                    del self._buffer[self._cursor_row]
                self._buffer.insert(self._scroll_bottom, _make_row(self._cols))

    def _parse_params(self, params_str: str) -> list[int]:
        """Parse CSI parameter string into list of ints."""
        if not params_str:
            return []
        parts = params_str.split(";")
        result: list[int] = []
        for p in parts:
            try:
                result.append(int(p) if p else 0)
            except ValueError:
                result.append(0)
        return result

    def _handle_dec_mode(self, params: list[int], final: str) -> None:
        """Handle DEC private mode set/reset (CSI ? ... h/l)."""
        for mode in params:
            if mode in (1049, 47, 1047):
                if final == "h":
                    self._enter_alt_screen()
                elif final == "l":
                    self._leave_alt_screen()
            elif mode == 20:
                # LNM
                self._lnm = final == "h"

    def _enter_alt_screen(self) -> None:
        """Enter alternate screen buffer."""
        if self._in_alt_screen:
            return
        self._saved_screen = _SavedScreen(
            buffer=self._buffer,
            cursor_row=self._cursor_row,
            cursor_col=self._cursor_col,
        )
        self._buffer = [_make_row(self._cols) for _ in range(self._rows)]
        self._cursor_row = 0
        self._cursor_col = 0
        self._in_alt_screen = True

    def _leave_alt_screen(self) -> None:
        """Leave alternate screen buffer, restoring main screen."""
        if not self._in_alt_screen:
            return
        if self._saved_screen is not None:
            self._buffer = self._saved_screen.buffer
            self._cursor_row = self._saved_screen.cursor_row
            self._cursor_col = self._saved_screen.cursor_col
            self._saved_screen = None
        self._in_alt_screen = False

    def _handle_sgr(self, params: list[int]) -> None:  # noqa: C901
        """Handle SGR (Select Graphic Rendition) sequence."""
        if not params:
            params = [0]

        i = 0
        while i < len(params):
            code = params[i]

            if code == 0:
                # Reset all
                self._attrs = Cell()
            elif code in _SGR_ATTRS:
                attr, val = _SGR_ATTRS[code]
                setattr(self._attrs, attr, val)
                # code 22 also resets dim
                if code == 22:
                    self._attrs.dim = False
            elif code in _FG_COLORS:
                self._attrs.fg = _FG_COLORS[code]
            elif code in _BG_COLORS:
                self._attrs.bg = _BG_COLORS[code]
            elif code == 38:
                # Extended foreground: 38;5;n or 38;2;r;g;b
                color, consumed = self._parse_extended_color(params, i)
                if color:
                    self._attrs.fg = color
                i += consumed
            elif code == 48:
                # Extended background: 48;5;n or 48;2;r;g;b
                color, consumed = self._parse_extended_color(params, i)
                if color:
                    self._attrs.bg = color
                i += consumed
            elif code == 39:
                self._attrs.fg = "default"
            elif code == 49:
                self._attrs.bg = "default"

            i += 1

    def _parse_extended_color(self, params: list[int], i: int) -> tuple[str, int]:
        """Parse 38;5;n or 38;2;r;g;b extended color sequences.

        Returns (color_string, extra_params_consumed).
        """
        if i + 1 >= len(params):
            return "", 0
        kind = params[i + 1]
        if kind == 5 and i + 2 < len(params):
            # 256-color: convert index to hex RGB
            idx = params[i + 2]
            return self._color256_to_hex(idx), 2
        if kind == 2 and i + 4 < len(params):
            # True color RGB
            r, g, b = params[i + 2], params[i + 3], params[i + 4]
            return f"{r:02x}{g:02x}{b:02x}", 4
        return "", 0

    @staticmethod
    def _color256_to_hex(idx: int) -> str:
        """Convert a 256-color index to a 6-digit hex string."""
        if idx < 16:
            # Standard + bright colors → use named
            names = [
                "black",
                "red",
                "green",
                "brown",
                "blue",
                "magenta",
                "cyan",
                "white",
                "brightblack",
                "brightred",
                "brightgreen",
                "brightbrown",
                "brightblue",
                "brightmagenta",
                "brightcyan",
                "brightwhite",
            ]
            return names[idx] if idx < len(names) else "default"
        if idx < 232:
            # 6×6×6 color cube
            idx -= 16
            r = (idx // 36) * 51
            g = ((idx % 36) // 6) * 51
            b = (idx % 6) * 51
            return f"{r:02x}{g:02x}{b:02x}"
        # Grayscale ramp: 232-255
        v = (idx - 232) * 10 + 8
        return f"{v:02x}{v:02x}{v:02x}"

    def _erase_display(self, mode: int) -> None:
        """CSI J — erase in display."""
        if mode == 0:
            # Erase from cursor to end of screen
            row = self._buffer[self._cursor_row]
            for c in range(self._cursor_col, self._cols):
                row[c] = Cell()
            for r in range(self._cursor_row + 1, self._rows):
                self._buffer[r] = _make_row(self._cols)
        elif mode == 1:
            # Erase from start to cursor
            for r in range(self._cursor_row):
                self._buffer[r] = _make_row(self._cols)
            row = self._buffer[self._cursor_row]
            for c in range(self._cursor_col + 1):
                row[c] = Cell()
        elif mode == 2:
            # Erase entire screen
            for r in range(self._rows):
                self._buffer[r] = _make_row(self._cols)

    def _erase_line(self, mode: int) -> None:
        """CSI K — erase in line."""
        row = self._buffer[self._cursor_row]
        if mode == 0:
            # Erase from cursor to end of line
            for c in range(self._cursor_col, self._cols):
                row[c] = Cell()
        elif mode == 1:
            # Erase from start to cursor
            for c in range(self._cursor_col + 1):
                row[c] = Cell()
        elif mode == 2:
            # Erase entire line
            self._buffer[self._cursor_row] = _make_row(self._cols)

    def render_ansi(self) -> list[str]:  # noqa: C901
        """Build ANSI string fragments from the screen buffer.

        Returns a list of strings containing cursor positioning and SGR
        sequences that reproduce the current screen state.
        """
        parts: list[str] = []
        # Current SGR state — emit changes only
        c_fg = "default"
        c_bg = "default"
        c_bold = c_dim = c_ital = c_under = c_rev = c_strike = False

        for row_idx in range(self._rows):
            row = self._buffer[row_idx]
            # Find rightmost non-default cell
            max_col = -1
            for c in range(self._cols - 1, -1, -1):
                if not row[c].is_default():
                    max_col = c
                    break
            if max_col < 0:
                continue

            parts.append(f"\x1b[{row_idx + 1};1H")
            for col in range(max_col + 1):
                cell = row[col]
                sgr: list[str] = []

                # If any active attribute is being turned off, reset first
                if (
                    (c_bold and not cell.bold)
                    or (c_dim and not cell.dim)
                    or (c_ital and not cell.italic)
                    or (c_under and not cell.underline)
                    or (c_rev and not cell.reverse)
                    or (c_strike and not cell.strikethrough)
                ):
                    sgr.append("0")
                    c_fg = "default"
                    c_bg = "default"
                    c_bold = c_dim = c_ital = c_under = c_rev = c_strike = False

                # Turn on new attributes
                if cell.bold and not c_bold:
                    sgr.append("1")
                if cell.dim and not c_dim:
                    sgr.append("2")
                if cell.italic and not c_ital:
                    sgr.append("3")
                if cell.underline and not c_under:
                    sgr.append("4")
                if cell.reverse and not c_rev:
                    sgr.append("7")
                if cell.strikethrough and not c_strike:
                    sgr.append("9")

                # Foreground color
                if cell.fg != c_fg:
                    code = _color_sgr(cell.fg, _COLOR_TO_FG_SGR, "38")
                    if code:
                        sgr.append(code)
                # Background color
                if cell.bg != c_bg:
                    code = _color_sgr(cell.bg, _COLOR_TO_BG_SGR, "48")
                    if code:
                        sgr.append(code)

                if sgr:
                    parts.append(f"\x1b[{';'.join(sgr)}m")
                    c_fg = cell.fg
                    c_bg = cell.bg
                    c_bold = cell.bold
                    c_dim = cell.dim
                    c_ital = cell.italic
                    c_under = cell.underline
                    c_rev = cell.reverse
                    c_strike = cell.strikethrough

                parts.append(cell.char)

        # Reset attributes and position cursor
        has_attrs = (
            c_fg != "default"
            or c_bg != "default"
            or c_bold
            or c_dim
            or c_ital
            or c_under
            or c_rev
            or c_strike
        )
        if has_attrs:
            parts.append("\x1b[0m")
        parts.append(f"\x1b[{self._cursor_row + 1};{self._cursor_col + 1}H")
        return parts

    def resize(self, rows: int, cols: int) -> None:
        """Resize the terminal, preserving content where possible."""
        old_rows = self._rows
        old_cols = self._cols
        self._rows = rows
        self._cols = cols

        # Adjust existing rows to new column width
        for r in range(min(old_rows, rows)):
            row = self._buffer[r]
            if cols > old_cols:
                row.extend(Cell() for _ in range(cols - old_cols))
            elif cols < old_cols:
                del row[cols:]

        # Add or remove rows
        if rows > old_rows:
            for _ in range(rows - old_rows):
                self._buffer.append(_make_row(cols))
        elif rows < old_rows:
            del self._buffer[rows:]

        # Clamp cursor
        self._cursor_row = min(self._cursor_row, rows - 1)
        self._cursor_col = min(self._cursor_col, cols - 1)

        # Reset scroll region to full screen
        self._scroll_top = 0
        self._scroll_bottom = rows - 1

    def reset(self) -> None:
        """Reset the terminal to its initial state."""
        self._buffer = [_make_row(self._cols) for _ in range(self._rows)]
        self._cursor_row = 0
        self._cursor_col = 0
        self._attrs = Cell()
        self._in_alt_screen = False
        self._saved_screen = None
        self._lnm = False
        self._scroll_top = 0
        self._scroll_bottom = self._rows - 1
        self._parse_state = "ground"
        self._esc_buf = ""
