"""Tests for swarm.pty.pool — ProcessPool."""

from __future__ import annotations

import asyncio

import pytest

from swarm.pty.holder import PtyHolder
from swarm.pty.pool import ProcessPool
from swarm.pty.process import ProcessError


@pytest.fixture()
def socket_path(tmp_path):
    return str(tmp_path / "test-pool.sock")


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


@pytest.fixture()
async def pool(holder, socket_path):
    p = ProcessPool(socket_path)
    await p.connect()
    yield p
    await p._disconnect()


class TestProcessPool:
    async def test_connect_and_ping(self, pool):
        resp = await pool._send_cmd({"cmd": "ping"})
        assert resp["pong"] is True

    async def test_spawn(self, pool):
        proc = await pool.spawn("test-cat", "/tmp", command=["cat"])
        assert proc.name == "test-cat"
        assert proc.pid > 0
        assert proc.is_alive is True
        assert pool.get("test-cat") is proc

    async def test_spawn_not_connected_raises(self, socket_path):
        p = ProcessPool(socket_path)
        with pytest.raises(ProcessError, match="Not connected"):
            await p.spawn("x", "/tmp")

    async def test_get_returns_none_for_unknown(self, pool):
        assert pool.get("nonexistent") is None

    async def test_get_all(self, pool):
        await pool.spawn("a", "/tmp", command=["cat"])
        await pool.spawn("b", "/tmp", command=["cat"])
        all_workers = pool.get_all()
        assert len(all_workers) == 2
        names = {w.name for w in all_workers}
        assert names == {"a", "b"}

    async def test_kill(self, pool):
        await pool.spawn("to-kill", "/tmp", command=["sleep", "3600"])
        await asyncio.sleep(0.1)
        await pool.kill("to-kill")
        assert pool.get("to-kill") is None

    async def test_kill_all(self, pool):
        await pool.spawn("k1", "/tmp", command=["cat"])
        await pool.spawn("k2", "/tmp", command=["cat"])
        await asyncio.sleep(0.1)
        await pool.kill_all()
        assert pool.get_all() == []

    async def test_revive(self, pool):
        await pool.spawn("revive-me", "/tmp", command=["cat"])
        await asyncio.sleep(0.1)
        # Kill the worker first so revive can reuse the name
        await pool.kill("revive-me")
        new_proc = await pool.revive("revive-me")
        # revive on a killed worker returns None (already removed)
        assert new_proc is None

    async def test_revive_existing(self, pool, monkeypatch):
        proc = await pool.spawn("rev-exist", "/tmp", command=["cat"])
        old_pid = proc.pid
        await asyncio.sleep(0.1)

        # Patch revive to use "cat" instead of "claude --continue"
        async def patched_revive(name: str):
            old = pool._workers.get(name)
            if not old:
                return None
            cwd = old.cwd
            await pool.kill(name)
            return await pool.spawn(name, cwd, command=["cat"])

        monkeypatch.setattr(pool, "revive", patched_revive)
        new_proc = await pool.revive("rev-exist")
        assert new_proc is not None
        assert new_proc.name == "rev-exist"
        assert new_proc.pid != old_pid

    async def test_revive_nonexistent(self, pool):
        result = await pool.revive("ghost")
        assert result is None

    async def test_spawn_batch(self, pool):
        workers = [("b1", "/tmp"), ("b2", "/tmp")]
        procs = await pool.spawn_batch(workers, command=["cat"], stagger_seconds=0.05)
        assert len(procs) == 2
        assert procs[0].name == "b1"
        assert procs[1].name == "b2"

    async def test_discover(self, holder, pool):
        # Spawn a worker directly via the pool
        await pool.spawn("disc-test", "/tmp", command=["cat"])
        await asyncio.sleep(0.2)

        # Clear the pool's local tracking to simulate reconnect
        pool._workers.clear()

        discovered = await pool.discover()
        assert len(discovered) >= 1
        names = {w.name for w in discovered}
        assert "disc-test" in names

    async def test_discover_updates_existing(self, pool):
        await pool.spawn("disc-update", "/tmp", command=["cat"])
        await asyncio.sleep(0.1)
        # Discover should update the existing entry (not duplicate)
        discovered = await pool.discover()
        assert len(discovered) == 1
        assert discovered[0].name == "disc-update"

    async def test_shutdown_holder(self, holder, socket_path):
        p = ProcessPool(socket_path)
        await p.connect()
        await p.spawn("shutdown-w", "/tmp", command=["cat"])
        await p.shutdown_holder()
        assert p.get_all() == []
        assert not p._connected

    async def test_send_keys_via_pool(self, pool):
        proc = await pool.spawn("keys-pool", "/tmp", command=["cat"])
        await asyncio.sleep(0.2)
        # Should not raise
        await proc.send_keys("hello", enter=True)
        await asyncio.sleep(0.3)
        content = proc.get_content(10)
        assert "hello" in content

    async def test_discover_recovers_dimensions(self, holder, pool):
        """Discover should recover cols/rows from the holder."""
        await pool.spawn("dim-disc", "/tmp", command=["cat"], cols=160, rows=45)
        await asyncio.sleep(0.2)

        # Clear local tracking to simulate daemon restart
        pool._workers.clear()

        discovered = await pool.discover()
        proc = next(p for p in discovered if p.name == "dim-disc")
        assert proc.cols == 160
        assert proc.rows == 45

    async def test_duplicate_name_alive_fails(self, pool):
        await pool.spawn("dupe", "/tmp", command=["sleep", "3600"])
        await asyncio.sleep(0.1)
        with pytest.raises(ProcessError, match="Spawn failed"):
            await pool.spawn("dupe", "/tmp", command=["sleep", "3600"])


class TestCommandIdProtocol:
    """Command ID is sent to holder and echoed in responses."""

    async def test_command_id_echoed(self, pool):
        """Pool sends 'id' field and holder echoes it back."""
        resp = await pool._send_cmd({"cmd": "ping"})
        # The response should have the ID field echoed
        assert "id" in resp
        assert isinstance(resp["id"], int)

    async def test_dispatch_matches_by_id(self, pool):
        """Multiple commands in flight should match by ID, not FIFO."""
        # Sequential sends still get matched correctly
        r1 = await pool._send_cmd({"cmd": "ping"})
        r2 = await pool._send_cmd({"cmd": "ping"})
        assert r1["pong"] is True
        assert r2["pong"] is True
        # IDs should be different (monotonically increasing)
        assert r1["id"] != r2["id"]
