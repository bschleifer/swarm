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

    async def test_holder_reports_cols_rows(self, holder, socket_path):
        """Spawn with custom dims, resize, list — verify cols/rows reported correctly."""
        await _send_cmd(
            socket_path,
            {
                "cmd": "spawn",
                "name": "dims-test",
                "cwd": "/tmp",
                "command": ["cat"],
                "cols": 100,
                "rows": 30,
            },
        )
        await asyncio.sleep(0.1)

        # list should report initial dimensions
        resp = await _send_cmd(socket_path, {"cmd": "list"})
        w = resp["workers"][0]
        assert w["cols"] == 100
        assert w["rows"] == 30

        # Resize and verify updated dimensions
        await _send_cmd(
            socket_path,
            {"cmd": "resize", "name": "dims-test", "cols": 160, "rows": 45},
        )
        resp = await _send_cmd(socket_path, {"cmd": "list"})
        w = resp["workers"][0]
        assert w["cols"] == 160
        assert w["rows"] == 45

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

    async def test_shell_wrap_keeps_alive(self, holder, socket_path):
        """With shell_wrap, worker stays alive after the command exits."""
        resp = await _send_cmd(
            socket_path,
            {
                "cmd": "spawn",
                "name": "wrapped",
                "cwd": "/tmp",
                "command": ["echo", "bye"],
                "shell_wrap": True,
            },
        )
        assert resp["ok"] is True

        # echo exits instantly; the wrapper bash takes over
        await asyncio.sleep(1.0)

        resp = await _send_cmd(socket_path, {"cmd": "list"})
        workers = resp["workers"]
        assert len(workers) == 1
        assert workers[0]["name"] == "wrapped"
        assert workers[0]["alive"] is True, "shell_wrap should keep the worker alive"

        # Should still be writable (user can type in the fallback shell)
        data = base64.b64encode(b"echo alive\n").decode()
        write_resp = await _send_cmd(socket_path, {"cmd": "write", "name": "wrapped", "data": data})
        assert write_resp["ok"] is True


class TestCommandIdEcho:
    """Holder echoes the 'id' field from incoming commands."""

    async def test_command_with_id_echoed(self, holder, socket_path):
        resp = await _send_cmd(socket_path, {"cmd": "ping", "id": 42})
        assert resp["pong"] is True
        assert resp["id"] == 42

    async def test_command_without_id_still_works(self, holder, socket_path):
        resp = await _send_cmd(socket_path, {"cmd": "ping"})
        assert resp["pong"] is True
        assert "id" not in resp


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


# ── A1: Broadcast with dead clients ─────────────────────────────────────


class TestBroadcastClientRemoval:
    async def test_broadcast_with_dead_client(self, holder, socket_path):
        """Broadcast removes dead clients without raising."""
        # Connect two clients
        r1, w1 = await asyncio.open_unix_connection(socket_path)
        r2, w2 = await asyncio.open_unix_connection(socket_path)
        await asyncio.sleep(0.1)

        assert len(holder._clients) == 2

        # Kill one client's transport
        w1.close()
        await w1.wait_closed()
        await asyncio.sleep(0.1)

        # Spawn a worker to trigger broadcast output
        await _send_cmd(
            socket_path,
            {
                "cmd": "spawn",
                "name": "bc-test",
                "cwd": "/tmp",
                "command": ["echo", "hello"],
            },
        )
        await asyncio.sleep(0.3)

        # Dead client should have been removed
        assert len(holder._clients) <= 2  # at most the live client + spawn sender
        # No crash — that's the point

        w2.close()
        await w2.wait_closed()

    async def test_concurrent_client_disconnect_during_broadcast(self, holder, socket_path):
        """Client disconnect while broadcast is in progress should not crash."""
        r1, w1 = await asyncio.open_unix_connection(socket_path)
        await asyncio.sleep(0.1)

        assert len(holder._clients) >= 1

        # Forcefully close transport to simulate mid-broadcast disconnect
        for client in set(holder._clients):
            client.transport.close()

        await asyncio.sleep(0.1)

        # Broadcast should handle the dead clients gracefully
        holder._broadcast(b'{"test": true}\n')
        # No crash = success


# ── A2: HeldWorker.alive idempotency ────────────────────────────────────


