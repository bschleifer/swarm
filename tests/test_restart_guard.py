"""Regression tests: Queen RESTART must not kill active (non-STUNG) workers.

Root cause: _handle_restart() had no state guard, so the Queen could auto-exec
a RESTART on a BUZZING worker, killing it mid-work.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from swarm.drones.directives import DirectiveExecutor
from swarm.drones.log import DroneLog, SystemAction
from swarm.worker.manager import revive_worker
from swarm.worker.worker import Worker, WorkerState
from tests.conftest import make_worker
from tests.fakes.process import FakeWorkerProcess

# ── Helpers ──────────────────────────────────────────────────────────────


def _make_executor(
    workers: list[Worker],
    log: DroneLog | None = None,
    pool: AsyncMock | None = None,
) -> DirectiveExecutor:
    """Build a DirectiveExecutor with minimal fakes."""
    log = log or DroneLog()
    pool = pool or AsyncMock()

    async def _noop_safe_action(worker, coro, action, reason=""):
        await coro
        return True

    return DirectiveExecutor(
        workers=workers,
        log=log,
        pool=pool,
        queen=None,
        task_board=None,
        emit=MagicMock(),
        classify_worker_state=MagicMock(),
        get_provider=MagicMock(),
        safe_worker_action=_noop_safe_action,
        pending_proposals_check=None,
        proposed_completions={},
    )


def _make_fake_pool():
    pool = AsyncMock()

    async def fake_revive(name, cwd=None, command=None, shell_wrap=False):
        proc = FakeWorkerProcess(name=name, cwd=cwd or "/tmp")
        proc.pid = 9999
        return proc

    pool.revive = AsyncMock(side_effect=fake_revive)
    return pool


# ── _handle_restart state guard ──────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "state",
    [WorkerState.BUZZING, WorkerState.RESTING, WorkerState.SLEEPING, WorkerState.WAITING],
)
async def test_handle_restart_blocks_non_stung(state: WorkerState):
    """_handle_restart must refuse to restart a worker that isn't STUNG."""
    worker = make_worker("alice", state=state)
    log = DroneLog()
    executor = _make_executor([worker], log=log)

    directive = {"worker": "alice", "action": "restart", "reason": "stuck"}
    result = await executor._handle_restart(directive, worker)

    assert result is False
    blocked = [e for e in log.entries if e.action == SystemAction.QUEEN_BLOCKED]
    assert len(blocked) == 1
    assert state.value in blocked[0].detail


@pytest.mark.asyncio
async def test_handle_restart_allows_stung():
    """_handle_restart should proceed when the worker is STUNG."""
    pool = _make_fake_pool()
    worker = make_worker("alice", state=WorkerState.STUNG)
    log = DroneLog()
    executor = _make_executor([worker], log=log, pool=pool)

    directive = {"worker": "alice", "action": "restart", "reason": "exited", "_confidence": 0.9}
    result = await executor._handle_restart(directive, worker)

    assert result is True
    pool.revive.assert_called_once()


# ── revive_worker defensive guard ────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "state",
    [WorkerState.BUZZING, WorkerState.RESTING, WorkerState.SLEEPING, WorkerState.WAITING],
)
async def test_revive_worker_refuses_non_stung(state: WorkerState):
    """revive_worker must be a no-op when the worker is not STUNG."""
    pool = _make_fake_pool()
    fake_proc = FakeWorkerProcess(name="alice", cwd="/tmp")
    worker = Worker(name="alice", path="/tmp", process=fake_proc, state=state)

    await revive_worker(worker, pool)

    pool.revive.assert_not_called()
    # State unchanged
    assert worker.state == state


@pytest.mark.asyncio
async def test_revive_worker_allows_stung():
    """revive_worker should proceed normally for STUNG workers."""
    pool = _make_fake_pool()
    fake_proc = FakeWorkerProcess(name="alice", cwd="/tmp")
    worker = Worker(name="alice", path="/tmp", process=fake_proc, state=WorkerState.STUNG)

    await revive_worker(worker, pool)

    pool.revive.assert_called_once()
    assert worker.state == WorkerState.BUZZING
