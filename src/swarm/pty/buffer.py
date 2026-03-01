"""RingBuffer — fixed-capacity byte buffer for PTY output.

All reads/writes are protected by a threading lock so the buffer
can be fed from an asyncio ``add_reader`` callback while synchronous
callers (state detection) read from it.

Uses a custom ``TerminalEmulator`` to maintain a virtual terminal
screen so that ``get_lines()`` returns properly rendered content
instead of raw ANSI-stripped byte soup.
"""

from __future__ import annotations

import codecs
import re
import threading

from swarm.logging import get_logger
from swarm.pty.terminal import CellStyle, TerminalEmulator

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


class RingBuffer:
    """Fixed-capacity ring buffer for PTY output bytes.

    Maintains a virtual terminal screen for rendered content (used by
    state detection) alongside the raw byte buffer (used for WebSocket
    snapshot).

    Parameters
    ----------
    capacity:
        Maximum bytes to retain.  Defaults to 1MB (~15000 lines).
    cols, rows:
        Virtual screen dimensions for terminal rendering.
    """

    __slots__ = ("_buf", "_capacity", "_decoder", "_emulator", "_lock")

    def __init__(
        self,
        capacity: int = 1048576,
        cols: int = _SCREEN_COLS,
        rows: int = _SCREEN_ROWS,
    ) -> None:
        self._buf = bytearray()
        self._capacity = capacity
        self._lock = threading.Lock()
        self._emulator = TerminalEmulator(cols, rows)
        self._emulator.set_mode_lnm(True)  # LF implies CR (matches PTY onlcr)
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
            # Feed to emulator for rendered screen content
            try:
                # Use incremental decoder to handle UTF-8 sequences split across writes
                self._emulator.feed(self._decoder.decode(data))
            except Exception:
                _log.debug("emulator feed error", exc_info=True)

    def get_lines(self, n: int = 35) -> str:
        """Return the last *n* lines of the rendered screen.

        Uses the virtual terminal emulator to produce properly rendered
        content.  Cursor positioning, alternate screen buffer, and other
        TUI sequences are handled correctly.
        """
        with self._lock:
            lines = [line.rstrip() for line in self._emulator.display]
        # Trim trailing empty rows so we return content, not blank padding
        while lines and not lines[-1]:
            lines.pop()
        if not lines:
            return ""
        return "\n".join(lines[-n:])

    def get_styled_lines(self, n: int = 35) -> tuple[str, list[tuple[str, list[CellStyle]]]]:
        """Return plain text AND styled rows under a single lock.

        Returns ``(text, styled_rows)`` where *text* is identical to
        ``get_lines(n)`` output and *styled_rows* is a list of
        ``(line_text, [CellStyle, ...])`` pairs from the emulator.

        Both are captured atomically so they always describe the same
        screen state.
        """
        with self._lock:
            lines = [line.rstrip() for line in self._emulator.display]
            styled_rows = self._emulator.get_styled_rows(last_n=n)

        # Trim trailing empty rows (same as get_lines)
        while lines and not lines[-1]:
            lines.pop()
        if not lines:
            return "", []
        text = "\n".join(lines[-n:])

        # Rstrip each styled row's text to match get_lines() behaviour
        trimmed: list[tuple[str, list[CellStyle]]] = []
        for row_text, row_styles in styled_rows:
            stripped = row_text.rstrip()
            trimmed.append((stripped, row_styles[: len(stripped)] if stripped else []))

        return text, trimmed

    @property
    def in_alternate_screen(self) -> bool:
        """True when the virtual terminal is in alternate screen buffer mode."""
        with self._lock:
            return self._emulator.in_alternate_screen

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
            alt = self._emulator.in_alternate_screen
        cleaned = _strip_leading_partial_utf8(buf)
        cleaned = _strip_leading_partial_csi(cleaned)
        if alt and b"\x1b[?1049h" not in cleaned:
            cleaned = b"\x1b[?1049h" + cleaned
        return cleaned

    def render_ansi(self) -> bytes:
        """Render screen as clean ANSI output for WebSocket initial view.

        Instead of replaying raw buffer bytes (which causes xterm.js rendering
        artifacts on line 0), reconstruct the terminal display from the virtual
        screen with SGR color/style attributes.
        """
        with self._lock:
            alt = self._emulator.in_alternate_screen
            parts = self._emulator.render_ansi()

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
            self._emulator.reset()
            self._decoder.reset()

    def resize(self, cols: int, rows: int) -> None:
        """Resize the virtual screen."""
        with self._lock:
            self._emulator.resize(rows, cols)

    def __len__(self) -> int:
        with self._lock:
            return len(self._buf)

    @staticmethod
    def strip_ansi(text: str) -> str:
        """Remove ANSI escape sequences from text."""
        return _RE_ANSI.sub("", text)
