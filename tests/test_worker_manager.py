"""Tests for worker/manager.py — pool-based worker lifecycle."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from tests.fakes.process import FakeWorkerProcess
from swarm.config import WorkerConfig
from swarm.pty.process import ProcessError
from swarm.worker.manager import add_worker_live, kill_worker, launch_workers, revive_worker
from swarm.worker.worker import Worker, WorkerState


def _make_fake_pool(workers_dict: dict | None = None):
    """Create a mock ProcessPool that returns FakeWorkerProcess instances."""
    pool = AsyncMock()
    spawned = workers_dict if workers_dict is not None else {}

    async def fake_spawn(name, cwd, command=None, cols=200, rows=50):
        proc = FakeWorkerProcess(name=name, cwd=cwd)
        proc.pid = 1000 + len(spawned)
        spawned[name] = proc
        return proc

    async def fake_spawn_batch(worker_list, command=None, stagger_seconds=2.0):
        result = []
        for name, cwd in worker_list:
            proc = await fake_spawn(name, cwd, command=command)
            result.append(proc)
        return result

    async def fake_kill(name):
        if name in spawned:
            spawned[name]._alive = False
            del spawned[name]

    async def fake_revive(name):
        if name in spawned:
            old = spawned[name]
            old._alive = False
            proc = FakeWorkerProcess(name=name, cwd=old.cwd)
            proc.pid = 2000 + len(spawned)
            spawned[name] = proc
            return proc
        return None

    pool.spawn = AsyncMock(side_effect=fake_spawn)
    pool.spawn_batch = AsyncMock(side_effect=fake_spawn_batch)
    pool.kill = AsyncMock(side_effect=fake_kill)
    pool.revive = AsyncMock(side_effect=fake_revive)
    pool.get = MagicMock(side_effect=lambda name: spawned.get(name))
    return pool


@pytest.mark.asyncio
async def test_launch_workers_spawns_all():
    pool = _make_fake_pool()
    configs = [
        WorkerConfig(name="api", path="/tmp/api"),
        WorkerConfig(name="web", path="/tmp/web"),
    ]
    result = await launch_workers(pool, configs, stagger_seconds=0)

    pool.spawn_batch.assert_called_once()
    assert len(result) == 2
    assert result[0].name == "api"
    assert result[1].name == "web"
    assert result[0].process is not None
    assert result[1].process is not None


@pytest.mark.asyncio
async def test_launch_workers_sets_initial_state():
    pool = _make_fake_pool()
    configs = [WorkerConfig(name="api", path="/tmp/api")]
    result = await launch_workers(pool, configs)

    assert result[0].state == WorkerState.BUZZING


@pytest.mark.asyncio
async def test_launch_workers_passes_paths():
    pool = _make_fake_pool()
    configs = [WorkerConfig(name="api", path="/tmp/api")]
    result = await launch_workers(pool, configs)

    assert result[0].path == "/tmp/api"
    assert result[0].process.cwd == "/tmp/api"


@pytest.mark.asyncio
async def test_revive_worker_success():
    spawned = {}
    pool = _make_fake_pool(spawned)
    fake_proc = FakeWorkerProcess(name="api", cwd="/tmp/api")
    fake_proc.pid = 1000
    spawned["api"] = fake_proc

    worker = Worker(name="api", path="/tmp/api", process=fake_proc, state=WorkerState.STUNG)
    # Force past hysteresis
    worker._stung_confirmations = 2

    await revive_worker(worker, pool)

    pool.revive.assert_called_once_with("api")
    assert worker.process is not None
    assert worker.process.pid == 2001


@pytest.mark.asyncio
async def test_revive_worker_not_found():
    pool = _make_fake_pool()
    fake_proc = FakeWorkerProcess(name="ghost", cwd="/tmp")
    worker = Worker(name="ghost", path="/tmp", process=fake_proc, state=WorkerState.STUNG)

    await revive_worker(worker, pool)

    # Should not crash, process stays the same
    assert worker.process is fake_proc


@pytest.mark.asyncio
async def test_revive_worker_handles_error():
    pool = AsyncMock()
    pool.revive = AsyncMock(side_effect=ProcessError("holder dead"))

    fake_proc = FakeWorkerProcess(name="api", cwd="/tmp")
    worker = Worker(name="api", path="/tmp", process=fake_proc, state=WorkerState.STUNG)

    # Should not raise
    await revive_worker(worker, pool)


@pytest.mark.asyncio
async def test_add_worker_live_with_auto_start():
    pool = _make_fake_pool()
    workers: list[Worker] = []
    config = WorkerConfig(name="api", path="/tmp/api")

    worker = await add_worker_live(pool, config, workers, auto_start=True)

    assert worker.name == "api"
    assert worker.state == WorkerState.BUZZING
    assert worker.process is not None
    assert len(workers) == 1
    pool.spawn.assert_called_once_with("api", "/tmp/api", command=["claude", "--continue"])


@pytest.mark.asyncio
async def test_add_worker_live_without_auto_start():
    pool = _make_fake_pool()
    workers: list[Worker] = []
    config = WorkerConfig(name="api", path="/tmp/api")

    worker = await add_worker_live(pool, config, workers, auto_start=False)

    assert worker.state == WorkerState.RESTING
    pool.spawn.assert_called_once_with("api", "/tmp/api", command=["bash"])


@pytest.mark.asyncio
async def test_kill_worker_calls_pool():
    pool = _make_fake_pool()
    fake_proc = FakeWorkerProcess(name="api", cwd="/tmp")
    worker = Worker(name="api", path="/tmp", process=fake_proc)

    await kill_worker(worker, pool)

    pool.kill.assert_called_once_with("api")


@pytest.mark.asyncio
async def test_kill_worker_ignores_error():
    pool = AsyncMock()
    pool.kill = AsyncMock(side_effect=ProcessError("already dead"))

    fake_proc = FakeWorkerProcess(name="api", cwd="/tmp")
    worker = Worker(name="api", path="/tmp", process=fake_proc)

    # Should not raise
    await kill_worker(worker, pool)
