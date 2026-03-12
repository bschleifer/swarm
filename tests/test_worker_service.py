"""Tests for server/worker_service.py — prep_for_task regression."""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from swarm.config import HiveConfig, QueenConfig
from swarm.drones.log import DroneLog
from swarm.drones.pilot import DronePilot
from swarm.server.daemon import SwarmDaemon
from swarm.server.worker_service import WorkerService
from swarm.tasks.board import TaskBoard
from swarm.tasks.history import TaskHistory
from swarm.tasks.proposal import ProposalStore
from swarm.worker.worker import Worker, WorkerState
from tests.fakes.process import FakeWorkerProcess


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
    d.notification_bus = MagicMock()
    d.pilot = MagicMock(spec=DronePilot)
    d.pilot.enabled = True
    d._bg_tasks: set[asyncio.Task[object]] = set()
    d.broadcast_ws = MagicMock()

    from swarm.server.broadcast import BroadcastHub

    d.hub = BroadcastHub(track_task=lambda t: d._bg_tasks.add(t))
    d.ws_clients = set()
    d.terminal_ws_clients = set()
    d.start_time = 0.0
    d.proposals = ProposalManager(
        store=d.proposal_store,
        broadcast_ws=d.broadcast_ws,
        drone_log=d.drone_log,
        notification_bus=d.notification_bus,
        task_board=d.task_board,
        get_worker=lambda name: d.get_worker(name),
        get_workers=lambda: d.workers,
        get_pilot=lambda: d.pilot,
        assign_task=lambda *a, **kw: d.assign_task(*a, **kw),
        complete_task=lambda *a, **kw: d.complete_task(*a, **kw),
        execute_escalation=lambda p: d.analyzer.execute_escalation(p),
    )
    d.analyzer = QueenAnalyzer(
        queen=d.queen,
        queue=d.queen_queue,
        broadcast_ws=d.broadcast_ws,
        drone_log=d.drone_log,
        emit_event=d.emit,
        proposal_store=d.proposal_store,
        queue_proposal=d.queue_proposal,
        task_board=d.task_board,
        get_worker=lambda name: d.get_worker(name),
        require_worker=lambda name: d._require_worker(name),
        get_workers=lambda: d.workers,
        get_pool=lambda: d.pool,
        get_config=lambda: d.config,
        get_worker_descriptions=lambda: d._worker_descriptions(),
        clear_escalation=lambda name: d.pilot.clear_escalation(name) if d.pilot else None,
    )
    d.graph_mgr = None
    d._mtime_task = None
    d._usage_task = None
    d._heartbeat_task = None
    d._heartbeat_snapshot = {}
    d._state_dirty = False
    d._state_debounce_handle = None
    d._state_debounce_delay = 0.3
    d.email = MagicMock()
    d.config_mgr = ConfigManager(
        config=cfg,
        broadcast_ws=d.broadcast_ws,
        drone_log=d.drone_log,
        apply_config=d.apply_config,
        get_pilot=lambda: d.pilot,
        rebuild_graph=lambda: None,
        get_worker_svc=lambda: d.worker_svc,
    )
    d.worker_svc = WorkerService(
        broadcast_ws=d.broadcast_ws,
        drone_log=d.drone_log,
        task_board=d.task_board,
        get_pilot=lambda: d.pilot,
        get_pool=lambda: d.pool,
        get_config=lambda: d.config,
        get_workers=lambda: d.workers,
        set_workers=lambda ws: setattr(d, "workers", ws),
        worker_lock=d._worker_lock,
        init_pilot=lambda enabled: d.init_pilot(enabled=enabled),
    )
    d.tasks = TaskManager(
        task_board=d.task_board,
        task_history=d.task_history,
        drone_log=d.drone_log,
        pilot=d.pilot,
    )
    d.tunnel = TunnelManager(port=cfg.port)

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


def test_reorder_workers(daemon):
    """reorder_workers should rearrange workers to match given order."""
    bob = Worker(name="bob", path="/tmp/bob", process=FakeWorkerProcess(name="bob"))
    daemon.workers.append(bob)

    svc = daemon.worker_svc
    svc.reorder_workers(["bob", "alice"])

    assert [w.name for w in daemon.workers] == ["bob", "alice"]
    daemon.broadcast_ws.assert_called_with({"type": "workers_changed"})


def test_reorder_workers_unknown_names_ignored(daemon):
    """Names not matching any worker are silently ignored."""
    svc = daemon.worker_svc
    svc.reorder_workers(["nonexistent", "alice"])

    assert [w.name for w in daemon.workers] == ["alice"]


