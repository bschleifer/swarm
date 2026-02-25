"""Tests for server/worker_service.py — prep_for_task regression."""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from tests.fakes.process import FakeWorkerProcess
from swarm.config import HiveConfig, QueenConfig
from swarm.drones.log import DroneLog
from swarm.drones.pilot import DronePilot
from swarm.server.daemon import SwarmDaemon
from swarm.server.worker_service import WorkerService
from swarm.tasks.board import TaskBoard
from swarm.tasks.history import TaskHistory
from swarm.tasks.proposal import ProposalStore
from swarm.worker.worker import Worker, WorkerState


@pytest.fixture
def daemon(monkeypatch):
    """Minimal daemon with one shell-wrapped worker."""
    monkeypatch.setattr("swarm.queen.queen.load_session", lambda _: None)
    monkeypatch.setattr("swarm.queen.queen.save_session", lambda *a: None)

    cfg = HiveConfig(session_name="test")
    d = SwarmDaemon.__new__(SwarmDaemon)
    d.config = cfg
    d.pool = None
    d._worker_lock = asyncio.Lock()
    d.drone_log = DroneLog()
    d.task_board = TaskBoard()
    d.task_history = TaskHistory(log_file=Path(tempfile.mktemp(suffix=".jsonl")))

    from swarm.queen.queen import Queen
    from swarm.queen.queue import QueenCallQueue
    from swarm.server.analyzer import QueenAnalyzer
    from swarm.server.config_manager import ConfigManager
    from swarm.server.proposals import ProposalManager
    from swarm.server.task_manager import TaskManager
    from swarm.tunnel import TunnelManager

    d.queen = Queen(config=QueenConfig(cooldown=0.0), session_name="test")
    d.queen_queue = QueenCallQueue(max_concurrent=2)
    d.proposal_store = ProposalStore()
    d.proposals = ProposalManager(d.proposal_store, d)
    d.analyzer = QueenAnalyzer(d.queen, d, d.queen_queue)
    d.notification_bus = MagicMock()
    d.pilot = MagicMock(spec=DronePilot)
    d.pilot.enabled = True
    d.ws_clients = set()
    d.terminal_ws_clients = set()
    d.start_time = 0.0
    d.broadcast_ws = MagicMock()
    d.graph_mgr = None
    d._mtime_task = None
    d._usage_task = None
    d._heartbeat_task = None
    d._heartbeat_snapshot = {}
    d._state_dirty = False
    d._state_debounce_handle = None
    d._state_debounce_delay = 0.3
    d._bg_tasks: set[asyncio.Task[object]] = set()
    d.email = MagicMock()
    d.config_mgr = ConfigManager(d)
    d.worker_svc = WorkerService(d)
    d.tasks = TaskManager(
        task_board=d.task_board,
        task_history=d.task_history,
        drone_log=d.drone_log,
        pilot=d.pilot,
    )
    d.tunnel = TunnelManager(port=cfg.port)
    d._config_mtime = 0.0

    # Shell-wrapped worker: outer process is bash, child is claude
    proc = FakeWorkerProcess(name="alice")
    proc._foreground_command = "bash"
    proc._child_foreground_command = "claude"
    # Simulate a RESTING prompt so classify_output returns RESTING
    proc.set_content("$ ? for shortcuts\n")

    worker = Worker(name="alice", path="/tmp/alice", process=proc)
    d.workers = [worker]
    return d


@pytest.mark.asyncio
async def test_prep_for_task_uses_child_foreground_command(daemon):
    """Regression: prep_for_task must use get_child_foreground_command().

    With shell_wrap, get_foreground_command() returns 'bash' (the wrapper),
    causing classify_output() to return STUNG and _wait_for_idle() to never
    see RESTING. The fix uses get_child_foreground_command() which returns
    the actual inner process ('claude').
    """
    svc = daemon.worker_svc
    # Should complete without timing out
    await svc.prep_for_task("alice")

    worker = svc.get_worker("alice")
    # prep sends /get-latest then /clear — verify both were sent
    assert "/get-latest\n" in worker.process.keys_sent
    assert "/clear\n" in worker.process.keys_sent


@pytest.mark.asyncio
async def test_prep_for_task_times_out_when_not_idle(daemon):
    """prep_for_task should log a warning and return when worker never idles."""
    svc = daemon.worker_svc
    worker = svc.get_worker("alice")
    # Clear the buffer so there's no prompt — classify_output returns BUZZING
    worker.process.set_content("")

    # Use a very small timeout to avoid a slow test
    async def fast_prep(self, worker_name: str) -> None:
        """Wrapper that patches _wait_for_idle to use fewer polls."""
        from swarm.providers import get_provider

        w = self.require_worker(worker_name)
        provider = get_provider(w.provider_name)

        async def _wait_for_idle(timeout_polls: int = 3) -> bool:
            for _ in range(timeout_polls):
                await asyncio.sleep(0.0)
                cmd = w.process.get_child_foreground_command()
                content = w.process.get_content(35)
                state = provider.classify_output(cmd, content)
                if state == WorkerState.RESTING:
                    return True
            return False

        if not await _wait_for_idle():
            return

    await fast_prep(svc, "alice")
    # No keys should have been sent since the worker never became idle
    assert worker.process.keys_sent == []


@pytest.mark.asyncio
async def test_continue_all_skips_user_active_terminal(daemon):
    """continue_all should skip workers with an active web terminal."""
    svc = daemon.worker_svc
    worker = svc.get_worker("alice")
    worker.state = WorkerState.RESTING

    # Mark user as active in terminal
    worker.process.set_terminal_active(True)
    worker.process.mark_user_input()

    count = await svc.continue_all()
    assert count == 0
    assert len(worker.process.keys_sent) == 0


@pytest.mark.asyncio
async def test_send_all_skips_user_active_terminal(daemon):
    """send_all should skip workers with an active web terminal."""
    svc = daemon.worker_svc
    worker = svc.get_worker("alice")

    # Mark user as active in terminal
    worker.process.set_terminal_active(True)
    worker.process.mark_user_input()

    count = await svc.send_all("hello everyone")
    assert count == 0
    assert len(worker.process.keys_sent) == 0
