"""Tests for swarm.pty.holder — PtyHolder."""

from __future__ import annotations

import asyncio
import base64
import json

import pytest

from swarm.pty.holder import PtyHolder


@pytest.fixture()
def socket_path(tmp_path):
    """Return a temp socket path."""
    return str(tmp_path / "test-holder.sock")


@pytest.fixture()
async def holder(socket_path):
    """Start a PtyHolder and yield it, then stop."""
    h = PtyHolder(socket_path)
    task = asyncio.create_task(h.serve())
    # Wait for server to be ready
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
    """Send a command to the holder and return the response."""
    reader, writer = await asyncio.open_unix_connection(socket_path)
    writer.write(json.dumps(cmd).encode() + b"\n")
    await writer.drain()
    line = await reader.readline()
    writer.close()
    await writer.wait_closed()
    return json.loads(line)


class TestPtyHolder:
    async def test_ping(self, holder, socket_path):
        resp = await _send_cmd(socket_path, {"cmd": "ping"})
        assert resp["pong"] is True

    async def test_spawn_and_list(self, holder, socket_path):
        resp = await _send_cmd(
            socket_path,
            {
                "cmd": "spawn",
                "name": "test-echo",
                "cwd": "/tmp",
                "command": ["cat"],
                "cols": 80,
                "rows": 24,
            },
        )
        assert resp["ok"] is True
        assert resp["name"] == "test-echo"
        assert resp["pid"] > 0

        # List should include the worker
        resp = await _send_cmd(socket_path, {"cmd": "list"})
        workers = resp["workers"]
        assert len(workers) == 1
        assert workers[0]["name"] == "test-echo"
        assert workers[0]["alive"] is True

    async def test_write_and_snapshot(self, holder, socket_path):
        # Spawn cat (echoes stdin to stdout)
        await _send_cmd(
            socket_path,
            {
                "cmd": "spawn",
                "name": "writer",
                "cwd": "/tmp",
                "command": ["cat"],
            },
        )
        # Small delay for process startup
        await asyncio.sleep(0.2)

        # Write data
        data = base64.b64encode(b"hello\n").decode()
        resp = await _send_cmd(
            socket_path,
            {
                "cmd": "write",
                "name": "writer",
                "data": data,
            },
        )
        assert resp["ok"] is True

        # Wait for cat to echo back
        await asyncio.sleep(0.3)

        # Snapshot should contain the echoed data
        resp = await _send_cmd(socket_path, {"cmd": "snapshot", "name": "writer"})
        assert resp["ok"] is True
        buf = base64.b64decode(resp["data"])
        assert b"hello" in buf

    async def test_kill(self, holder, socket_path):
        await _send_cmd(
            socket_path,
            {
                "cmd": "spawn",
                "name": "to-kill",
                "cwd": "/tmp",
                "command": ["sleep", "3600"],
            },
        )
        await asyncio.sleep(0.1)

        resp = await _send_cmd(socket_path, {"cmd": "kill", "name": "to-kill"})
        assert resp["ok"] is True

        # Should be gone from list
        resp = await _send_cmd(socket_path, {"cmd": "list"})
        assert len(resp["workers"]) == 0

    async def test_signal(self, holder, socket_path):
        await _send_cmd(
            socket_path,
            {
                "cmd": "spawn",
                "name": "sig-test",
                "cwd": "/tmp",
                "command": ["sleep", "3600"],
            },
        )
        await asyncio.sleep(0.1)

        resp = await _send_cmd(
            socket_path,
            {
                "cmd": "signal",
                "name": "sig-test",
                "sig": "SIGTERM",
            },
        )
        assert resp["ok"] is True

        # Process should die shortly
        await asyncio.sleep(0.5)
        resp = await _send_cmd(socket_path, {"cmd": "list"})
        workers = resp["workers"]
        if workers:
            assert workers[0]["alive"] is False

    async def test_resize(self, holder, socket_path):
        await _send_cmd(
            socket_path,
            {
                "cmd": "spawn",
                "name": "resize-test",
                "cwd": "/tmp",
                "command": ["cat"],
            },
        )
        await asyncio.sleep(0.1)

        resp = await _send_cmd(
            socket_path,
            {
                "cmd": "resize",
                "name": "resize-test",
                "cols": 120,
                "rows": 40,
            },
        )
        assert resp["ok"] is True

    async def test_duplicate_name_dead(self, holder, socket_path):
        """Re-using a name after the worker dies should succeed."""
        await _send_cmd(
            socket_path,
            {
                "cmd": "spawn",
                "name": "reuse",
                "cwd": "/tmp",
                "command": ["true"],  # exits immediately
            },
        )
        await asyncio.sleep(0.3)

        # Should be able to reuse the name
        resp = await _send_cmd(
            socket_path,
            {
                "cmd": "spawn",
                "name": "reuse",
                "cwd": "/tmp",
                "command": ["cat"],
            },
        )
        assert resp["ok"] is True

    async def test_duplicate_name_alive_fails(self, holder, socket_path):
        await _send_cmd(
            socket_path,
            {
                "cmd": "spawn",
                "name": "dupe",
                "cwd": "/tmp",
                "command": ["sleep", "3600"],
            },
        )
        await asyncio.sleep(0.1)

        resp = await _send_cmd(
            socket_path,
            {
                "cmd": "spawn",
                "name": "dupe",
                "cwd": "/tmp",
                "command": ["sleep", "3600"],
            },
        )
        assert resp["ok"] is False
        assert "already exists" in resp.get("error", "")

    async def test_unknown_command(self, holder, socket_path):
        resp = await _send_cmd(socket_path, {"cmd": "bogus"})
        assert "error" in resp

    async def test_write_nonexistent(self, holder, socket_path):
        data = base64.b64encode(b"hello").decode()
        resp = await _send_cmd(
            socket_path,
            {
                "cmd": "write",
                "name": "ghost",
                "data": data,
            },
        )
        assert resp["ok"] is False

    async def test_snapshot_nonexistent(self, holder, socket_path):
        resp = await _send_cmd(socket_path, {"cmd": "snapshot", "name": "ghost"})
        assert resp["ok"] is False

    async def test_shutdown(self, holder, socket_path):
        await _send_cmd(
            socket_path,
            {
                "cmd": "spawn",
                "name": "shutdown-test",
                "cwd": "/tmp",
                "command": ["sleep", "3600"],
            },
        )
        resp = await _send_cmd(socket_path, {"cmd": "shutdown"})
        assert resp["ok"] is True
        assert len(holder.workers) == 0


class TestHeldWorker:
    async def test_exit_detection(self, holder, socket_path):
        """Worker that exits naturally should be detected as dead."""
        await _send_cmd(
            socket_path,
            {
                "cmd": "spawn",
                "name": "short-lived",
                "cwd": "/tmp",
                "command": ["echo", "bye"],
            },
        )
        # Wait for process to exit
        await asyncio.sleep(0.5)

        resp = await _send_cmd(socket_path, {"cmd": "list"})
        workers = resp["workers"]
        assert len(workers) == 1
        assert workers[0]["alive"] is False
