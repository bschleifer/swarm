"""Tests for swarm.pty.process — WorkerProcess."""

from __future__ import annotations

import asyncio
import json

import pytest

from swarm.pty.buffer import RingBuffer
from swarm.pty.holder import PtyHolder
from swarm.pty.process import ProcessError, WorkerProcess


@pytest.fixture()
def socket_path(tmp_path):
    return str(tmp_path / "test-process.sock")


@pytest.fixture()
async def holder(socket_path):
    h = PtyHolder(socket_path)
    task = asyncio.create_task(h.serve())
    for _ in range(50):
        if h._server is not None:
            break
        await asyncio.sleep(0.05)
    yield h
    h._running = False
    h._shutdown_all()
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


async def _send_cmd(socket_path: str, cmd: dict) -> dict:
    """Send a single command to the holder and return the response."""
    reader, writer = await asyncio.open_unix_connection(socket_path)
    writer.write(json.dumps(cmd).encode() + b"\n")
    await writer.drain()
    line = await reader.readline()
    writer.close()
    await writer.wait_closed()
    return json.loads(line)


async def _make_process(
    holder: PtyHolder,
    socket_path: str,
    name: str = "test",
    cwd: str = "/tmp",
    command: list[str] | None = None,
) -> WorkerProcess:
    """Create a WorkerProcess spawned via the holder, using direct send_cmd."""
    command = command or ["cat"]
    proc = WorkerProcess(name=name, cwd=cwd)

    async def send_cmd(msg: dict) -> dict:
        return await _send_cmd(socket_path, msg)

    proc._send_cmd = send_cmd

    resp = await send_cmd(
        {
            "cmd": "spawn",
            "name": name,
            "cwd": cwd,
            "command": command,
            "cols": proc.cols,
            "rows": proc.rows,
        }
    )
    assert resp["ok"], f"Spawn failed: {resp}"
    proc.pid = resp["pid"]
    proc.is_alive = True

    # Feed holder's buffer into the process buffer
    held = holder.workers.get(name)
    if held:
        proc.buffer = held.buffer

    return proc


