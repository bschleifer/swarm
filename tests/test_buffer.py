"""Tests for swarm.pty.buffer — RingBuffer."""

from __future__ import annotations

import threading

from swarm.pty.buffer import RingBuffer


class TestRingBuffer:
    def test_write_and_snapshot(self) -> None:
        buf = RingBuffer(capacity=100)
        buf.write(b"hello ")
        buf.write(b"world")
        assert buf.snapshot() == b"hello world"

    def test_capacity_wraps(self) -> None:
        buf = RingBuffer(capacity=10)
        buf.write(b"abcdefghij")  # exactly 10
        assert len(buf) == 10
        buf.write(b"XYZ")
        assert len(buf) == 10
        # Oldest bytes discarded, newest retained
        assert buf.snapshot() == b"defghijXYZ"

    def test_large_write_exceeding_capacity(self) -> None:
        buf = RingBuffer(capacity=5)
        buf.write(b"abcdefghij")  # 10 bytes into 5-byte buffer
        assert buf.snapshot() == b"fghij"
        assert len(buf) == 5

    def test_get_lines_basic(self) -> None:
        buf = RingBuffer()
        buf.write(b"line1\nline2\nline3\n")
        result = buf.get_lines(2)
        # Last 2 non-empty lines
        assert "line2" in result
        assert "line3" in result

    def test_get_lines_with_ansi(self) -> None:
        buf = RingBuffer()
        # Simulate colored output
        buf.write(b"\x1b[32mgreen text\x1b[0m\nplain line\n")
        result = buf.get_lines(5)
        assert "green text" in result
        assert "\x1b[32m" not in result
        assert "\x1b[0m" not in result

    def test_get_lines_empty(self) -> None:
        buf = RingBuffer()
        assert buf.get_lines(10) == ""

    def test_strip_ansi_comprehensive(self) -> None:
        # CSI color
        assert RingBuffer.strip_ansi("\x1b[1;31mred\x1b[0m") == "red"
        # OSC title with BEL
        assert RingBuffer.strip_ansi("\x1b]0;my title\x07text") == "text"
        # OSC title with ST
        assert RingBuffer.strip_ansi("\x1b]0;my title\x1b\\text") == "text"
        # Charset designators
        assert RingBuffer.strip_ansi("\x1b(Btext") == "text"
        # Erase sequences
        assert RingBuffer.strip_ansi("\x1b[2Jtext") == "text"
        # Shift in/out
        assert RingBuffer.strip_ansi("\x0ftext\x0e") == "text"
        # Carriage return
        assert RingBuffer.strip_ansi("old\rnew") == "oldnew"
        # Application/normal keypad mode
        assert RingBuffer.strip_ansi("\x1b=text\x1b>") == "text"

    def test_clear(self) -> None:
        buf = RingBuffer()
        buf.write(b"data")
        assert len(buf) == 4
        buf.clear()
        assert len(buf) == 0
        assert buf.snapshot() == b""

    def test_thread_safety(self) -> None:
        """Multiple threads writing concurrently should not corrupt the buffer."""
        buf = RingBuffer(capacity=10000)
        errors: list[Exception] = []

        def writer(prefix: bytes) -> None:
            try:
                for _ in range(100):
                    buf.write(prefix * 10)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=writer, args=(bytes([i + 65]),)) for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
        # Buffer should not exceed capacity
        assert len(buf) <= buf.capacity

    def test_utf8_decode_errors_replaced(self) -> None:
        """Invalid UTF-8 bytes should be replaced, not raise."""
        buf = RingBuffer()
        buf.write(b"valid \xff\xfe invalid\n")
        result = buf.get_lines(5)
        assert "valid" in result
        assert "invalid" in result

    def test_get_lines_respects_count(self) -> None:
        buf = RingBuffer()
        buf.write(b"a\nb\nc\nd\ne\n")
        assert buf.get_lines(2) == "d\ne"
        assert buf.get_lines(1) == "e"
        # More lines than available returns all
        lines = buf.get_lines(100)
        assert lines.count("\n") == 4  # 5 lines, 4 newlines between them