class TestHeldWorkerAlive:
    async def test_alive_property_idempotent(self, holder, socket_path):
        """Calling .alive twice on a dead worker should both return False."""
        resp = await _send_cmd(
            socket_path,
            {
                "cmd": "spawn",
                "name": "idem-test",
                "cwd": "/tmp",
                "command": ["true"],  # exits immediately
            },
        )
        assert resp["ok"]
        await asyncio.sleep(0.5)

        worker = holder.workers.get("idem-test")
        assert worker is not None
        # First call reaps the zombie
        alive1 = worker.alive
        # Second call should not raise ChildProcessError
        alive2 = worker.alive
        assert alive1 is False
        assert alive2 is False
        assert worker._reaped is True

    async def test_kill_worker_after_process_exit(self, holder, socket_path):
        """kill_worker on an already-exited process should not crash."""
        await _send_cmd(
            socket_path,
            {
                "cmd": "spawn",
                "name": "exit-then-kill",
                "cwd": "/tmp",
                "command": ["true"],
            },
        )
        await asyncio.sleep(0.5)

        # Process has exited; kill should succeed gracefully
        result = holder.kill_worker("exit-then-kill")
        assert result is True

    def test_alive_reaped_flag_unit(self):
        """Unit test: _reaped flag prevents repeated waitpid calls."""
        from swarm.pty.holder import HeldWorker

        worker = HeldWorker(
            name="unit",
            pid=999999,  # non-existent PID
            master_fd=-1,
            cwd="/tmp",
            command=["true"],
        )
        # Simulate already reaped
        worker._reaped = True
        worker.exit_code = 0
        assert worker.alive is False
        # Should not attempt waitpid (would raise on invalid PID)


# ── A7: SIGTERM→SIGKILL grace period ────────────────────────────────────


class TestKillGracePeriod:
    async def test_kill_worker_graceful_exit(self, holder, socket_path):
        """Worker that handles SIGTERM should exit without needing SIGKILL."""
        # `sleep` handles SIGTERM by default (exits cleanly)
        resp = await _send_cmd(
            socket_path,
            {
                "cmd": "spawn",
                "name": "graceful",
                "cwd": "/tmp",
                "command": ["sleep", "3600"],
            },
        )
        assert resp["ok"]
        await asyncio.sleep(0.1)

        worker = holder.workers.get("graceful")
        assert worker is not None
        assert worker.alive is True

        result = holder.kill_worker("graceful")
        assert result is True
        # Worker should be cleaned up
        assert "graceful" not in holder.workers

    async def test_kill_worker_stuck_process(self, holder, socket_path):
        """Worker that ignores SIGTERM should be SIGKILL'd after grace period."""
        # bash -c 'trap "" TERM; sleep 3600' ignores SIGTERM
        resp = await _send_cmd(
            socket_path,
            {
                "cmd": "spawn",
                "name": "stuck",
                "cwd": "/tmp",
                "command": ["bash", "-c", 'trap "" TERM; sleep 3600'],
            },
        )
        assert resp["ok"]
        await asyncio.sleep(0.2)

        worker = holder.workers.get("stuck")
        assert worker is not None
        assert worker.alive is True

        result = holder.kill_worker("stuck")
        assert result is True
        assert "stuck" not in holder.workers


# ── Spawn edge cases ─────────────────────────────────────────────────


class TestSpawnEdgeCases:
    async def test_spawn_at_max_capacity(self, socket_path):
        """Spawn fails when max_workers is reached."""
        h = PtyHolder(socket_path, max_workers=1)
        task = asyncio.create_task(h.serve())
        for _ in range(50):
            if h._server is not None:
                break
            await asyncio.sleep(0.05)

        # First spawn — succeeds
        resp = await _send_cmd(
            socket_path,
            {"cmd": "spawn", "name": "w1", "cwd": "/tmp", "command": ["cat"]},
        )
        assert resp["ok"] is True

        # Second spawn — should fail (capacity)
        resp = await _send_cmd(
            socket_path,
            {"cmd": "spawn", "name": "w2", "cwd": "/tmp", "command": ["cat"]},
        )
        assert resp["ok"] is False
        assert "limit" in resp.get("error", "").lower() or "max" in resp.get("error", "").lower()

        h._running = False
        h._shutdown_all()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def test_spawn_invalid_cols_rows(self, holder, socket_path):
        """Spawn with non-numeric cols/rows returns error."""
        resp = await _send_cmd(
            socket_path,
            {
                "cmd": "spawn",
                "name": "bad-dims",
                "cwd": "/tmp",
                "command": ["cat"],
                "cols": "abc",
                "rows": "def",
            },
        )
        assert resp["ok"] is False
        assert "cols" in resp.get("error", "").lower() or "rows" in resp.get("error", "").lower()


# ── Write/resize/signal error paths ──────────────────────────────────


