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

    async def test_output_for_unknown_worker_buffered_not_dropped(self, pool):
        """Holder output arriving before a worker is in ``_workers`` must be buffered.

        Regression for the reload race where the daemon's ``_read_loop``
        starts draining the holder socket as soon as ``connect()`` finishes,
        but ``discover()`` hasn't yet populated ``_workers``.  Previously the
        bytes were silently dropped, producing a terminal that showed a stale
        snapshot until the operator hit Reload a second time.
        """
        import base64 as _b64

        pool._workers.clear()
        pool._dispatch_message(
            {"output": "ghost-worker", "data": _b64.b64encode(b"ignored bytes").decode()}
        )
        # Buffer holds exactly the bytes that arrived.
        assert pool._pending_output == {"ghost-worker": [b"ignored bytes"]}

    async def test_discover_drains_pre_snapshot_pending_output(self, pool):
        """``discover`` discards pre-snapshot pending output and logs the drop.

        The snapshot the holder hands back contains everything it had in its
        ring buffer at the moment it processed the snapshot command, and the
        read loop dispatches messages in arrival order — so any output
        chunks already in ``_pending_output`` at the time ``_send_cmd`` for
        the snapshot resolves MUST be pre-snapshot (already captured).
        Replaying them would duplicate bytes in the local ring buffer.
        """
        # Spawn a real worker and let it settle so the holder has it listed.
        await pool.spawn("disc-drain", "/tmp", command=["cat"])
        await asyncio.sleep(0.2)

        # Simulate pre-discovery state: pool has no workers, and a stray
        # output chunk for the (not yet discovered) worker is buffered
        # exactly as the read loop would have left it.
        pool._workers.clear()
        pool._pending_output["disc-drain"] = [b"pre-snapshot chunk"]

        await pool.discover()

        # Chunk was discarded — it would have been a duplicate of snapshot data.
        assert "disc-drain" not in pool._pending_output
        assert "disc-drain" in pool._workers


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


# ── Holder version-skew detection ───────────────────────────────────
#
# The holder is a double-forked persistent sidecar. Once spawned, its
# bytecode never refreshes — not on daemon reload, not on os.execv, not
# even if the operator reinstalls the swarm tool. The only way to adopt
# a holder.py change is to explicitly kill + respawn the holder process.
#
# This happened in production: commit 0df45be raised
# ``_MAX_WRITE_BUFFER`` 1 MB → 8 MB on 2026-04-21 to fix the reload
# lockup, but the fix sat unapplied for days because the holder had been
# running since April 5. Every dashboard Reload refreshed the daemon and
# immediately got dropped again as a "slow client" by the stale holder.
#
# Drift detection makes that invisible failure loud: on every successful
# connect, the pool asks the holder for its import-time source hash and
# compares against ``holder.py`` on disk. A mismatch is logged at
# WARNING level and surfaced via ``pool.holder_drift`` for the daemon
# health endpoint.


class TestHolderVersionDrift:
    async def test_no_drift_when_holder_and_pool_share_source(self, socket_path):
        """Happy path: fresh holder + fresh pool loaded from same holder.py."""
        h = PtyHolder(socket_path)
        task = asyncio.create_task(h.serve())
        for _ in range(50):
            if h._server is not None:
                break
            await asyncio.sleep(0.05)

        p = ProcessPool(socket_path)
        try:
            connected = await p._try_connect()
            assert connected is True
            assert p.holder_drift["checked"] is True
            assert p.holder_drift["drift"] is False
            assert p.holder_drift["unknown"] is False
            # Both hashes come from the same source file, so they match.
            assert p.holder_drift["holder_hash"] == p.holder_drift["daemon_hash"]
            assert len(p.holder_drift["holder_hash"]) == 64
        finally:
            await p._disconnect()
            h._running = False
            h._shutdown_all()
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def test_drift_detected_when_holder_bytecode_stale(
        self, monkeypatch, socket_path, caplog
    ):
        """The scenario from 2026-04-24: holder.py on disk has moved but the
        running holder still reports its import-time hash. Pool must flip
        drift=True and log a warning naming the PID to kill."""
        import logging

        h = PtyHolder(socket_path)
        task = asyncio.create_task(h.serve())
        for _ in range(50):
            if h._server is not None:
                break
            await asyncio.sleep(0.05)

        # Simulate holder.py having been edited since the holder process
        # started: the daemon's current-source-hash helper returns a
        # different sha256 than the holder's import-time hash.
        monkeypatch.setattr(
            "swarm.pty.pool.holder_current_source_hash",
            lambda: "f" * 64,
        )

        p = ProcessPool(socket_path)
        try:
            with caplog.at_level(logging.WARNING, logger="swarm.pty.pool"):
                connected = await p._try_connect()
            assert connected is True
            assert p.holder_drift["drift"] is True
            assert p.holder_drift["daemon_hash"] == "f" * 64
            assert p.holder_drift["holder_hash"] != "f" * 64
            assert p.holder_drift["holder_pid"] > 0
            # The warning must name the PID and include the kill instructions
            # so the operator sees a path out without reading source.
            matched = [r for r in caplog.records if "[holder-drift]" in r.getMessage()]
            assert matched, "expected a [holder-drift] WARNING on the pool logger"
            msg = matched[-1].getMessage()
            assert str(p.holder_drift["holder_pid"]) in msg
            assert "restart swarm" in msg or "rm -f" in msg
        finally:
            await p._disconnect()
            h._running = False
            h._shutdown_all()
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def test_unknown_when_holder_predates_version_cmd(self, monkeypatch, socket_path):
        """Graceful degradation: an older holder (spawned before the
        ``version`` command shipped) returns ``{"ok": false, "error":
        "unknown cmd"}``. Pool records unknown=True but must not assert
        drift and must not break the connection."""
        h = PtyHolder(socket_path)
        # Strip the version handler so the holder behaves like a pre-skew-
        # detection build. We rebind the class attribute on this instance's
        # class so the test's handler map matches what an old deploy would
        # ship.
        original_handlers = PtyHolder._CMD_HANDLERS
        monkeypatch.setattr(
            PtyHolder,
            "_CMD_HANDLERS",
            {k: v for k, v in original_handlers.items() if k != "version"},
        )
        task = asyncio.create_task(h.serve())
        for _ in range(50):
            if h._server is not None:
                break
            await asyncio.sleep(0.05)

        p = ProcessPool(socket_path)
        try:
            connected = await p._try_connect()
            assert connected is True
            assert p.holder_drift["checked"] is True
            assert p.holder_drift["drift"] is False
            assert p.holder_drift["unknown"] is True
            assert p.holder_drift["holder_hash"] == ""
        finally:
            await p._disconnect()
            h._running = False
            h._shutdown_all()
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