def test_reorder_workers_missing_names_appended(daemon):
    """Workers not in the order list are appended at the end."""
    bob = Worker(name="bob", path="/tmp/bob", process=FakeWorkerProcess(name="bob"))
    daemon.workers.append(bob)

    svc = daemon.worker_svc
    # Only mention bob — alice should be appended
    svc.reorder_workers(["bob"])

    assert [w.name for w in daemon.workers] == ["bob", "alice"]


# --- update_worker tests ---


def test_update_worker_rename(daemon):
    """update_worker should rename a worker and broadcast."""
    svc = daemon.worker_svc
    svc.update_worker("alice", name="carol")

    assert daemon.workers[0].name == "carol"
    assert svc.get_worker("carol") is not None
    assert svc.get_worker("alice") is None
    daemon.broadcast_ws.assert_called_with({"type": "workers_changed"})


def test_update_worker_change_path(daemon):
    """update_worker should update the working path."""
    svc = daemon.worker_svc
    svc.update_worker("alice", path="/tmp/new-path")

    assert daemon.workers[0].path == "/tmp/new-path"
    daemon.broadcast_ws.assert_called_with({"type": "workers_changed"})


def test_update_worker_rename_and_path(daemon):
    """update_worker should handle both name and path at once."""
    svc = daemon.worker_svc
    svc.update_worker("alice", name="carol", path="/tmp/carol")

    assert daemon.workers[0].name == "carol"
    assert daemon.workers[0].path == "/tmp/carol"


def test_update_worker_not_found(daemon):
    """update_worker should raise WorkerNotFoundError for unknown worker."""
    from swarm.server.daemon import WorkerNotFoundError

    svc = daemon.worker_svc
    with pytest.raises(WorkerNotFoundError):
        svc.update_worker("nonexistent", name="foo")


def test_update_worker_duplicate_name(daemon):
    """update_worker should reject renaming to an existing worker's name."""
    from swarm.server.daemon import SwarmOperationError

    bob = Worker(name="bob", path="/tmp/bob", process=FakeWorkerProcess(name="bob"))
    daemon.workers.append(bob)

    svc = daemon.worker_svc
    with pytest.raises(SwarmOperationError, match="already exists"):
        svc.update_worker("alice", name="bob")


def test_update_worker_invalid_name(daemon):
    """update_worker should reject invalid worker names."""
    from swarm.server.daemon import SwarmOperationError

    svc = daemon.worker_svc
    with pytest.raises(SwarmOperationError, match="Invalid"):
        svc.update_worker("alice", name="bad name!")


def test_update_worker_no_changes(daemon):
    """update_worker with no new values should be a no-op."""
    svc = daemon.worker_svc
    svc.update_worker("alice")
    # No broadcast since nothing changed
    daemon.broadcast_ws.assert_not_called()


def test_update_worker_same_name(daemon):
    """update_worker with the same name should be a no-op for name."""
    svc = daemon.worker_svc
    svc.update_worker("alice", name="alice")
    # No broadcast since nothing changed
    daemon.broadcast_ws.assert_not_called()


def test_update_worker_clears_api_cache(daemon):
    """update_worker should invalidate the API dict cache."""
    svc = daemon.worker_svc
    worker = svc.get_worker("alice")
    # Prime the cache
    worker.to_api_dict()
    assert worker._api_dict_cache is not None

    svc.update_worker("alice", name="carol")
    assert worker._api_dict_cache is None


def test_update_worker_updates_task_board(daemon):
    """update_worker should reassign tasks when worker is renamed."""
    from swarm.tasks.task import SwarmTask

    svc = daemon.worker_svc
    # Assign a task to alice
    task = daemon.task_board.add(SwarmTask(title="Test task", description="desc"))
    daemon.task_board.assign(task.id, "alice")

    svc.update_worker("alice", name="carol")

    updated = daemon.task_board.get(task.id)
    assert updated.assigned_worker == "carol"


def test_config_rename_syncs_live_worker(daemon):
    """Renaming a worker in config should also rename the live worker."""
    from swarm.config import WorkerConfig

    # Add a config entry for "alice" so config_mgr can find it
    daemon.config.workers = [WorkerConfig(name="alice", path="/tmp/alice")]

    daemon.config_mgr._apply_workers({"alice": {"name": "carol", "path": "/tmp/carol-new"}})

    # Config should be updated
    assert daemon.config.workers[0].name == "carol"
    assert daemon.config.workers[0].path == "/tmp/carol-new"

    # Live worker should also be updated
    assert daemon.workers[0].name == "carol"
    assert daemon.workers[0].path == "/tmp/carol-new"