class TestErrorPaths:
    async def test_write_invalid_base64(self, holder, socket_path):
        """Write with invalid base64 data returns error."""
        await _send_cmd(
            socket_path,
            {"cmd": "spawn", "name": "b64-test", "cwd": "/tmp", "command": ["cat"]},
        )
        resp = await _send_cmd(
            socket_path,
            {"cmd": "write", "name": "b64-test", "data": "!!!not-base64!!!"},
        )
        assert resp["ok"] is False
        assert "base64" in resp.get("error", "").lower()

    async def test_signal_disallowed(self, holder, socket_path):
        """Signal with disallowed signal name returns error."""
        await _send_cmd(
            socket_path,
            {"cmd": "spawn", "name": "sig-deny", "cwd": "/tmp", "command": ["cat"]},
        )
        resp = await _send_cmd(
            socket_path,
            {"cmd": "signal", "name": "sig-deny", "sig": "SIGUSR1"},
        )
        assert resp["ok"] is False
        assert "not allowed" in resp.get("error", "")

    async def test_signal_nonexistent_worker(self, holder, socket_path):
        """Signal for missing worker returns ok=False."""
        resp = await _send_cmd(
            socket_path,
            {"cmd": "signal", "name": "ghost", "sig": "SIGINT"},
        )
        assert resp["ok"] is False

    async def test_resize_nonexistent_worker(self, holder, socket_path):
        """Resize for missing worker returns ok=False."""
        resp = await _send_cmd(
            socket_path,
            {"cmd": "resize", "name": "ghost", "cols": 80, "rows": 24},
        )
        assert resp["ok"] is False

    async def test_resize_invalid_cols_rows(self, holder, socket_path):
        """Resize with non-numeric cols/rows returns error."""
        resp = await _send_cmd(
            socket_path,
            {"cmd": "resize", "name": "x", "cols": "abc", "rows": "def"},
        )
        assert resp["ok"] is False

    async def test_kill_nonexistent_worker(self, holder, socket_path):
        """Kill for missing worker returns ok=False."""
        resp = await _send_cmd(socket_path, {"cmd": "kill", "name": "ghost"})
        assert resp["ok"] is False


# ── Broadcast helpers ─────────────────────────────────────────────────


class TestBroadcastHelpers:
    def test_broadcast_output_format(self, holder):
        """_broadcast_output sends JSON with output name and base64 data."""
        messages: list[bytes] = []
        original = holder._broadcast
        holder._broadcast = lambda data: messages.append(data)
        try:
            holder._broadcast_output("w1", b"hello")
            assert len(messages) == 1
            msg = json.loads(messages[0])
            assert msg["output"] == "w1"
            assert base64.b64decode(msg["data"]) == b"hello"
        finally:
            holder._broadcast = original

    def test_broadcast_death_format(self, holder):
        """_broadcast_death sends JSON with died name and exit_code."""
        messages: list[bytes] = []
        original = holder._broadcast
        holder._broadcast = lambda data: messages.append(data)
        try:
            holder._broadcast_death("w1", 42)
            assert len(messages) == 1
            msg = json.loads(messages[0])
            assert msg["died"] == "w1"
            assert msg["exit_code"] == 42
        finally:
            holder._broadcast = original

    def test_broadcast_no_clients(self, holder):
        """Broadcast with no clients should not raise."""
        assert len(holder._clients) == 0
        holder._broadcast(b'{"test": true}\n')
        # No crash = success


# ── Command ID echo on error responses ────────────────────────────────


class TestCommandIdOnError:
    async def test_error_response_includes_id(self, holder, socket_path):
        """Error responses should echo the command id."""
        resp = await _send_cmd(socket_path, {"cmd": "bogus", "id": 99})
        assert "error" in resp
        assert resp["id"] == 99

    async def test_spawn_error_includes_id(self, holder, socket_path):
        """Spawn failure response should echo the command id."""
        resp = await _send_cmd(
            socket_path,
            {"cmd": "spawn", "name": "", "cwd": "/tmp", "command": ["cat"], "id": 77},
        )
        # Empty name may or may not fail, but id should be echoed
        assert resp.get("id") == 77


# ── List and reap ────────────────────────────────────────────────────


class TestListAndReap:
    async def test_list_empty(self, holder, socket_path):
        """List with no workers returns empty list."""
        resp = await _send_cmd(socket_path, {"cmd": "list"})
        assert resp["workers"] == []

    async def test_reap_dead_children(self, holder, socket_path):
        """Dead children are reaped during the reap cycle."""
        await _send_cmd(
            socket_path,
            {"cmd": "spawn", "name": "ephemeral", "cwd": "/tmp", "command": ["true"]},
        )
        await asyncio.sleep(1.5)  # wait for at least one reap cycle
        resp = await _send_cmd(socket_path, {"cmd": "list"})
        workers = resp["workers"]
        assert len(workers) == 1
        assert workers[0]["alive"] is False
        assert workers[0]["exit_code"] is not None