class TestWorkerProcess:
    async def test_get_content_empty(self):
        proc = WorkerProcess(name="empty", cwd="/tmp")
        assert proc.get_content() == ""

    async def test_feed_output(self):
        proc = WorkerProcess(name="feed", cwd="/tmp")
        proc.feed_output(b"hello\nworld\n")
        content = proc.get_content(5)
        assert "hello" in content
        assert "world" in content

    async def test_record_input_bytes_updates_counters(self):
        """``record_input_bytes`` accumulates into the term-trace window
        counters so the 30 s rollup reports real input volume, not zero.

        Regression for the blind-input gap: before this method existed,
        a worker with typing but no echo (e.g. PTY stalled) produced a
        rollup that showed output=0 AND input=0, hiding which side
        broke. The counter pins the input number so the rollup is
        actually useful.
        """
        proc = WorkerProcess(name="input-count", cwd="/tmp")
        assert proc._trace_bytes_input == 0
        assert proc._trace_frames_input == 0
        proc.record_input_bytes(7)
        proc.record_input_bytes(3)
        assert proc._trace_bytes_input == 10
        assert proc._trace_frames_input == 2

    async def test_record_input_bytes_ignores_nonpositive(self):
        """Zero / negative sizes are defensive no-ops so an empty BINARY
        frame (or a bug in chunked-send math) doesn't wedge the counter."""
        proc = WorkerProcess(name="input-guard", cwd="/tmp")
        proc.record_input_bytes(0)
        proc.record_input_bytes(-5)
        assert proc._trace_bytes_input == 0
        assert proc._trace_frames_input == 0

    async def test_send_keys_with_holder(self, holder, socket_path):
        proc = await _make_process(holder, socket_path, name="keys-test")
        await asyncio.sleep(0.2)

        await proc.send_keys("hello", enter=True)
        await asyncio.sleep(0.3)

        content = proc.get_content(10)
        assert "hello" in content

    async def test_send_enter(self, holder, socket_path):
        proc = await _make_process(holder, socket_path, name="enter-test")
        await asyncio.sleep(0.2)
        # Should not raise
        await proc.send_enter()

    async def test_send_interrupt(self, holder, socket_path):
        proc = await _make_process(holder, socket_path, name="int-test", command=["sleep", "3600"])
        await asyncio.sleep(0.2)
        await proc.send_interrupt()
        await asyncio.sleep(0.5)

    async def test_send_escape(self, holder, socket_path):
        proc = await _make_process(holder, socket_path, name="esc-test")
        await asyncio.sleep(0.2)
        await proc.send_escape()

    async def test_send_sigwinch(self, holder, socket_path):
        proc = await _make_process(holder, socket_path, name="winch-test")
        await asyncio.sleep(0.2)
        await proc.send_sigwinch()

    async def test_kill(self, holder, socket_path):
        proc = await _make_process(holder, socket_path, name="kill-test", command=["sleep", "3600"])
        await asyncio.sleep(0.2)
        await proc.kill()
        assert not proc.is_alive

    async def test_resize(self, holder, socket_path):
        proc = await _make_process(holder, socket_path, name="resize-test")
        await asyncio.sleep(0.1)
        await proc.resize(120, 40)
        assert proc.cols == 120
        assert proc.rows == 40

    async def test_write_without_connection_raises(self):
        proc = WorkerProcess(name="disconnected", cwd="/tmp")
        with pytest.raises(ProcessError):
            await proc.send_keys("hello")

    async def test_properties(self):
        proc = WorkerProcess(name="props", cwd="/tmp")
        assert proc.is_alive is False
        assert proc.exit_code is None
        proc.is_alive = True
        assert proc.is_alive is True
        proc.exit_code = 0
        assert proc.exit_code == 0

    async def test_get_foreground_command(self, holder, socket_path):
        proc = await _make_process(holder, socket_path, name="fg-test", command=["sleep", "3600"])
        await asyncio.sleep(0.2)
        cmd = proc.get_foreground_command()
        assert isinstance(cmd, str)

    async def test_buffer_is_ring_buffer(self):
        proc = WorkerProcess(name="buf", cwd="/tmp")
        assert isinstance(proc.buffer, RingBuffer)

    async def test_subscribe_and_snapshot_no_gap(self, holder, socket_path):
        """subscribe_and_snapshot is atomic — no data lost between snapshot and live stream."""
        proc = await _make_process(holder, socket_path, name="snap-gap")
        await asyncio.sleep(0.2)

        # Write initial data so the snapshot has content
        await proc.send_keys("hello", enter=True)
        await asyncio.sleep(0.3)

        # Create a fake WS to subscribe
        from unittest.mock import MagicMock

        fake_ws = MagicMock()
        fake_ws.closed = False

        # Atomic subscribe + snapshot
        snapshot = proc.subscribe_and_snapshot(fake_ws)
        assert isinstance(snapshot, bytes)
        assert b"hello" in snapshot

        # The WS should now be in the subscriber set
        assert fake_ws in proc._ws_subscribers

    async def test_get_replay_snapshot_fetches_from_holder_when_local_empty(self):
        proc = WorkerProcess(name="snap-fetch", cwd="/tmp")

        async def _send_cmd(_msg: dict) -> dict:
            return {"ok": True, "data": "aGVsbG8K"}  # "hello\n"

        proc._send_cmd = _send_cmd

        snapshot = await proc.get_replay_snapshot()

        assert snapshot == b"hello\n"
        assert b"hello" in proc.buffer.snapshot()


class TestUserActiveGuard:
    """Tests for the terminal-active guard preventing automated input injection."""

    def test_is_user_active_default_false(self):
        proc = WorkerProcess(name="guard", cwd="/tmp")
        assert proc.is_user_active is False

    def test_is_user_active_true_after_mark(self):
        proc = WorkerProcess(name="guard", cwd="/tmp")
        proc.set_terminal_active(True)
        proc.mark_user_input()
        assert proc.is_user_active is True

    def test_is_user_active_expires_after_window(self):
        import time

        proc = WorkerProcess(name="guard", cwd="/tmp")
        proc.set_terminal_active(True)
        proc._last_user_input = time.time() - 3.0  # beyond 2s window
        assert proc.is_user_active is False

    def test_is_user_active_requires_terminal_active(self):
        proc = WorkerProcess(name="guard", cwd="/tmp")
        proc.mark_user_input()  # mark input but terminal not active
        assert proc.is_user_active is False


class TestSendKeysVariants:
    """send_keys with and without enter, text-only paths."""

    async def test_send_keys_no_enter(self, holder, socket_path):
        proc = await _make_process(holder, socket_path, name="noenter")
        await asyncio.sleep(0.2)
        await proc.send_keys("partial", enter=False)
        # Should not raise; text sent without CR

    async def test_send_keys_empty_text(self, holder, socket_path):
        proc = await _make_process(holder, socket_path, name="empty-key")
        await asyncio.sleep(0.2)
        await proc.send_keys("", enter=True)
        # Sends just CR — should not raise


