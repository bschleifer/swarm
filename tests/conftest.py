"""Shared test fixtures and helpers."""

from __future__ import annotations

import asyncio
import logging
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest

from swarm.worker.worker import Worker, WorkerState
from tests.fakes.process import FakeWorkerProcess

if TYPE_CHECKING:
    from swarm.server.daemon import SwarmDaemon


@pytest.fixture(autouse=True, scope="session")
def _isolate_logging():
    """Prevent tests from writing to the production ``~/.swarm/swarm.log``.

    CLI tests invoke click commands that call ``setup_logging()`` which
    attaches a ``RotatingFileHandler`` pointing at ``~/.swarm/swarm.log``.
    We patch ``setup_logging`` to redirect all file output to ``/dev/null``
    so test warnings never pollute the production debug log.
    """
    import swarm.cli as _cli
    import swarm.logging as _swarm_logging

    _real_setup = _swarm_logging.setup_logging

    def _test_setup(level="WARNING", log_file=None, stderr=False):
        return _real_setup(level=level, log_file="/dev/null", stderr=False)

    with (
        patch.object(_swarm_logging, "setup_logging", _test_setup),
        patch.object(_cli, "setup_logging", _test_setup),
    ):
        # Also neutralise the logger right now for tests that never
        # call setup_logging but still emit warnings.
        logger = logging.getLogger("swarm")
        logger.handlers.clear()
        logger.addHandler(logging.NullHandler())
        logger.setLevel(logging.WARNING)
        yield


def make_worker(
    name: str = "api",
    state: WorkerState = WorkerState.BUZZING,
    process: FakeWorkerProcess | None = None,
    resting_since: float | None = None,
    revive_count: int = 0,
    provider_name: str = "claude",
) -> Worker:
    """Create a Worker for testing.

    Parameters
    ----------
    name:
        Worker name.
    state:
        Initial worker state.
    process:
        Fake process for the worker. Defaults to a new ``FakeWorkerProcess``.
    resting_since:
        If set, overrides ``state_since`` (useful for escalation threshold tests).
    revive_count:
        Initial revive counter.
    provider_name:
        Provider name for the worker.
    """
    if process is None:
        process = FakeWorkerProcess(name=name)
    w = Worker(name=name, path="/tmp", provider_name=provider_name, process=process, state=state)
    if resting_since is not None:
        w.state_since = resting_since
    w.revive_count = revive_count
    return w


def make_daemon(
    monkeypatch: pytest.MonkeyPatch | None = None,
    workers: list[Worker] | None = None,
) -> SwarmDaemon:
    """Factory for a minimal SwarmDaemon suitable for unit tests.

    Stubs out Queen session persistence and creates the daemon via
    ``__new__`` (skipping ``__init__``) so no I/O occurs.
    """
    from swarm.config import HiveConfig, QueenConfig
    from swarm.drones.log import DroneLog
    from swarm.drones.pilot import DronePilot
    from swarm.queen.queen import Queen
    from swarm.queen.queue import QueenCallQueue
    from swarm.server.analyzer import QueenAnalyzer
    from swarm.server.config_manager import ConfigManager
    from swarm.server.daemon import SwarmDaemon
    from swarm.server.proposals import ProposalManager
    from swarm.server.task_manager import TaskManager
    from swarm.server.worker_service import WorkerService
    from swarm.tasks.board import TaskBoard
    from swarm.tasks.history import TaskHistory
    from swarm.tasks.proposal import ProposalStore
    from swarm.tunnel import TunnelManager

    if monkeypatch:
        monkeypatch.setattr("swarm.queen.queen.load_session", lambda _: None)
        monkeypatch.setattr("swarm.queen.queen.save_session", lambda *a: None)

    cfg = HiveConfig(session_name="test")
    d = SwarmDaemon.__new__(SwarmDaemon)
    d.config = cfg

    if workers is None:
        workers = [
            Worker(name="api", path="/tmp/api", process=FakeWorkerProcess(name="api")),
            Worker(name="web", path="/tmp/web", process=FakeWorkerProcess(name="web")),
        ]
    d.workers = workers
    d.pool = None
    d._worker_lock = asyncio.Lock()
    d.drone_log = DroneLog()
    d.task_board = TaskBoard()
    d.task_history = TaskHistory(log_file=Path(tempfile.mktemp(suffix=".jsonl")))
    d.queen = Queen(config=QueenConfig(cooldown=0.0), session_name="test")
    d.queen_queue = QueenCallQueue(max_concurrent=2)
    d.proposal_store = ProposalStore()
    d.proposals = ProposalManager(d.proposal_store, d)
    d.analyzer = QueenAnalyzer(d.queen, d, d.queen_queue)
    d.notification_bus = MagicMock()
    d.pilot = MagicMock(spec=DronePilot)
    d.pilot.enabled = True
    d.pilot.toggle = MagicMock(return_value=False)
    d.ws_clients = set()
    d.terminal_ws_clients = set()
    d.start_time = 0.0
    d.broadcast_ws = MagicMock()
    d.graph_mgr = None
    d._mtime_task = None
    d._usage_task = None
    d._heartbeat_task = None
    d._heartbeat_snapshot = {}
    d._config_mtime = 0.0
    d._state_dirty = False
    d._state_debounce_handle = None
    d._state_debounce_delay = 0.3
    d._bg_tasks: set[asyncio.Task[object]] = set()
    d.email = MagicMock()
    d.tasks = TaskManager(
        task_board=d.task_board,
        task_history=d.task_history,
        drone_log=d.drone_log,
        pilot=d.pilot,
    )
    d.config_mgr = ConfigManager(d)
    d.worker_svc = WorkerService(d)
    d.tunnel = TunnelManager(port=cfg.port)
    return d
