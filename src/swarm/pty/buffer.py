"""RingBuffer — fixed-capacity byte buffer for PTY output.

All reads/writes are protected by a threading lock so the buffer
can be fed from an asyncio ``add_reader`` callback while synchronous
callers (state detection) read from it.

Uses pyte to maintain a virtual terminal screen so that
``get_lines()`` returns properly rendered content instead of raw
ANSI-stripped byte soup.
"""

from __future__ import annotations

import codecs
import re
import threading

import pyte

from swarm.logging import get_logger

_log = get_logger("pty.buffer")

# Pre-compiled ANSI escape sequence stripper (kept for strip_ansi utility).
_RE_ANSI = re.compile(
    r"\x1b\[[0-9;]*[A-Za-z]"  # CSI (e.g. colors, cursor movement)
    r"|\x1b\][^\x07]*\x07"  # OSC (e.g. terminal title)
    r"|\x1b\][^\x1b]*\x1b\\"  # OSC with ST terminator
    r"|\x1b[()][AB012]"  # Charset designators
    r"|\x1b\[[0-9]*[JKH]"  # Erase/cursor position
    r"|\x1b\[[\?0-9;]*[hlm]"  # Mode set/reset, SGR
    r"|\x1b="  # Application keypad mode
    r"|\x1b>"  # Normal keypad mode
    r"|\x0f"  # SI (shift in)
    r"|\x0e"  # SO (shift out)
    r"|\r"  # Carriage return
)

# Default virtual screen dimensions — matches the PTY default.
_SCREEN_COLS = 200
_SCREEN_ROWS = 50

