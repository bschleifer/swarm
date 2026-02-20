"""RingBuffer — fixed-capacity byte buffer for PTY output.

Replaces ``capture_pane(pane_id, lines=N)`` from ``tmux/cell.py``.
All reads/writes are protected by a threading lock so the buffer
can be fed from an asyncio ``add_reader`` callback while synchronous
callers (state detection) read from it.
"""

from __future__ import annotations

import re
import threading

# Pre-compiled ANSI escape sequence stripper.
# Covers: CSI sequences, OSC sequences, charset designators,
# erase/cursor sequences, and shift-in/shift-out control chars.
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


class RingBuffer:
    """Fixed-capacity ring buffer for PTY output bytes.

    Parameters
    ----------
    capacity:
        Maximum bytes to retain.  Defaults to 32KB (~500 lines).
    """

    __slots__ = ("_buf", "_capacity", "_lock")

    def __init__(self, capacity: int = 32768) -> None:
        self._buf = bytearray()
        self._capacity = capacity
        self._lock = threading.Lock()

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

    def get_lines(self, n: int = 35) -> str:
        """Return the last *n* lines as ANSI-stripped text.

        Used by ``classify_pane_content()`` for state detection.
        """
        with self._lock:
            raw = bytes(self._buf)
        text = raw.decode("utf-8", errors="replace")
        text = self.strip_ansi(text)
        lines = text.splitlines()
        return "\n".join(lines[-n:])

    def snapshot(self) -> bytes:
        """Return a copy of all buffered bytes (for initial WS send)."""
        with self._lock:
            return bytes(self._buf)

    def clear(self) -> None:
        """Discard all buffered data."""
        with self._lock:
            self._buf.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._buf)

    @staticmethod
    def strip_ansi(text: str) -> str:
        """Remove ANSI escape sequences from text."""
        return _RE_ANSI.sub("", text)
