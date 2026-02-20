"""RingBuffer — fixed-capacity byte buffer for PTY output.

Replaces ``capture_pane(pane_id, lines=N)`` from ``tmux/cell.py``.
All reads/writes are protected by a threading lock so the buffer
can be fed from an asyncio ``add_reader`` callback while synchronous
callers (state detection) read from it.

Uses pyte to maintain a virtual terminal screen so that
``get_lines()`` returns properly rendered content (equivalent to
``tmux capture-pane``) instead of raw ANSI-stripped byte soup.
"""

from __future__ import annotations

import re
import threading

import pyte

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


class RingBuffer:
    """Fixed-capacity ring buffer for PTY output bytes.

    Maintains a pyte virtual terminal screen for rendered content
    (used by state detection) alongside the raw byte buffer
    (used for WebSocket snapshot).

    Parameters
    ----------
    capacity:
        Maximum bytes to retain.  Defaults to 32KB (~500 lines).
    cols, rows:
        Virtual screen dimensions for pyte rendering.
    """

    __slots__ = ("_buf", "_capacity", "_lock", "_screen", "_stream")

    def __init__(
        self,
        capacity: int = 32768,
        cols: int = _SCREEN_COLS,
        rows: int = _SCREEN_ROWS,
    ) -> None:
        self._buf = bytearray()
        self._capacity = capacity
        self._lock = threading.Lock()
        self._screen = pyte.Screen(cols, rows)
        self._screen.set_mode(pyte.modes.LNM)  # LF implies CR (matches PTY onlcr)
        self._stream = pyte.Stream(self._screen)

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
                self._stream.feed(data.decode("utf-8", errors="replace"))
            except Exception:
                pass  # pyte parse errors should not break output capture

    def get_lines(self, n: int = 35) -> str:
        """Return the last *n* lines of the rendered screen.

        Uses pyte's virtual terminal to produce properly rendered content
        — equivalent to ``tmux capture-pane``.  Cursor positioning,
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

    def snapshot(self) -> bytes:
        """Return a copy of all buffered bytes (for initial WS send)."""
        with self._lock:
            return bytes(self._buf)

    def clear(self) -> None:
        """Discard all buffered data."""
        with self._lock:
            self._buf.clear()
            self._screen.reset()

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