# Pyte named color → SGR foreground code mapping.
_PYTE_FG: dict[str, str] = {
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
# Background codes are fg + 10.
_PYTE_BG: dict[str, str] = {k: str(int(v) + 10) for k, v in _PYTE_FG.items()}


def _strip_leading_partial_csi(buf: bytes) -> bytes:
    """Strip a partial ANSI CSI sequence from the start of the buffer.

    When the ring buffer wraps, the oldest bytes are deleted, potentially
    cutting through a multi-byte ANSI escape sequence like ``\\x1b[38;5;73m``.
    This leaves the tail end (e.g. ``8;5;73m``) at the start, which terminals
    render as garbled text.

    Scans the first few bytes for the pattern of a truncated CSI sequence:
    optional ``[``, parameter bytes (digits, ``;``, ``?``), and a final byte
    (a letter).  Without a leading ``[``, requires a semicolon in the params
    to avoid false-positives with normal text starting with digits.
    """
    if not buf or buf[0] == 0x1B:
        # Empty, or starts with ESC — sequence is intact, nothing to strip.
        return buf
    i = 0
    limit = min(len(buf), 48)
    has_bracket = buf[i] == ord("[")
    if has_bracket:
        i += 1
    # Consume CSI parameter bytes: 0x30-0x3F (0-9 : ; < = > ?)
    param_start = i
    has_semicolon = False
    while i < limit and 0x30 <= buf[i] <= 0x3F:
        if buf[i] == ord(";"):
            has_semicolon = True
        i += 1
    param_len = i - param_start
    # Final byte must immediately follow params (no intermediate bytes —
    # they overlap with space/punctuation and cause false positives).
    if i < limit and 0x40 <= buf[i] <= 0x7E and param_len > 0:
        # With '[': always strip (very likely CSI).
        # Without '[': require a semicolon to distinguish from normal text.
        if has_bracket or has_semicolon:
            return buf[i + 1 :]
    return buf


def _strip_leading_partial_utf8(buf: bytes) -> bytes:
    """Strip leading bytes until a valid UTF-8 start is found.

    When the ring buffer wraps, we can cut through a multi-byte UTF-8
    sequence.  Trim any leading continuation bytes or invalid starts
    to avoid rendering replacement glyphs at the top of the terminal.
    """
    i = 0
    n = len(buf)
    while i < n:
        need = _utf8_seq_len(buf[i])
        if need == 1:
            return buf[i:]
        if need < 0:
            i += 1
            continue
        if i + need <= n and _has_utf8_continuations(buf, i, need):
            return buf[i:]
        i += 1
    return b""


def _utf8_seq_len(b: int) -> int:
    if b < 0x80:
        return 1
    if 0x80 <= b < 0xC0:
        return -1
    if 0xC2 <= b <= 0xDF:
        return 2
    if 0xE0 <= b <= 0xEF:
        return 3
    if 0xF0 <= b <= 0xF4:
        return 4
    return -1


def _has_utf8_continuations(buf: bytes, start: int, need: int) -> bool:
    for j in range(1, need):
        if not (0x80 <= buf[start + j] < 0xC0):
            return False
    return True


def _color_sgr(color: str, table: dict[str, str], rgb_prefix: str) -> str:
    """Convert a pyte color to an SGR parameter string."""
    if color == "default":
        return "39" if rgb_prefix == "38" else "49"
    if color in table:
        return table[color]
    if len(color) == 6:
        r, g, b = int(color[:2], 16), int(color[2:4], 16), int(color[4:6], 16)
        return f"{rgb_prefix};2;{r};{g};{b}"
    return ""


def _row_extent(row_data: dict[int, object]) -> int:
    """Return the rightmost column with non-default content, or -1."""
    max_col = -1
    for c, char in row_data.items():
        if char.data != " " or char.fg != "default" or char.bg != "default":
            if c > max_col:
                max_col = c
    return max_col


def _render_screen(screen: pyte.Screen) -> list[str]:  # noqa: C901
    """Build a list of ANSI string fragments from the pyte screen buffer."""
    buf = screen.buffer
    default = screen.default_char
    parts: list[str] = []
    # Current SGR state — track to emit changes only.
    c_fg = "default"
    c_bg = "default"
    c_bold = c_ital = c_under = c_rev = c_strike = False

    for row in range(screen.lines):
        row_data = buf.get(row)
        if not row_data:
            continue
        max_col = _row_extent(row_data)
        if max_col < 0:
            continue

        parts.append(f"\x1b[{row + 1};1H")
        for col in range(max_col + 1):
            char = row_data.get(col, default)
            sgr: list[str] = []
            # If any active attribute is being turned off, reset first.
            if (
                (c_bold and not char.bold)
                or (c_ital and not char.italics)
                or (c_under and not char.underscore)
                or (c_rev and not char.reverse)
                or (c_strike and not char.strikethrough)
            ):
                sgr.append("0")
                c_fg = "default"
                c_bg = "default"
                c_bold = c_ital = c_under = c_rev = c_strike = False
            # Turn on new attributes.
            if char.bold and not c_bold:
                sgr.append("1")
            if char.italics and not c_ital:
                sgr.append("3")
            if char.underscore and not c_under:
                sgr.append("4")
            if char.reverse and not c_rev:
                sgr.append("7")
            if char.strikethrough and not c_strike:
                sgr.append("9")
            # Foreground color.
            if char.fg != c_fg:
                code = _color_sgr(char.fg, _PYTE_FG, "38")
                if code:
                    sgr.append(code)
            # Background color.
            if char.bg != c_bg:
                code = _color_sgr(char.bg, _PYTE_BG, "48")
                if code:
                    sgr.append(code)
            if sgr:
                parts.append(f"\x1b[{';'.join(sgr)}m")
                c_fg = char.fg
                c_bg = char.bg
                c_bold = char.bold
                c_ital = char.italics
                c_under = char.underscore
                c_rev = char.reverse
                c_strike = char.strikethrough
            parts.append(char.data)

    # Reset attributes and position cursor.
    has_attrs = (
        c_fg != "default" or c_bg != "default" or c_bold or c_ital or c_under or c_rev or c_strike
    )
    if has_attrs:
        parts.append("\x1b[0m")
    parts.append(f"\x1b[{screen.cursor.y + 1};{screen.cursor.x + 1}H")
    return parts


class RingBuffer:
    """Fixed-capacity ring buffer for PTY output bytes.

    Maintains a pyte virtual terminal screen for rendered content
    (used by state detection) alongside the raw byte buffer
    (used for WebSocket snapshot).

    Parameters
    ----------
    capacity:
        Maximum bytes to retain.  Defaults to 1MB (~15000 lines).
    cols, rows:
        Virtual screen dimensions for pyte rendering.
    """

    __slots__ = ("_buf", "_capacity", "_lock", "_screen", "_stream", "_decoder")

    def __init__(
        self,
        capacity: int = 1048576,
        cols: int = _SCREEN_COLS,
        rows: int = _SCREEN_ROWS,
    ) -> None:
        self._buf = bytearray()
        self._capacity = capacity
        self._lock = threading.Lock()
        self._screen = pyte.Screen(cols, rows)
        self._screen.set_mode(pyte.modes.LNM)  # LF implies CR (matches PTY onlcr)
        self._stream = pyte.Stream(self._screen)
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")

    @property
    def capacity(self) -> int:
        return self._capacity

    def write(self, data: bytes) -> None:
        """Append data, discarding oldest bytes if capacity is exceeded."""
        with self._lock:
            self._buf.extend(data)
            overflow = len(self._buf) - self._capacity
            if overflow > 0:
                del self._buf[:overflow]
            # Feed to pyte for rendered screen content
            try:
                # Use incremental decoder to handle UTF-8 sequences split across writes
                self._stream.feed(self._decoder.decode(data))
            except Exception:
                _log.debug("pyte feed error", exc_info=True)

    def get_lines(self, n: int = 35) -> str:
        """Return the last *n* lines of the rendered screen.

        Uses pyte's virtual terminal to produce properly rendered content.
        Cursor positioning,
        alternate screen buffer, and other TUI sequences are handled
        correctly by the terminal emulator.
        """
        with self._lock:
            lines = [line.rstrip() for line in self._screen.display]
        # Trim trailing empty rows so we return content, not blank padding
        while lines and not lines[-1]:
            lines.pop()
        if not lines:
            return ""
        return "\n".join(lines[-n:])

    @property
    def in_alternate_screen(self) -> bool:
        """True when the virtual terminal is in alternate screen buffer mode."""
        # pyte stores private modes shifted left by 5 bits.
        _ALT_SCREEN_MODES = {47 << 5, 1047 << 5, 1049 << 5}
        with self._lock:
            return bool(self._screen.mode & _ALT_SCREEN_MODES)

    def snapshot(self) -> bytes:
        """Return buffered bytes for WebSocket send or reconnect.

        Processing pipeline:
        1. Strip any partial ANSI CSI sequence left at the start after
           the ring buffer discarded oldest bytes mid-sequence.
        2. If the virtual terminal is in alternate screen mode but the
           raw buffer has rolled past the switch sequence, prepend it
           so clients enter the correct buffer mode on replay.
        """
        with self._lock:
            buf = bytes(self._buf)
            alt = bool(self._screen.mode & {47 << 5, 1047 << 5, 1049 << 5})
        cleaned = _strip_leading_partial_utf8(buf)
        cleaned = _strip_leading_partial_csi(cleaned)
        if alt and b"\x1b[?1049h" not in cleaned:
            cleaned = b"\x1b[?1049h" + cleaned
        return cleaned

    def render_ansi(self) -> bytes:
        """Render pyte screen as clean ANSI output for WebSocket initial view.

        Instead of replaying raw buffer bytes (which causes xterm.js rendering
        artifacts on line 0), reconstruct the terminal display from pyte's
        virtual screen with SGR color/style attributes.
        """
        with self._lock:
            screen = self._screen
            _ALT = {47 << 5, 1047 << 5, 1049 << 5}
            alt = bool(screen.mode & _ALT)
            parts = _render_screen(screen)

        if not parts:
            return b""
        result = "".join(parts).encode("utf-8")
        if alt:
            result = b"\x1b[?1049h" + result
        return result

    def clear(self) -> None:
        """Discard all buffered data."""
        with self._lock:
            self._buf.clear()
            self._screen.reset()
            self._decoder.reset()

    def resize(self, cols: int, rows: int) -> None:
        """Resize the virtual screen."""
        with self._lock:
            self._screen.resize(rows, cols)

    def __len__(self) -> int:
        with self._lock:
            return len(self._buf)

    @staticmethod
    def strip_ansi(text: str) -> str:
        """Remove ANSI escape sequences from text."""
        return _RE_ANSI.sub("", text)
