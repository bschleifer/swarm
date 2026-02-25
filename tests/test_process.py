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