class TestSignalErrorPaths:
    """Error paths for send_interrupt, send_escape, _signal."""

    async def test_send_interrupt_without_connection_raises(self):
        proc = WorkerProcess(name="no-conn", cwd="/tmp")
        with pytest.raises(ProcessError):
            await proc.send_interrupt()

    async def test_send_escape_without_connection_raises(self):
        proc = WorkerProcess(name="no-conn", cwd="/tmp")
        with pytest.raises(ProcessError):
            await proc.send_escape()

    async def test_send_sigwinch_without_connection_raises(self):
        proc = WorkerProcess(name="no-conn", cwd="/tmp")
        with pytest.raises(ProcessError):
            await proc.send_sigwinch()

    async def test_send_enter_without_connection_raises(self):
        proc = WorkerProcess(name="no-conn", cwd="/tmp")
        with pytest.raises(ProcessError):
            await proc.send_enter()

    async def test_signal_failure_response_raises(self):
        """A non-ok response from the holder raises ProcessError."""
        proc = WorkerProcess(name="sigfail", cwd="/tmp")

        async def fail_cmd(msg: dict) -> dict:
            return {"ok": False, "error": "no such process"}

        proc._send_cmd = fail_cmd
        with pytest.raises(ProcessError, match="no such process"):
            await proc.send_interrupt()

    async def test_write_failure_response_raises(self):
        """A non-ok response from the holder raises ProcessError."""
        proc = WorkerProcess(name="writefail", cwd="/tmp")

        async def fail_cmd(msg: dict) -> dict:
            return {"ok": False, "error": "dead process"}

        proc._send_cmd = fail_cmd
        with pytest.raises(ProcessError, match="dead process"):
            await proc.send_keys("hello")


class TestResizeEdgeCases:
    """Resize no-op and error paths."""

    async def test_resize_noop_same_dimensions(self, holder, socket_path):
        """Resize with same cols/rows is a no-op — no command sent."""
        proc = await _make_process(holder, socket_path, name="resize-noop")
        await asyncio.sleep(0.1)
        original_cols, original_rows = proc.cols, proc.rows
        await proc.resize(original_cols, original_rows)
        assert proc.cols == original_cols
        assert proc.rows == original_rows

    async def test_resize_without_connection_raises(self):
        proc = WorkerProcess(name="no-conn", cwd="/tmp")
        # Force different dims so it doesn't hit the no-op path
        with pytest.raises(ProcessError):
            await proc.resize(999, 999)


class TestWSSubscription:
    """WebSocket subscription lifecycle."""

    def test_subscribe_ws_adds_to_set(self):
        from unittest.mock import MagicMock

        proc = WorkerProcess(name="ws-sub", cwd="/tmp")
        ws = MagicMock()
        proc.subscribe_ws(ws)
        assert ws in proc._ws_subscribers
        assert proc.has_ws_subscribers is True

    def test_unsubscribe_ws_removes(self):
        from unittest.mock import MagicMock

        proc = WorkerProcess(name="ws-unsub", cwd="/tmp")
        ws = MagicMock()
        proc.subscribe_ws(ws)
        proc.unsubscribe_ws(ws)
        assert ws not in proc._ws_subscribers
        assert proc.has_ws_subscribers is False

    def test_cleanup_ws_clears_all(self):
        from unittest.mock import MagicMock

        proc = WorkerProcess(name="ws-clean", cwd="/tmp")
        ws1 = MagicMock()
        ws2 = MagicMock()
        proc.subscribe_ws(ws1)
        proc.subscribe_ws(ws2)
        assert len(proc._ws_subscribers) == 2
        proc.cleanup_ws()
        assert len(proc._ws_subscribers) == 0
        assert proc.has_ws_subscribers is False

    def test_has_ws_subscribers_false_initially(self):
        proc = WorkerProcess(name="ws-init", cwd="/tmp")
        assert proc.has_ws_subscribers is False


class TestGetForegroundCommandEdgeCases:
    """Edge cases for get_foreground_command and get_child_foreground_command."""

    def test_get_foreground_command_no_pid(self):
        proc = WorkerProcess(name="nopid", cwd="/tmp")
        proc.pid = None
        assert proc.get_foreground_command() == ""

    def test_get_child_foreground_command_no_pid(self):
        proc = WorkerProcess(name="nopid", cwd="/tmp")
        proc.pid = None
        assert proc.get_child_foreground_command() == ""

    def test_get_foreground_command_invalid_pid(self):
        proc = WorkerProcess(name="badpid", cwd="/tmp")
        proc.pid = 999999999  # non-existent PID
        assert proc.get_foreground_command() == ""

    def test_get_child_foreground_command_invalid_pid(self):
        proc = WorkerProcess(name="badpid", cwd="/tmp")
        proc.pid = 999999999
        result = proc.get_child_foreground_command()
        # Falls back to own foreground command, also empty for invalid PID
        assert result == ""
