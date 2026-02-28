"""Tests for pool.py concurrency fixes — A5 (pending futures) and A6 (PID detection)."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from swarm.pty.pool import ProcessPool
from swarm.pty.process import ProcessError, WorkerProcess

# ── A5: Pending futures safety ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_disconnect_during_pending_cmd():
    """Disconnect while a command is pending should fail the future with ProcessError."""
    pool = ProcessPool("/tmp/fake.sock")
    pool._connected = True
    pool._cmd_lock = asyncio.Lock()

    # Create a fake writer that accepts writes but never responds
    mock_writer = MagicMock()
    mock_writer.write = MagicMock()
    mock_writer.drain = AsyncMock()
    mock_writer.close = MagicMock()
    mock_writer.wait_closed = AsyncMock()
    pool._writer = mock_writer
    pool._reader = MagicMock()

    # Register a pending future manually (simulating mid-flight command)
    loop = asyncio.get_running_loop()
    fut: asyncio.Future[dict] = loop.create_future()
    pool._pending[99] = fut

    # Disconnect should fail the pending future
    pool._read_task = None
    await pool._disconnect()

    assert fut.done()
    with pytest.raises(ProcessError, match="Disconnected"):
        fut.result()
    assert len(pool._pending) == 0


@pytest.mark.asyncio
async def test_pending_cleanup_after_timeout():
    """Timed-out command's future should be removed from _pending."""
    pool = ProcessPool("/tmp/fake.sock")
    pool._connected = True
    pool._cmd_lock = asyncio.Lock()

    # Fake writer that drains but never gets a response
    mock_writer = MagicMock()
    mock_writer.write = MagicMock()
    mock_writer.drain = AsyncMock()
    pool._writer = mock_writer

    async def fast_timeout_send(msg: dict) -> dict:
        """Send with very short timeout to trigger cleanup."""
        if not pool._writer or not pool._connected:
            raise ProcessError("Not connected to holder")
        async with pool._cmd_lock:
            pool._cmd_counter += 1
            cmd_id = pool._cmd_counter
            fut: asyncio.Future[dict] = asyncio.get_running_loop().create_future()
            pool._pending[cmd_id] = fut
            pool._writer.write(b'{"cmd":"ping","id":1}\n')
        await pool._writer.drain()
        try:
            return await asyncio.wait_for(fut, timeout=0.01)
        except TimeoutError:
            raise ProcessError("Command timed out: ping")
        finally:
            pool._pending.pop(cmd_id, None)

    with pytest.raises(ProcessError, match="timed out"):
        await fast_timeout_send({"cmd": "ping"})

    # Future should have been cleaned up
    assert len(pool._pending) == 0


# ── A6: PID change detection in discover ─────────────────────────────────


@pytest.mark.asyncio
async def test_discover_pid_change_replaces_process():
    """If holder reports a different PID for an existing worker, replace the process."""
    pool = ProcessPool("/tmp/fake.sock")
    pool._connected = True
    pool._cmd_lock = asyncio.Lock()

    # Pre-populate with an existing worker at PID 100
    old_proc = WorkerProcess(name="alice", cwd="/tmp")
    old_proc.pid = 100
    old_proc.is_alive = True
    old_proc._send_cmd = AsyncMock()
    pool._workers["alice"] = old_proc

    # Mock _send_cmd to return list with new PID, then snapshot
    async def mock_send(msg: dict) -> dict:
        if msg.get("cmd") == "list":
            return {
                "workers": [
                    {
                        "name": "alice",
                        "pid": 200,  # Different PID!
                        "alive": True,
                        "cwd": "/tmp",
                        "cols": 200,
                        "rows": 50,
                    }
                ]
            }
        if msg.get("cmd") == "snapshot":
            return {"ok": True, "data": ""}
        return {}

    pool._send_cmd = mock_send

    result = await pool.discover()
    assert len(result) == 1
    new_proc = pool._workers["alice"]
    assert new_proc.pid == 200
    assert new_proc is not old_proc


@pytest.mark.asyncio
async def test_discover_same_pid_updates_in_place():
    """Same PID should update liveness without replacing the process object."""
    pool = ProcessPool("/tmp/fake.sock")
    pool._connected = True
    pool._cmd_lock = asyncio.Lock()

    old_proc = WorkerProcess(name="bob", cwd="/tmp")
    old_proc.pid = 300
    old_proc.is_alive = True
    old_proc._send_cmd = AsyncMock()
    pool._workers["bob"] = old_proc

    async def mock_send(msg: dict) -> dict:
        if msg.get("cmd") == "list":
            return {
                "workers": [
                    {
                        "name": "bob",
                        "pid": 300,  # Same PID
                        "alive": False,
                        "exit_code": 1,
                        "cwd": "/tmp",
                    }
                ]
            }
        return {}

    pool._send_cmd = mock_send

    result = await pool.discover()
    assert len(result) == 1
    # Should be the SAME object, updated in place
    assert pool._workers["bob"] is old_proc
    assert old_proc.is_alive is False
    assert old_proc.exit_code == 1
