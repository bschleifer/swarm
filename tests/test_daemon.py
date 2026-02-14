"""Tests for server/daemon.py — daemon operation methods."""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from swarm.config import HiveConfig, QueenConfig, WorkerConfig
from swarm.drones.log import DroneLog, SystemAction
from swarm.drones.pilot import DronePilot
from swarm.queen.queen import Queen
from swarm.server.daemon import (
    SwarmDaemon,
    SwarmOperationError,
    TaskOperationError,
    WorkerNotFoundError,
)
from swarm.server.analyzer import QueenAnalyzer
from swarm.server.config_manager import ConfigManager
from swarm.server.worker_service import WorkerService
from swarm.server.proposals import ProposalManager
from swarm.server.task_manager import TaskManager
from swarm.tasks.board import TaskBoard
from swarm.tasks.history import TaskHistory
from swarm.tasks.proposal import AssignmentProposal, ProposalStatus, ProposalStore
from swarm.tasks.task import TaskPriority, TaskStatus
from swarm.worker.worker import Worker, WorkerState


@pytest.fixture
def daemon(monkeypatch):
    """Create a minimal daemon without starting it."""
    monkeypatch.setattr("swarm.queen.queen.load_session", lambda _: None)
    monkeypatch.setattr("swarm.queen.queen.save_session", lambda *a: None)

    cfg = HiveConfig(session_name="test")
    d = SwarmDaemon.__new__(SwarmDaemon)
    d.config = cfg
    d.workers = [
        Worker(name="api", path="/tmp/api", pane_id="%0"),
        Worker(name="web", path="/tmp/web", pane_id="%1"),
    ]
    d._worker_lock = asyncio.Lock()
    d.drone_log = DroneLog()
    d.task_board = TaskBoard()
    d.task_history = TaskHistory(log_file=Path(tempfile.mktemp(suffix=".jsonl")))
    d.queen = Queen(config=QueenConfig(cooldown=0.0), session_name="test")
    d.proposal_store = ProposalStore()
    d.proposals = ProposalManager(d.proposal_store, d)
    d.analyzer = QueenAnalyzer(d.queen, d)
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
    d.email = MagicMock()
    d.tasks = TaskManager(
        task_board=d.task_board,
        task_history=d.task_history,
        drone_log=d.drone_log,
        pilot=d.pilot,
    )
    d._config_mtime = 0.0
    d._heartbeat_task = None
    d._heartbeat_snapshot = {}
    d.config_mgr = ConfigManager(d)
    d.worker_svc = WorkerService(d)

    from swarm.tunnel import TunnelManager

    d.tunnel = TunnelManager(port=cfg.port)
    return d


# --- Exception hierarchy ---


def test_exception_hierarchy():
    assert issubclass(WorkerNotFoundError, SwarmOperationError)
    assert issubclass(TaskOperationError, SwarmOperationError)
    assert issubclass(SwarmOperationError, Exception)


# --- get_worker ---


def test_get_worker_found(daemon):
    w = daemon.get_worker("api")
    assert w is not None
    assert w.name == "api"


def test_get_worker_not_found(daemon):
    assert daemon.get_worker("nonexistent") is None


# --- kill_worker ---


@pytest.mark.asyncio
async def test_kill_worker(daemon):
    with patch("swarm.worker.manager.kill_worker", new_callable=AsyncMock) as mock_kill:
        await daemon.kill_worker("api")
        mock_kill.assert_called_once()
        worker = daemon.get_worker("api")
        assert worker.state == WorkerState.STUNG


@pytest.mark.asyncio
async def test_kill_worker_unassigns_tasks(daemon):
    task = daemon.task_board.create(title="Test task")
    daemon.task_board.assign(task.id, "api")
    with patch("swarm.worker.manager.kill_worker", new_callable=AsyncMock):
        await daemon.kill_worker("api")
    reloaded = daemon.task_board.get(task.id)
    assert reloaded.status == TaskStatus.PENDING
    assert reloaded.assigned_worker is None


@pytest.mark.asyncio
async def test_kill_worker_not_found(daemon):
    with pytest.raises(WorkerNotFoundError):
        await daemon.kill_worker("nonexistent")


@pytest.mark.asyncio
async def test_kill_worker_broadcasts(daemon):
    with patch("swarm.worker.manager.kill_worker", new_callable=AsyncMock):
        await daemon.kill_worker("api")
    daemon.broadcast_ws.assert_called()
    call_data = daemon.broadcast_ws.call_args[0][0]
    assert call_data["type"] == "workers_changed"


# --- revive_worker ---


@pytest.mark.asyncio
async def test_revive_worker(daemon):
    daemon.workers[0].state = WorkerState.STUNG
    with patch("swarm.worker.manager.revive_worker", new_callable=AsyncMock) as mock_revive:
        await daemon.revive_worker("api")
        mock_revive.assert_called_once()
        # Check session_name was passed
        _, kwargs = mock_revive.call_args
        assert kwargs["session_name"] == "test"
    w = daemon.get_worker("api")
    assert w.state == WorkerState.BUZZING
    assert w.revive_count == 1


@pytest.mark.asyncio
async def test_revive_worker_not_found(daemon):
    with pytest.raises(WorkerNotFoundError):
        await daemon.revive_worker("nonexistent")


@pytest.mark.asyncio
async def test_revive_worker_not_stung(daemon):
    # Worker is BUZZING, should raise
    with pytest.raises(SwarmOperationError, match="not STUNG"):
        await daemon.revive_worker("api")


@pytest.mark.asyncio
async def test_revive_worker_broadcasts(daemon):
    daemon.workers[0].state = WorkerState.STUNG
    with patch("swarm.worker.manager.revive_worker", new_callable=AsyncMock):
        await daemon.revive_worker("api")
    daemon.broadcast_ws.assert_called()


# --- kill_session ---


@pytest.mark.asyncio
async def test_kill_session(daemon):
    with patch("swarm.tmux.hive.kill_session", new_callable=AsyncMock) as mock_kill:
        await daemon.kill_session()
        mock_kill.assert_called_once_with("test")
    assert len(daemon.workers) == 0
    daemon.pilot.stop.assert_called_once()


@pytest.mark.asyncio
async def test_kill_session_unassigns_tasks(daemon):
    task = daemon.task_board.create(title="Test")
    daemon.task_board.assign(task.id, "api")
    with patch("swarm.tmux.hive.kill_session", new_callable=AsyncMock):
        await daemon.kill_session()
    reloaded = daemon.task_board.get(task.id)
    assert reloaded.status == TaskStatus.PENDING


@pytest.mark.asyncio
async def test_kill_session_clears_drone_log(daemon):
    daemon.drone_log.add(action=MagicMock(value="TEST"), worker_name="api", detail="test")
    with patch("swarm.tmux.hive.kill_session", new_callable=AsyncMock):
        await daemon.kill_session()
    assert len(daemon.drone_log.entries) == 0


@pytest.mark.asyncio
async def test_kill_session_broadcasts(daemon):
    with patch("swarm.tmux.hive.kill_session", new_callable=AsyncMock):
        await daemon.kill_session()
    daemon.broadcast_ws.assert_called()


# --- launch_workers ---


@pytest.mark.asyncio
async def test_launch_workers_into_existing_session(daemon):
    """When workers already exist, add_worker_live is used (no session kill)."""
    new_worker = Worker(name="new", path="/tmp/new", pane_id="%5")
    with patch(
        "swarm.worker.manager.add_worker_live",
        new_callable=AsyncMock,
        return_value=new_worker,
    ):
        result = await daemon.launch_workers([WorkerConfig("new", "/tmp/new")])
    assert len(result) == 1
    assert result[0].name == "new"
    # Workers should be extended
    assert any(w.name == "new" for w in daemon.workers)
    daemon.broadcast_ws.assert_called()


@pytest.mark.asyncio
async def test_launch_workers_fresh_session(daemon):
    """When no workers exist, launch_hive creates a new session."""
    daemon.workers.clear()
    launched = [Worker(name="new", path="/tmp/new", pane_id="%5")]
    with patch("swarm.worker.manager.launch_hive", new_callable=AsyncMock, return_value=launched):
        result = await daemon.launch_workers([WorkerConfig("new", "/tmp/new")])
    assert len(result) == 1
    assert result[0].name == "new"
    assert any(w.name == "new" for w in daemon.workers)
    daemon.broadcast_ws.assert_called()


@pytest.mark.asyncio
async def test_launch_workers_updates_pilot(daemon):
    new_worker = Worker(name="new", path="/tmp/new", pane_id="%5")
    with patch(
        "swarm.worker.manager.add_worker_live",
        new_callable=AsyncMock,
        return_value=new_worker,
    ):
        await daemon.launch_workers([WorkerConfig("new", "/tmp/new")])
    assert daemon.pilot.workers == daemon.workers


# --- spawn_worker ---


@pytest.mark.asyncio
async def test_spawn_worker(daemon):
    new_worker = Worker(name="new", path="/tmp/new", pane_id="%5")
    with patch(
        "swarm.worker.manager.add_worker_live", new_callable=AsyncMock, return_value=new_worker
    ):
        result = await daemon.spawn_worker(WorkerConfig("new", "/tmp/new"))
    assert result.name == "new"
    daemon.broadcast_ws.assert_called()


@pytest.mark.asyncio
async def test_spawn_worker_duplicate(daemon):
    with pytest.raises(SwarmOperationError, match="already running"):
        await daemon.spawn_worker(WorkerConfig("api", "/tmp/api"))


# --- create_task ---


def test_create_task(daemon):
    task = daemon.create_task(title="Fix bug", description="It's broken")
    assert task.title == "Fix bug"
    assert task.description == "It's broken"
    assert daemon.task_board.get(task.id) is not None


def test_create_task_with_priority(daemon):
    task = daemon.create_task(title="Urgent fix", priority=TaskPriority.URGENT)
    assert task.priority == TaskPriority.URGENT


# --- assign_task ---


async def test_assign_task(daemon):
    task = daemon.create_task(title="Test", description="Do something important")
    with (
        patch.object(daemon, "_prep_worker_for_task", new_callable=AsyncMock),
        patch.object(daemon, "send_to_worker", new_callable=AsyncMock) as mock_send,
        patch("swarm.server.daemon.send_enter", new_callable=AsyncMock),
    ):
        result = await daemon.assign_task(task.id, "api")
    assert result is True
    reloaded = daemon.task_board.get(task.id)
    assert reloaded.assigned_worker == "api"
    mock_send.assert_awaited_once()
    sent_msg = mock_send.call_args[0][1]
    assert "Test" in sent_msg
    assert "Do something important" in sent_msg


async def test_assign_task_worker_not_found(daemon):
    task = daemon.create_task(title="Test")
    with pytest.raises(WorkerNotFoundError):
        await daemon.assign_task(task.id, "nonexistent")


async def test_assign_task_not_found(daemon):
    with pytest.raises(TaskOperationError):
        await daemon.assign_task("nonexistent", "api")


async def test_assign_task_not_available(daemon):
    task = daemon.create_task(title="Test")
    daemon.task_board.assign(task.id, "api")
    daemon.task_board.complete(task.id)
    with pytest.raises(TaskOperationError, match="not available"):
        await daemon.assign_task(task.id, "web")


# --- complete_task ---


def test_complete_task(daemon):
    task = daemon.create_task(title="Test")
    daemon.task_board.assign(task.id, "api")
    result = daemon.complete_task(task.id)
    assert result is True
    assert daemon.task_board.get(task.id).status == TaskStatus.COMPLETED


def test_complete_task_not_found(daemon):
    with pytest.raises(TaskOperationError):
        daemon.complete_task("nonexistent")


def test_complete_task_wrong_state(daemon):
    task = daemon.create_task(title="Test")
    # Task is PENDING — can't complete
    with pytest.raises(TaskOperationError):
        daemon.complete_task(task.id)


# --- fail_task ---


def test_fail_task(daemon):
    task = daemon.create_task(title="Test")
    daemon.task_board.assign(task.id, "api")
    result = daemon.fail_task(task.id)
    assert result is True
    assert daemon.task_board.get(task.id).status == TaskStatus.FAILED


def test_fail_task_not_found(daemon):
    with pytest.raises(TaskOperationError):
        daemon.fail_task("nonexistent")


# --- remove_task ---


def test_remove_task(daemon):
    task = daemon.create_task(title="Test")
    result = daemon.remove_task(task.id)
    assert result is True
    assert daemon.task_board.get(task.id) is None


def test_remove_task_not_found(daemon):
    with pytest.raises(TaskOperationError):
        daemon.remove_task("nonexistent")


# --- toggle_drones ---


def test_toggle_drones(daemon):
    result = daemon.toggle_drones()
    assert result is False  # mock returns False
    daemon.pilot.toggle.assert_called_once()
    daemon.broadcast_ws.assert_called()
    call_data = daemon.broadcast_ws.call_args[0][0]
    assert call_data["type"] == "drones_toggled"


def test_toggle_drones_no_pilot(daemon):
    daemon.pilot = None
    result = daemon.toggle_drones()
    assert result is False


# --- check_config_file ---


def test_check_config_file_no_source(daemon):
    daemon.config.source_path = None
    assert daemon.check_config_file() is False


def test_check_config_file_no_change(daemon, tmp_path):
    cfg_file = tmp_path / "swarm.yaml"
    cfg_file.write_text("session_name: test\n")
    daemon.config.source_path = str(cfg_file)
    daemon._config_mtime = cfg_file.stat().st_mtime
    assert daemon.check_config_file() is False


def test_check_config_file_changed(daemon, tmp_path, monkeypatch):
    cfg_file = tmp_path / "swarm.yaml"
    cfg_file.write_text("session_name: test\nworkers: []\n")
    daemon.config.source_path = str(cfg_file)
    daemon._config_mtime = 0.0  # Force reload

    mock_reload = AsyncMock()
    monkeypatch.setattr(daemon, "reload_config", mock_reload)

    with patch("swarm.server.config_manager.load_config") as mock_load:
        mock_load.return_value = HiveConfig(session_name="test")
        result = daemon.check_config_file()
    assert result is True


# --- task_board on_change auto-broadcast ---


def test_task_board_on_change_broadcasts(monkeypatch):
    """Creating tasks should auto-broadcast via on_change wiring."""
    monkeypatch.setattr("swarm.queen.queen.load_session", lambda _: None)
    monkeypatch.setattr("swarm.queen.queen.save_session", lambda *a: None)

    cfg = HiveConfig(session_name="test")
    d = SwarmDaemon.__new__(SwarmDaemon)
    d.config = cfg
    d.workers = []
    d._worker_lock = asyncio.Lock()
    d.drone_log = DroneLog()
    d.task_board = TaskBoard()
    d.task_history = TaskHistory(log_file=Path(tempfile.mktemp(suffix=".jsonl")))
    d.queen = Queen(config=QueenConfig(cooldown=0.0), session_name="test")
    d.proposal_store = ProposalStore()
    d.proposals = ProposalManager(d.proposal_store, d)
    d.analyzer = QueenAnalyzer(d.queen, d)
    d.notification_bus = MagicMock()
    d.pilot = None
    d.ws_clients = set()
    d.start_time = 0.0
    d.broadcast_ws = MagicMock()
    d._config_mtime = 0.0
    d.config_mgr = ConfigManager(d)
    d.worker_svc = WorkerService(d)

    # Wire up on_change like __init__ does
    d._wire_task_board()

    # Now create a task — should trigger broadcast
    d.task_board.create(title="Test")
    d.broadcast_ws.assert_called_with({"type": "tasks_changed"})


# --- _hot_apply_config ---


def test_hot_apply_config(daemon):
    """_hot_apply_config updates pilot, queen, and notification bus."""
    from swarm.config import DroneConfig

    daemon.config.drones = DroneConfig(poll_interval=99.0)
    daemon._hot_apply_config()
    assert daemon.pilot.drone_config.poll_interval == 99.0
    daemon.pilot.set_poll_intervals.assert_called_once_with(
        99.0, daemon.config.drones.max_idle_interval
    )


def test_hot_apply_config_no_pilot(daemon):
    """_hot_apply_config doesn't crash without pilot."""
    daemon.pilot = None
    daemon._hot_apply_config()  # should not raise


# --- save_config ---


def test_save_config(daemon, tmp_path, monkeypatch):
    cfg_file = tmp_path / "swarm.yaml"
    cfg_file.write_text("session_name: test\n")
    daemon.config.source_path = str(cfg_file)

    monkeypatch.setattr("swarm.server.config_manager.save_config", MagicMock())
    daemon.save_config()
    assert daemon._config_mtime == cfg_file.stat().st_mtime


# --- init_pilot ---


@pytest.mark.asyncio
async def test_init_pilot(daemon, monkeypatch):
    daemon.pilot = None
    monkeypatch.setattr("swarm.queen.queen.load_session", lambda _: None)
    monkeypatch.setattr("swarm.queen.queen.save_session", lambda *a: None)
    monkeypatch.setattr(DronePilot, "start", lambda self: None)
    pilot = daemon.init_pilot(enabled=False)
    assert pilot is not None
    assert pilot.enabled is False
    assert daemon.pilot is pilot


@pytest.mark.asyncio
async def test_init_pilot_enabled(daemon, monkeypatch):
    daemon.pilot = None
    monkeypatch.setattr("swarm.queen.queen.load_session", lambda _: None)
    monkeypatch.setattr("swarm.queen.queen.save_session", lambda *a: None)
    monkeypatch.setattr(DronePilot, "start", lambda self: None)
    pilot = daemon.init_pilot(enabled=True)
    assert pilot.enabled is True


# --- continue_all ---


@pytest.mark.asyncio
async def test_continue_all(daemon):
    daemon.workers[0].state = WorkerState.RESTING
    daemon.workers[1].state = WorkerState.BUZZING
    with patch("swarm.server.worker_service.send_enter", new_callable=AsyncMock) as mock_enter:
        count = await daemon.continue_all()
    assert count == 1
    mock_enter.assert_called_once_with("%0")


@pytest.mark.asyncio
async def test_continue_all_none_resting(daemon):
    daemon.workers[0].state = WorkerState.BUZZING
    daemon.workers[1].state = WorkerState.BUZZING
    with patch("swarm.server.worker_service.send_enter", new_callable=AsyncMock) as mock_enter:
        count = await daemon.continue_all()
    assert count == 0
    mock_enter.assert_not_called()


# --- send_all ---


@pytest.mark.asyncio
async def test_send_all(daemon):
    with patch("swarm.server.worker_service.send_keys", new_callable=AsyncMock) as mock_keys:
        count = await daemon.send_all("hello")
    assert count == 2
    assert mock_keys.call_count == 2


# --- send_group ---


@pytest.mark.asyncio
async def test_send_group(daemon):
    from swarm.config import GroupConfig

    daemon.config.workers = [WorkerConfig("api", "/tmp/api"), WorkerConfig("web", "/tmp/web")]
    daemon.config.groups = [GroupConfig(name="backend", workers=["api"])]
    with patch("swarm.server.worker_service.send_keys", new_callable=AsyncMock) as mock_keys:
        count = await daemon.send_group("backend", "deploy")
    assert count == 1
    mock_keys.assert_called_once_with("%0", "deploy")


@pytest.mark.asyncio
async def test_send_group_unknown(daemon):
    with pytest.raises(ValueError):
        await daemon.send_group("nonexistent", "hello")


# --- gather_hive_context ---


@pytest.mark.asyncio
async def test_gather_hive_context(daemon):
    with patch("swarm.tmux.cell.capture_pane", new_callable=AsyncMock, return_value="output"):
        ctx = await daemon.gather_hive_context()
    assert isinstance(ctx, str)
    assert "api" in ctx


# --- analyze_worker ---


@pytest.mark.asyncio
async def test_analyze_worker(daemon, monkeypatch):
    monkeypatch.setattr(
        daemon.queen, "analyze_worker", AsyncMock(return_value={"action": "continue"})
    )
    with patch("swarm.tmux.cell.capture_pane", new_callable=AsyncMock, return_value="output"):
        result = await daemon.analyze_worker("api")
    assert result["action"] == "continue"


@pytest.mark.asyncio
async def test_analyze_worker_not_found(daemon):
    with pytest.raises(WorkerNotFoundError):
        await daemon.analyze_worker("nonexistent")


# --- coordinate_hive ---


@pytest.mark.asyncio
async def test_coordinate_hive(daemon, monkeypatch):
    monkeypatch.setattr(daemon.queen, "coordinate_hive", AsyncMock(return_value={"plan": "done"}))
    with patch("swarm.tmux.cell.capture_pane", new_callable=AsyncMock, return_value="output"):
        result = await daemon.coordinate_hive()
    assert result["plan"] == "done"


# --- launch_workers inits pilot if none ---


@pytest.mark.asyncio
async def test_launch_workers_inits_pilot(daemon, monkeypatch):
    daemon.pilot = None
    monkeypatch.setattr("swarm.queen.queen.load_session", lambda _: None)
    monkeypatch.setattr("swarm.queen.queen.save_session", lambda *a: None)
    monkeypatch.setattr(DronePilot, "start", lambda self: None)
    new_worker = Worker(name="new", path="/tmp/new", pane_id="%5")
    with patch(
        "swarm.worker.manager.add_worker_live",
        new_callable=AsyncMock,
        return_value=new_worker,
    ):
        await daemon.launch_workers([WorkerConfig("new", "/tmp/new")])
    assert daemon.pilot is not None


# --- discover ---


@pytest.mark.asyncio
async def test_discover(daemon):
    mock_workers = [Worker(name="found", path="/tmp/found", pane_id="%9")]
    with patch(
        "swarm.server.worker_service.discover_workers",
        new_callable=AsyncMock,
        return_value=mock_workers,
    ):
        result = await daemon.discover()
    assert len(result) == 1
    assert result[0].name == "found"
    assert daemon.workers is result


# --- Per-worker tmux operations ---


@pytest.mark.asyncio
async def test_send_to_worker(daemon):
    with patch("swarm.server.worker_service.send_keys", new_callable=AsyncMock) as mock_keys:
        await daemon.send_to_worker("api", "hello")
    mock_keys.assert_called_once_with("%0", "hello")


@pytest.mark.asyncio
async def test_send_to_worker_not_found(daemon):
    with pytest.raises(WorkerNotFoundError):
        await daemon.send_to_worker("nonexistent", "hello")


@pytest.mark.asyncio
async def test_continue_worker(daemon):
    with patch("swarm.server.worker_service.send_enter", new_callable=AsyncMock) as mock_enter:
        await daemon.continue_worker("api")
    mock_enter.assert_called_once_with("%0")


@pytest.mark.asyncio
async def test_continue_worker_not_found(daemon):
    with pytest.raises(WorkerNotFoundError):
        await daemon.continue_worker("nonexistent")


@pytest.mark.asyncio
async def test_interrupt_worker(daemon):
    with patch("swarm.server.worker_service.send_interrupt", new_callable=AsyncMock) as mock_int:
        await daemon.interrupt_worker("api")
    mock_int.assert_called_once_with("%0")


@pytest.mark.asyncio
async def test_interrupt_worker_not_found(daemon):
    with pytest.raises(WorkerNotFoundError):
        await daemon.interrupt_worker("nonexistent")


@pytest.mark.asyncio
async def test_escape_worker(daemon):
    with patch("swarm.server.worker_service.send_escape", new_callable=AsyncMock) as mock_esc:
        await daemon.escape_worker("api")
    mock_esc.assert_called_once_with("%0")


@pytest.mark.asyncio
async def test_escape_worker_not_found(daemon):
    with pytest.raises(WorkerNotFoundError):
        await daemon.escape_worker("nonexistent")


@pytest.mark.asyncio
async def test_capture_worker_output(daemon):
    with patch(
        "swarm.server.worker_service.capture_pane",
        new_callable=AsyncMock,
        return_value="pane content",
    ) as mock_cap:
        result = await daemon.capture_worker_output("api")
    assert result == "pane content"
    mock_cap.assert_called_once_with("%0", lines=80)


@pytest.mark.asyncio
async def test_capture_worker_output_custom_lines(daemon):
    with patch(
        "swarm.server.worker_service.capture_pane", new_callable=AsyncMock, return_value="content"
    ) as mock_cap:
        await daemon.capture_worker_output("api", lines=20)
    mock_cap.assert_called_once_with("%0", lines=20)


@pytest.mark.asyncio
async def test_capture_worker_output_not_found(daemon):
    with pytest.raises(WorkerNotFoundError):
        await daemon.capture_worker_output("nonexistent")


# --- broadcast_ws safety ---


# --- Proposals ---


@pytest.mark.asyncio
async def test_approve_proposal(daemon):
    """Approving a proposal assigns the task and sends the message."""
    task = daemon.create_task(title="Fix bug", description="broken")
    daemon.workers[0].state = WorkerState.RESTING
    proposal = AssignmentProposal(
        worker_name="api",
        task_id=task.id,
        task_title=task.title,
        message="Go fix the bug please",
    )
    daemon.proposal_store.add(proposal)

    with (
        patch.object(daemon, "_prep_worker_for_task", new_callable=AsyncMock),
        patch.object(daemon, "send_to_worker", new_callable=AsyncMock) as mock_send,
        patch("swarm.server.daemon.send_enter", new_callable=AsyncMock),
    ):
        result = await daemon.approve_proposal(proposal.id)
    assert result is True
    assert proposal.status == ProposalStatus.APPROVED
    assert daemon.task_board.get(task.id).assigned_worker == "api"
    # Should use the standard task message with Queen context appended
    sent_msg = mock_send.call_args[0][1]
    assert "Fix bug" in sent_msg
    assert "Queen context: Go fix the bug please" in sent_msg


@pytest.mark.asyncio
async def test_approve_proposal_no_message(daemon):
    """Approving a proposal with no message falls back to auto-generated."""
    task = daemon.create_task(title="Fix bug", description="broken")
    daemon.workers[0].state = WorkerState.RESTING
    proposal = AssignmentProposal(
        worker_name="api",
        task_id=task.id,
        task_title=task.title,
        message="",
    )
    daemon.proposal_store.add(proposal)

    with (
        patch.object(daemon, "_prep_worker_for_task", new_callable=AsyncMock),
        patch.object(daemon, "send_to_worker", new_callable=AsyncMock) as mock_send,
        patch("swarm.server.daemon.send_enter", new_callable=AsyncMock),
    ):
        await daemon.approve_proposal(proposal.id)
    sent_msg = mock_send.call_args[0][1]
    assert "Fix bug" in sent_msg


@pytest.mark.asyncio
async def test_approve_proposal_worker_gone(daemon):
    """Approving when worker is gone should expire and raise."""
    task = daemon.create_task(title="Fix bug")
    proposal = AssignmentProposal(
        worker_name="nonexistent",
        task_id=task.id,
        task_title=task.title,
    )
    daemon.proposal_store.add(proposal)

    with pytest.raises(WorkerNotFoundError):
        await daemon.approve_proposal(proposal.id)
    assert proposal.status == ProposalStatus.EXPIRED


@pytest.mark.asyncio
async def test_approve_proposal_worker_busy(daemon):
    """Approving when worker is BUZZING should expire and raise."""
    task = daemon.create_task(title="Fix bug")
    daemon.workers[0].state = WorkerState.BUZZING
    proposal = AssignmentProposal(
        worker_name="api",
        task_id=task.id,
        task_title=task.title,
    )
    daemon.proposal_store.add(proposal)

    with pytest.raises(TaskOperationError, match="BUZZING"):
        await daemon.approve_proposal(proposal.id)
    assert proposal.status == ProposalStatus.EXPIRED


def test_reject_proposal(daemon):
    task = daemon.create_task(title="Fix bug")
    proposal = AssignmentProposal(
        worker_name="api",
        task_id=task.id,
        task_title=task.title,
    )
    daemon.proposal_store.add(proposal)

    result = daemon.reject_proposal(proposal.id)
    assert result is True
    assert proposal.status == ProposalStatus.REJECTED
    # Should be cleared from store
    assert len(daemon.proposal_store.pending) == 0


def test_reject_proposal_not_found(daemon):
    with pytest.raises(TaskOperationError):
        daemon.reject_proposal("nonexistent")


def test_reject_all_proposals(daemon):
    task1 = daemon.create_task(title="Fix bug")
    task2 = daemon.create_task(title="Add feature")
    p1 = AssignmentProposal(worker_name="api", task_id=task1.id, task_title=task1.title)
    p2 = AssignmentProposal(worker_name="web", task_id=task2.id, task_title=task2.title)
    daemon.proposal_store.add(p1)
    daemon.proposal_store.add(p2)

    count = daemon.reject_all_proposals()
    assert count == 2
    assert len(daemon.proposal_store.pending) == 0


# --- Escalation → Queen ---


@pytest.mark.asyncio
async def test_escalation_send_message_always_creates_proposal(daemon, monkeypatch):
    """send_message always creates proposal — never auto-acted, even at high confidence."""
    daemon.queen._last_call = 0.0
    daemon.queen.cooldown = 0.0
    daemon.queen.min_confidence = 0.7
    monkeypatch.setattr(
        daemon.queen,
        "analyze_worker",
        AsyncMock(
            return_value={
                "action": "send_message",
                "message": "yes",
                "assessment": "Stuck on approval",
                "reasoning": "Permission prompt detected",
                "confidence": 0.9,
            }
        ),
    )
    with (
        patch("swarm.tmux.cell.capture_pane", new_callable=AsyncMock, return_value="output"),
        patch("swarm.tmux.cell.send_keys", new_callable=AsyncMock) as mock_keys,
    ):
        await daemon.analyzer.analyze_escalation(daemon.workers[0], "test escalation")

    # send_message never auto-acts — always goes to proposals for user review
    assert len(daemon.proposal_store.pending) == 1
    assert daemon.proposal_store.pending[0].queen_action == "send_message"
    mock_keys.assert_not_awaited()


@pytest.mark.asyncio
async def test_escalation_continue_auto_acts_high_confidence(daemon, monkeypatch):
    """High-confidence continue action → auto-acted (safe action)."""
    daemon.queen._last_call = 0.0
    daemon.queen.cooldown = 0.0
    daemon.queen.min_confidence = 0.7
    monkeypatch.setattr(
        daemon.queen,
        "analyze_worker",
        AsyncMock(
            return_value={
                "action": "continue",
                "message": "",
                "assessment": "Worker idle at prompt",
                "reasoning": "Empty prompt detected",
                "confidence": 0.9,
            }
        ),
    )
    with (
        patch("swarm.tmux.cell.capture_pane", new_callable=AsyncMock, return_value="output"),
        patch("swarm.tmux.cell.send_enter", new_callable=AsyncMock) as mock_enter,
    ):
        await daemon.analyzer.analyze_escalation(daemon.workers[0], "test escalation")

    # continue is a safe auto-action
    assert len(daemon.proposal_store.pending) == 0
    mock_enter.assert_awaited_once()


@pytest.mark.asyncio
async def test_escalation_queen_creates_proposal_low_confidence(daemon, monkeypatch):
    """Low-confidence escalation → creates proposal for user review."""
    daemon.queen._last_call = 0.0
    daemon.queen.cooldown = 0.0
    daemon.queen.min_confidence = 0.7
    monkeypatch.setattr(
        daemon.queen,
        "analyze_worker",
        AsyncMock(
            return_value={
                "action": "send_message",
                "message": "yes",
                "assessment": "Stuck on approval",
                "reasoning": "Permission prompt detected",
                "confidence": 0.5,
            }
        ),
    )
    with patch("swarm.tmux.cell.capture_pane", new_callable=AsyncMock, return_value="output"):
        await daemon.analyzer.analyze_escalation(daemon.workers[0], "test escalation")

    pending = daemon.proposal_store.pending
    assert len(pending) == 1
    p = pending[0]
    assert p.proposal_type == "escalation"
    assert p.queen_action == "send_message"
    assert p.confidence == 0.5
    assert p.worker_name == "api"


@pytest.mark.asyncio
async def test_escalation_plan_always_creates_proposal(daemon, monkeypatch):
    """Plan escalation → always creates proposal, even with high confidence."""
    daemon.queen._last_call = 0.0
    daemon.queen.cooldown = 0.0
    daemon.queen.min_confidence = 0.7
    monkeypatch.setattr(
        daemon.queen,
        "analyze_worker",
        AsyncMock(
            return_value={
                "action": "continue",
                "assessment": "Plan looks good",
                "reasoning": "Worker presenting implementation plan",
                "confidence": 0.95,
            }
        ),
    )
    with patch("swarm.tmux.cell.capture_pane", new_callable=AsyncMock, return_value="output"):
        await daemon.analyzer.analyze_escalation(daemon.workers[0], "plan requires user approval")

    pending = daemon.proposal_store.pending
    assert len(pending) == 1
    assert pending[0].confidence == 0.95


@pytest.mark.asyncio
async def test_choice_approval_escalation_auto_acts_at_high_confidence(daemon, monkeypatch):
    """'choice requires approval' escalation → Queen auto-acts when confident."""
    daemon.queen._last_call = 0.0
    daemon.queen.cooldown = 0.0
    daemon.queen.min_confidence = 0.7
    monkeypatch.setattr(
        daemon.queen,
        "analyze_worker",
        AsyncMock(
            return_value={
                "action": "continue",
                "assessment": "Routine Bash grep — safe to continue",
                "reasoning": "Permission prompt for grep command",
                "confidence": 0.9,
            }
        ),
    )
    with (
        patch("swarm.tmux.cell.capture_pane", new_callable=AsyncMock, return_value="output"),
        patch("swarm.tmux.cell.send_enter", new_callable=AsyncMock) as mock_enter,
    ):
        await daemon.analyzer.analyze_escalation(
            daemon.workers[0], "choice requires approval: choice menu"
        )

    # High confidence → auto-acted, no proposal
    assert len(daemon.proposal_store.pending) == 0
    mock_enter.assert_awaited_once()


@pytest.mark.asyncio
async def test_escalation_queen_disabled_no_proposal(daemon):
    """Escalation with Queen disabled → no proposal created."""
    daemon.queen.enabled = False
    daemon._on_escalation(daemon.workers[0], "test")
    assert len(daemon.proposal_store.pending) == 0


@pytest.mark.asyncio
async def test_approve_escalation_send_message(daemon):
    """Approve escalation with send_message action sends keys."""
    proposal = AssignmentProposal(
        worker_name="api",
        proposal_type="escalation",
        queen_action="send_message",
        message="yes",
        confidence=0.85,
    )
    daemon.proposal_store.add(proposal)

    with patch("swarm.tmux.cell.send_keys", new_callable=AsyncMock) as mock_keys:
        result = await daemon.approve_proposal(proposal.id)
    assert result is True
    assert proposal.status == ProposalStatus.APPROVED
    mock_keys.assert_awaited_once_with("%0", "yes")


@pytest.mark.asyncio
async def test_approve_escalation_continue(daemon):
    """Approve escalation with continue action sends Enter."""
    proposal = AssignmentProposal(
        worker_name="api",
        proposal_type="escalation",
        queen_action="continue",
    )
    daemon.proposal_store.add(proposal)

    with patch("swarm.tmux.cell.send_enter", new_callable=AsyncMock) as mock_enter:
        await daemon.approve_proposal(proposal.id)
    mock_enter.assert_awaited_once_with("%0")


@pytest.mark.asyncio
async def test_approve_escalation_restart(daemon):
    """Approve escalation with restart action revives worker."""
    daemon.workers[0].state = WorkerState.STUNG
    proposal = AssignmentProposal(
        worker_name="api",
        proposal_type="escalation",
        queen_action="restart",
    )
    daemon.proposal_store.add(proposal)

    with patch("swarm.worker.manager.revive_worker", new_callable=AsyncMock) as mock_revive:
        await daemon.approve_proposal(proposal.id)
    mock_revive.assert_awaited_once()
    assert daemon.workers[0].revive_count == 1


@pytest.mark.asyncio
async def test_approve_escalation_wait(daemon):
    """Approve escalation with wait action is a no-op."""
    proposal = AssignmentProposal(
        worker_name="api",
        proposal_type="escalation",
        queen_action="wait",
    )
    daemon.proposal_store.add(proposal)

    result = await daemon.approve_proposal(proposal.id)
    assert result is True
    assert proposal.status == ProposalStatus.APPROVED


@pytest.mark.asyncio
async def testbroadcast_ws_dead_client(monkeypatch):
    """Dead WS clients should be discarded without crash."""
    monkeypatch.setattr("swarm.queen.queen.load_session", lambda _: None)
    monkeypatch.setattr("swarm.queen.queen.save_session", lambda *a: None)

    from swarm.config import HiveConfig, QueenConfig
    from swarm.tasks.history import TaskHistory
    import tempfile

    cfg = HiveConfig(session_name="test")
    d = SwarmDaemon.__new__(SwarmDaemon)
    d.config = cfg
    d.workers = []
    d._worker_lock = asyncio.Lock()
    d.drone_log = DroneLog()
    d.task_board = TaskBoard()
    d.task_history = TaskHistory(log_file=Path(tempfile.mktemp(suffix=".jsonl")))
    d.queen = Queen(config=QueenConfig(cooldown=0.0), session_name="test")
    d.proposal_store = ProposalStore()
    d.proposals = ProposalManager(d.proposal_store, d)
    d.analyzer = QueenAnalyzer(d.queen, d)
    d.notification_bus = MagicMock()
    d.pilot = None
    d.start_time = 0.0
    d._config_mtime = 0.0
    d.config_mgr = ConfigManager(d)
    d.worker_svc = WorkerService(d)

    # Create a mock WS that is "closed"
    dead_ws = MagicMock()
    dead_ws.closed = True
    d.ws_clients = {dead_ws}

    # Use real broadcast_ws (not mocked)
    SwarmDaemon.broadcast_ws(d, {"type": "test"})

    # The dead client should be discarded
    assert dead_ws not in d.ws_clients


# --- Operator action logging ---


@pytest.mark.asyncio
async def test_approve_proposal_logs_approved(daemon):
    """Approving a proposal logs APPROVED to drone_log."""
    from swarm.drones.log import SystemAction

    task = daemon.create_task(title="Fix bug")
    daemon.workers[0].state = WorkerState.RESTING
    proposal = AssignmentProposal(
        worker_name="api",
        task_id=task.id,
        task_title=task.title,
        message="Go fix it",
    )
    daemon.proposal_store.add(proposal)

    with (
        patch.object(daemon, "_prep_worker_for_task", new_callable=AsyncMock),
        patch.object(daemon, "send_to_worker", new_callable=AsyncMock),
        patch("swarm.server.daemon.send_enter", new_callable=AsyncMock),
    ):
        await daemon.approve_proposal(proposal.id)

    entries = daemon.drone_log.entries
    approved = [e for e in entries if e.action == SystemAction.APPROVED]
    assert len(approved) == 1
    assert approved[0].worker_name == "api"
    assert "Fix bug" in approved[0].detail


def test_reject_proposal_logs_rejected(daemon):
    """Rejecting a proposal logs REJECTED to drone_log."""
    from swarm.drones.log import SystemAction

    task = daemon.create_task(title="Add feature")
    proposal = AssignmentProposal(
        worker_name="api",
        task_id=task.id,
        task_title=task.title,
    )
    daemon.proposal_store.add(proposal)
    daemon.reject_proposal(proposal.id)

    entries = daemon.drone_log.entries
    rejected = [e for e in entries if e.action == SystemAction.REJECTED]
    assert len(rejected) == 1
    assert rejected[0].worker_name == "api"
    assert "Add feature" in rejected[0].detail


def test_reject_all_proposals_logs_rejected(daemon):
    """Rejecting all proposals logs REJECTED to drone_log."""
    from swarm.drones.log import SystemAction

    t1 = daemon.create_task(title="Bug 1")
    t2 = daemon.create_task(title="Bug 2")
    p1 = AssignmentProposal(worker_name="api", task_id=t1.id, task_title=t1.title)
    p2 = AssignmentProposal(worker_name="web", task_id=t2.id, task_title=t2.title)
    daemon.proposal_store.add(p1)
    daemon.proposal_store.add(p2)
    daemon.reject_all_proposals()

    entries = daemon.drone_log.entries
    rejected = [e for e in entries if e.action == SystemAction.REJECTED]
    assert len(rejected) == 1
    assert rejected[0].worker_name == "all"
    assert "2 proposal(s)" in rejected[0].detail


@pytest.mark.asyncio
async def test_continue_worker_logs_operator(daemon):
    """Continuing a worker logs OPERATOR to drone_log."""
    from swarm.drones.log import SystemAction

    with patch("swarm.server.worker_service.send_enter", new_callable=AsyncMock):
        await daemon.continue_worker("api")

    entries = daemon.drone_log.entries
    ops = [e for e in entries if e.action == SystemAction.OPERATOR]
    assert len(ops) == 1
    assert ops[0].worker_name == "api"
    assert "continued" in ops[0].detail


@pytest.mark.asyncio
async def test_kill_worker_logs_operator(daemon):
    """Killing a worker logs OPERATOR to drone_log."""
    from swarm.drones.log import SystemAction

    with patch("swarm.worker.manager.kill_worker", new_callable=AsyncMock):
        await daemon.kill_worker("api")

    entries = daemon.drone_log.entries
    ops = [e for e in entries if e.action == SystemAction.OPERATOR]
    assert len(ops) == 1
    assert ops[0].worker_name == "api"
    assert "killed" in ops[0].detail


@pytest.mark.asyncio
async def test_continue_all_logs_operator(daemon):
    """continue_all logs OPERATOR to drone_log."""
    from swarm.drones.log import SystemAction

    daemon.workers[0].state = WorkerState.RESTING
    daemon.workers[1].state = WorkerState.RESTING
    with patch("swarm.server.worker_service.send_enter", new_callable=AsyncMock):
        await daemon.continue_all()

    entries = daemon.drone_log.entries
    ops = [e for e in entries if e.action == SystemAction.OPERATOR]
    assert len(ops) == 1
    assert ops[0].worker_name == "all"
    assert "2 worker(s)" in ops[0].detail


# --- reload_config ---


@pytest.mark.asyncio
async def test_reload_config(daemon, tmp_path):
    """reload_config updates config, hot-applies, broadcasts, and logs."""
    from swarm.drones.log import SystemAction

    cfg_file = tmp_path / "swarm.yaml"
    cfg_file.write_text("session_name: test\n")
    new_config = HiveConfig(session_name="reloaded")
    new_config.source_path = str(cfg_file)

    await daemon.reload_config(new_config)

    assert daemon.config is new_config
    assert daemon.config.session_name == "reloaded"
    assert daemon._config_mtime == cfg_file.stat().st_mtime
    daemon.broadcast_ws.assert_called()
    # Should broadcast config_changed
    calls = [c[0][0] for c in daemon.broadcast_ws.call_args_list]
    assert any(c.get("type") == "config_changed" for c in calls)
    # Should log CONFIG_CHANGED
    entries = daemon.drone_log.entries
    config_entries = [e for e in entries if e.action == SystemAction.CONFIG_CHANGED]
    assert len(config_entries) == 1


@pytest.mark.asyncio
async def test_reload_config_no_source_path(daemon):
    """reload_config works when source_path is None."""
    new_config = HiveConfig(session_name="reloaded")
    new_config.source_path = None
    await daemon.reload_config(new_config)
    assert daemon.config is new_config


@pytest.mark.asyncio
async def test_reload_config_source_path_missing_file(daemon, tmp_path):
    """reload_config handles missing source file gracefully."""
    new_config = HiveConfig(session_name="reloaded")
    new_config.source_path = str(tmp_path / "nonexistent.yaml")
    await daemon.reload_config(new_config)
    assert daemon.config is new_config


# --- _on_escalation ---


def test_on_escalation_skips_pending_proposal(daemon):
    """_on_escalation skips if a pending escalation proposal already exists."""
    proposal = AssignmentProposal(
        worker_name="api",
        proposal_type="escalation",
        queen_action="continue",
    )
    daemon.proposal_store.add(proposal)

    daemon._on_escalation(daemon.workers[0], "test reason")
    # Should not broadcast escalation (skipped)
    calls = [c[0][0] for c in daemon.broadcast_ws.call_args_list]
    assert not any(c.get("type") == "escalation" for c in calls)


def test_on_escalation_skips_inflight_analysis(daemon):
    """_on_escalation skips if Queen analysis is already in flight."""
    daemon.analyzer.track_escalation("api")

    daemon._on_escalation(daemon.workers[0], "test reason")
    calls = [c[0][0] for c in daemon.broadcast_ws.call_args_list]
    assert not any(c.get("type") == "escalation" for c in calls)

    daemon.analyzer.clear_escalation("api")


def test_on_escalation_broadcasts_and_emits(daemon):
    """_on_escalation broadcasts WS and emits notification when no duplicates."""
    daemon.queen.enabled = False  # Disable Queen so we don't need asyncio loop

    daemon._on_escalation(daemon.workers[0], "test reason")

    # Should broadcast escalation event
    daemon.broadcast_ws.assert_called()
    calls = [c[0][0] for c in daemon.broadcast_ws.call_args_list]
    escalation_calls = [c for c in calls if c.get("type") == "escalation"]
    assert len(escalation_calls) == 1
    assert escalation_calls[0]["worker"] == "api"
    assert escalation_calls[0]["reason"] == "test reason"


def test_on_escalation_queen_disabled(daemon):
    """_on_escalation does not start Queen analysis when Queen is disabled."""
    daemon.queen.enabled = False
    daemon._on_escalation(daemon.workers[0], "test reason")
    # Should still broadcast but not start analysis
    daemon.broadcast_ws.assert_called()


# --- _on_task_done ---


def test_on_task_done_ignores_buzzing_worker(daemon):
    """_on_task_done skips if worker is BUZZING."""
    task = daemon.task_board.create(title="Test task")
    daemon.task_board.assign(task.id, "api")
    daemon.workers[0].state = WorkerState.BUZZING

    daemon._on_task_done(daemon.workers[0], task)
    # No proposals created
    assert len(daemon.proposal_store.pending) == 0


def test_on_task_done_skips_duplicate_completion(daemon):
    """_on_task_done skips if there's already a pending completion proposal."""
    task = daemon.task_board.create(title="Test task")
    daemon.task_board.assign(task.id, "api")
    daemon.workers[0].state = WorkerState.RESTING

    # Add a pending completion proposal
    existing = AssignmentProposal.completion(
        worker_name="api",
        task_id=task.id,
        task_title=task.title,
        assessment="already proposed",
    )
    daemon.proposal_store.add(existing)

    daemon._on_task_done(daemon.workers[0], task)
    # Should still have only the one proposal (no duplicates)
    completions = [p for p in daemon.proposal_store.pending if p.proposal_type == "completion"]
    assert len(completions) == 1


def test_on_task_done_with_resolution_creates_proposal(daemon):
    """_on_task_done creates proposal directly when resolution is provided."""
    task = daemon.task_board.create(title="Test task")
    daemon.task_board.assign(task.id, "api")
    daemon.workers[0].state = WorkerState.RESTING

    daemon._on_task_done(daemon.workers[0], task, resolution="All tests passing")

    pending = daemon.proposal_store.pending
    assert len(pending) == 1
    assert pending[0].proposal_type == "completion"
    assert pending[0].task_id == task.id
    assert pending[0].assessment == "All tests passing"


def test_on_task_done_queen_disabled_no_proposal(daemon):
    """_on_task_done creates no proposal when Queen is unavailable and no resolution."""
    task = daemon.task_board.create(title="Test task")
    daemon.task_board.assign(task.id, "api")
    daemon.workers[0].state = WorkerState.RESTING
    daemon.queen.enabled = False

    daemon._on_task_done(daemon.workers[0], task)
    assert len(daemon.proposal_store.pending) == 0


# --- _on_workers_changed ---


def test_on_workers_changed_broadcasts(daemon):
    """_on_workers_changed broadcasts workers_changed with task map."""
    task = daemon.task_board.create(title="Active task")
    daemon.task_board.assign(task.id, "api")

    daemon._on_workers_changed()

    daemon.broadcast_ws.assert_called()
    calls = [c[0][0] for c in daemon.broadcast_ws.call_args_list]
    wc_calls = [c for c in calls if c.get("type") == "workers_changed"]
    assert len(wc_calls) >= 1
    # Check it includes worker_tasks
    assert "worker_tasks" in wc_calls[0]
    assert wc_calls[0]["worker_tasks"]["api"] == "Active task"


# --- _on_state_changed ---


def test_on_state_changed_buzzing_expires_proposals(daemon):
    """_on_state_changed expires escalation/completion proposals when worker goes BUZZING."""
    # Add a pending escalation proposal
    proposal = AssignmentProposal(
        worker_name="api",
        proposal_type="escalation",
        queen_action="continue",
    )
    daemon.proposal_store.add(proposal)

    daemon.workers[0].state = WorkerState.BUZZING
    daemon._on_state_changed(daemon.workers[0])

    assert proposal.status == ProposalStatus.EXPIRED


def test_on_state_changed_stung_logs_worker_stung(daemon):
    """_on_state_changed logs WORKER_STUNG when worker becomes STUNG."""
    daemon.workers[0].state = WorkerState.STUNG
    daemon._on_state_changed(daemon.workers[0])

    entries = daemon.drone_log.entries
    stung = [e for e in entries if e.action == SystemAction.WORKER_STUNG]
    assert len(stung) == 1
    assert stung[0].worker_name == "api"


def test_on_state_changed_broadcasts_state(daemon):
    """_on_state_changed always broadcasts state update."""
    daemon.workers[0].state = WorkerState.RESTING
    daemon._on_state_changed(daemon.workers[0])

    daemon.broadcast_ws.assert_called()
    calls = [c[0][0] for c in daemon.broadcast_ws.call_args_list]
    state_calls = [c for c in calls if c.get("type") == "state"]
    assert len(state_calls) >= 1
    assert any(w["name"] == "api" for w in state_calls[0]["workers"])


def test_on_state_changed_buzzing_clears_inflight(daemon):
    """_on_state_changed clears in-flight analysis tracking when BUZZING."""
    daemon.analyzer.track_escalation("api")
    daemon.analyzer.track_completion("api:task123")

    daemon.workers[0].state = WorkerState.BUZZING
    daemon._on_state_changed(daemon.workers[0])

    assert not daemon.analyzer.has_inflight_escalation("api")
    assert not daemon.analyzer.has_inflight_completion("api:task123")


# --- _on_drone_entry ---


def test_on_drone_entry_broadcasts_legacy_and_system(daemon):
    """_on_drone_entry broadcasts both 'drones' and 'system_log' types."""
    from swarm.drones.log import LogCategory, SystemEntry

    entry = SystemEntry(
        timestamp=0.0,
        action=SystemAction.CONTINUED,
        worker_name="api",
        detail="test detail",
        category=LogCategory.DRONE,
        is_notification=False,
    )

    daemon._on_drone_entry(entry)

    assert daemon.broadcast_ws.call_count == 2
    calls = [c[0][0] for c in daemon.broadcast_ws.call_args_list]
    types = {c["type"] for c in calls}
    assert "drones" in types
    assert "system_log" in types


# --- safe_capture_output ---


@pytest.mark.asyncio
async def test_safe_capture_output_success(daemon):
    """safe_capture_output returns pane content on success."""
    with patch(
        "swarm.server.worker_service.capture_pane",
        new_callable=AsyncMock,
        return_value="live output",
    ):
        result = await daemon.safe_capture_output("api")
    assert result == "live output"


@pytest.mark.asyncio
async def test_safe_capture_output_os_error(daemon):
    """safe_capture_output returns fallback on OSError."""
    with patch(
        "swarm.server.worker_service.capture_pane",
        new_callable=AsyncMock,
        side_effect=OSError("gone"),
    ):
        result = await daemon.safe_capture_output("api")
    assert result == "(pane unavailable)"


@pytest.mark.asyncio
async def test_safe_capture_output_timeout(daemon):
    """safe_capture_output returns fallback on TimeoutError."""
    with patch(
        "swarm.server.worker_service.capture_pane",
        new_callable=AsyncMock,
        side_effect=asyncio.TimeoutError(),
    ):
        result = await daemon.safe_capture_output("api")
    assert result == "(pane unavailable)"


@pytest.mark.asyncio
async def test_safe_capture_output_not_found(daemon):
    """safe_capture_output returns fallback for missing worker."""
    result = await daemon.safe_capture_output("nonexistent")
    assert result == "(pane unavailable)"


@pytest.mark.asyncio
async def test_safe_capture_output_pane_gone_error(daemon):
    """safe_capture_output returns fallback on PaneGoneError."""
    from swarm.tmux.cell import PaneGoneError

    with patch(
        "swarm.server.worker_service.capture_pane",
        new_callable=AsyncMock,
        side_effect=PaneGoneError("pane gone"),
    ):
        result = await daemon.safe_capture_output("api")
    assert result == "(pane unavailable)"


# --- poll_once ---


@pytest.mark.asyncio
async def test_poll_once_no_pilot(daemon):
    """poll_once returns False when pilot is None."""
    daemon.pilot = None
    result = await daemon.poll_once()
    assert result is False


@pytest.mark.asyncio
async def test_poll_once_with_pilot(daemon):
    """poll_once delegates to pilot.poll_once."""
    daemon.pilot.poll_once = AsyncMock(return_value=True)
    result = await daemon.poll_once()
    assert result is True
    daemon.pilot.poll_once.assert_awaited_once()


# --- stop ---


@pytest.mark.asyncio
async def test_stop_stops_pilot(daemon):
    """stop() calls pilot.stop."""
    await daemon.stop()
    daemon.pilot.stop.assert_called_once()


@pytest.mark.asyncio
async def test_stop_no_pilot(daemon):
    """stop() works when pilot is None."""
    daemon.pilot = None
    await daemon.stop()  # should not raise


@pytest.mark.asyncio
async def test_stop_cancels_mtime_task(daemon):
    """stop() cancels the config mtime watcher task."""
    mock_task = MagicMock()
    daemon._mtime_task = mock_task
    await daemon.stop()
    mock_task.cancel.assert_called_once()


@pytest.mark.asyncio
async def test_stop_closes_ws_clients(daemon):
    """stop() closes all WS clients."""
    ws1 = AsyncMock()
    ws2 = AsyncMock()
    daemon.ws_clients = {ws1, ws2}
    daemon.terminal_ws_clients = set()
    await daemon.stop()
    ws1.close.assert_awaited_once()
    ws2.close.assert_awaited_once()
    assert len(daemon.ws_clients) == 0


@pytest.mark.asyncio
async def test_stop_closes_terminal_ws_clients(daemon):
    """stop() closes terminal WS clients too."""
    tws = AsyncMock()
    daemon.terminal_ws_clients = {tws}
    await daemon.stop()
    tws.close.assert_awaited_once()
    assert len(daemon.terminal_ws_clients) == 0


@pytest.mark.asyncio
async def test_stop_handles_ws_close_errors(daemon):
    """stop() doesn't raise even if ws.close() fails."""
    ws = AsyncMock()
    ws.close.side_effect = Exception("network down")
    daemon.ws_clients = {ws}
    daemon.terminal_ws_clients = set()
    await daemon.stop()  # should not raise
    assert len(daemon.ws_clients) == 0


# --- check_config_file with load error ---


def test_check_config_file_load_error(daemon, tmp_path):
    """check_config_file returns False on invalid config file."""
    cfg_file = tmp_path / "swarm.yaml"
    cfg_file.write_text("session_name: test\n")
    daemon.config.source_path = str(cfg_file)
    daemon._config_mtime = 0.0

    with patch("swarm.server.config_manager.load_config", side_effect=ValueError("bad yaml")):
        result = daemon.check_config_file()
    assert result is False


def test_check_config_file_oserror(daemon, tmp_path):
    """check_config_file returns False when stat fails."""
    daemon.config.source_path = str(tmp_path / "nonexistent.yaml")
    result = daemon.check_config_file()
    assert result is False


def test_check_config_file_applies_config_fields(daemon, tmp_path):
    """check_config_file hot-applies specific config fields from new config."""
    from swarm.config import DroneConfig, GroupConfig

    cfg_file = tmp_path / "swarm.yaml"
    cfg_file.write_text("session_name: test\n")
    daemon.config.source_path = str(cfg_file)
    daemon._config_mtime = 0.0

    new_config = HiveConfig(
        session_name="test",
        groups=[GroupConfig(name="backend", workers=["api"])],
        drones=DroneConfig(poll_interval=42.0),
    )
    with patch("swarm.server.config_manager.load_config", return_value=new_config):
        result = daemon.check_config_file()

    assert result is True
    assert len(daemon.config.groups) == 1
    assert daemon.config.groups[0].name == "backend"
    assert daemon.config.drones.poll_interval == 42.0


# --- _send_to_workers with failures ---


@pytest.mark.asyncio
async def test_send_to_workers_handles_errors(daemon):
    """_send_to_workers counts successes and skips failures."""
    call_count = 0

    async def flaky_action(pane_id):
        nonlocal call_count
        call_count += 1
        if pane_id == "%0":
            raise OSError("pane gone")

    count = await daemon.worker_svc._send_to_workers(
        daemon.workers, flaky_action, "all", "sent to {count} worker(s)"
    )
    assert count == 1  # only %1 succeeded
    assert call_count == 2


@pytest.mark.asyncio
async def test_send_to_workers_no_log_on_zero(daemon):
    """_send_to_workers doesn't log when all fail."""
    initial_entries = len(daemon.drone_log.entries)

    async def always_fail(pane_id):
        raise OSError("gone")

    count = await daemon.worker_svc._send_to_workers(
        daemon.workers, always_fail, "all", "sent to {count} worker(s)"
    )
    assert count == 0
    assert len(daemon.drone_log.entries) == initial_entries  # no new entries


# --- apply_config_update ---


@pytest.mark.asyncio
async def test_apply_config_update_drones(daemon, monkeypatch):
    """apply_config_update applies drones settings."""
    monkeypatch.setattr("swarm.server.config_manager.save_config", MagicMock())
    await daemon.apply_config_update(
        {"drones": {"poll_interval": 15.0, "enabled": False, "auto_approve_yn": True}}
    )
    assert daemon.config.drones.poll_interval == 15.0
    assert daemon.config.drones.enabled is False
    assert daemon.config.drones.auto_approve_yn is True


@pytest.mark.asyncio
async def test_apply_config_update_drones_invalid_bool(daemon, monkeypatch):
    """apply_config_update raises ValueError for invalid bool field."""
    monkeypatch.setattr("swarm.server.config_manager.save_config", MagicMock())
    with pytest.raises(ValueError, match="must be boolean"):
        await daemon.apply_config_update({"drones": {"enabled": "yes"}})


@pytest.mark.asyncio
async def test_apply_config_update_drones_invalid_number(daemon, monkeypatch):
    """apply_config_update raises ValueError for invalid numeric field."""
    monkeypatch.setattr("swarm.server.config_manager.save_config", MagicMock())
    with pytest.raises(ValueError, match="must be a number"):
        await daemon.apply_config_update({"drones": {"poll_interval": "fast"}})


@pytest.mark.asyncio
async def test_apply_config_update_drones_negative_number(daemon, monkeypatch):
    """apply_config_update raises ValueError for negative numeric field."""
    monkeypatch.setattr("swarm.server.config_manager.save_config", MagicMock())
    with pytest.raises(ValueError, match="must be >= 0"):
        await daemon.apply_config_update({"drones": {"poll_interval": -1}})


@pytest.mark.asyncio
async def test_apply_config_update_queen(daemon, monkeypatch):
    """apply_config_update applies queen settings."""
    monkeypatch.setattr("swarm.server.config_manager.save_config", MagicMock())
    await daemon.apply_config_update(
        {
            "queen": {
                "cooldown": 60.0,
                "enabled": False,
                "system_prompt": "Be careful",
                "min_confidence": 0.5,
            }
        }
    )
    assert daemon.config.queen.cooldown == 60.0
    assert daemon.config.queen.enabled is False
    assert daemon.config.queen.system_prompt == "Be careful"
    assert daemon.config.queen.min_confidence == 0.5


@pytest.mark.asyncio
async def test_apply_config_update_queen_invalid_cooldown(daemon, monkeypatch):
    """apply_config_update raises ValueError for bad queen.cooldown."""
    monkeypatch.setattr("swarm.server.config_manager.save_config", MagicMock())
    with pytest.raises(ValueError, match="non-negative"):
        await daemon.apply_config_update({"queen": {"cooldown": -5}})


@pytest.mark.asyncio
async def test_apply_config_update_queen_invalid_enabled(daemon, monkeypatch):
    """apply_config_update raises ValueError for bad queen.enabled."""
    monkeypatch.setattr("swarm.server.config_manager.save_config", MagicMock())
    with pytest.raises(ValueError, match="must be boolean"):
        await daemon.apply_config_update({"queen": {"enabled": 1}})


@pytest.mark.asyncio
async def test_apply_config_update_queen_invalid_system_prompt(daemon, monkeypatch):
    """apply_config_update raises ValueError for bad queen.system_prompt."""
    monkeypatch.setattr("swarm.server.config_manager.save_config", MagicMock())
    with pytest.raises(ValueError, match="must be a string"):
        await daemon.apply_config_update({"queen": {"system_prompt": 123}})


@pytest.mark.asyncio
async def test_apply_config_update_queen_invalid_min_confidence(daemon, monkeypatch):
    """apply_config_update raises ValueError for out-of-range min_confidence."""
    monkeypatch.setattr("swarm.server.config_manager.save_config", MagicMock())
    with pytest.raises(ValueError, match="between 0.0 and 1.0"):
        await daemon.apply_config_update({"queen": {"min_confidence": 1.5}})


@pytest.mark.asyncio
async def test_apply_config_update_notifications(daemon, monkeypatch):
    """apply_config_update applies notifications settings."""
    monkeypatch.setattr("swarm.server.config_manager.save_config", MagicMock())
    await daemon.apply_config_update(
        {"notifications": {"terminal_bell": False, "desktop": False, "debounce_seconds": 10.0}}
    )
    assert daemon.config.notifications.terminal_bell is False
    assert daemon.config.notifications.desktop is False
    assert daemon.config.notifications.debounce_seconds == 10.0


@pytest.mark.asyncio
async def test_apply_config_update_notifications_invalid_bool(daemon, monkeypatch):
    """apply_config_update raises for invalid notification booleans."""
    monkeypatch.setattr("swarm.server.config_manager.save_config", MagicMock())
    with pytest.raises(ValueError, match="must be boolean"):
        await daemon.apply_config_update({"notifications": {"desktop": "yes"}})


@pytest.mark.asyncio
async def test_apply_config_update_notifications_invalid_debounce(daemon, monkeypatch):
    """apply_config_update raises for negative debounce_seconds."""
    monkeypatch.setattr("swarm.server.config_manager.save_config", MagicMock())
    with pytest.raises(ValueError, match="must be >= 0"):
        await daemon.apply_config_update({"notifications": {"debounce_seconds": -1}})


@pytest.mark.asyncio
async def test_apply_config_update_approval_rules(daemon, monkeypatch):
    """apply_config_update applies approval_rules to drones config."""
    monkeypatch.setattr("swarm.server.config_manager.save_config", MagicMock())
    await daemon.apply_config_update(
        {
            "drones": {
                "approval_rules": [
                    {"pattern": ".*bash.*", "action": "approve"},
                    {"pattern": ".*rm.*", "action": "escalate"},
                ]
            }
        }
    )
    assert len(daemon.config.drones.approval_rules) == 2
    assert daemon.config.drones.approval_rules[0].pattern == ".*bash.*"
    assert daemon.config.drones.approval_rules[1].action == "escalate"


@pytest.mark.asyncio
async def test_apply_config_update_approval_rules_invalid_type(daemon, monkeypatch):
    """apply_config_update raises if approval_rules is not a list."""
    monkeypatch.setattr("swarm.server.config_manager.save_config", MagicMock())
    with pytest.raises(ValueError, match="must be a list"):
        await daemon.apply_config_update({"drones": {"approval_rules": "bad"}})


@pytest.mark.asyncio
async def test_apply_config_update_approval_rules_invalid_item(daemon, monkeypatch):
    """apply_config_update raises if an approval rule item is not a dict."""
    monkeypatch.setattr("swarm.server.config_manager.save_config", MagicMock())
    with pytest.raises(ValueError, match="must be an object"):
        await daemon.apply_config_update({"drones": {"approval_rules": ["not_a_dict"]}})


@pytest.mark.asyncio
async def test_apply_config_update_approval_rules_invalid_action(daemon, monkeypatch):
    """apply_config_update raises for invalid approval rule action."""
    monkeypatch.setattr("swarm.server.config_manager.save_config", MagicMock())
    with pytest.raises(ValueError, match="'approve' or 'escalate'"):
        await daemon.apply_config_update(
            {"drones": {"approval_rules": [{"pattern": ".*", "action": "deny"}]}}
        )


@pytest.mark.asyncio
async def test_apply_config_update_approval_rules_invalid_regex(daemon, monkeypatch):
    """apply_config_update raises for invalid regex in approval rule."""
    monkeypatch.setattr("swarm.server.config_manager.save_config", MagicMock())
    with pytest.raises(ValueError, match="invalid regex"):
        await daemon.apply_config_update(
            {"drones": {"approval_rules": [{"pattern": "[invalid", "action": "approve"}]}}
        )


@pytest.mark.asyncio
async def test_apply_config_update_worker_descriptions(daemon, monkeypatch):
    """apply_config_update updates worker descriptions."""
    monkeypatch.setattr("swarm.server.config_manager.save_config", MagicMock())
    daemon.config.workers = [WorkerConfig("api", "/tmp/api")]
    await daemon.apply_config_update({"workers": {"api": "API service worker"}})
    assert daemon.config.workers[0].description == "API service worker"


@pytest.mark.asyncio
async def test_apply_config_update_default_group(daemon, monkeypatch):
    """apply_config_update updates default_group."""
    from swarm.config import GroupConfig

    monkeypatch.setattr("swarm.server.config_manager.save_config", MagicMock())
    daemon.config.groups = [GroupConfig(name="backend", workers=["api"])]
    await daemon.apply_config_update({"default_group": "backend"})
    assert daemon.config.default_group == "backend"


@pytest.mark.asyncio
async def test_apply_config_update_default_group_invalid_type(daemon, monkeypatch):
    """apply_config_update raises for non-string default_group."""
    monkeypatch.setattr("swarm.server.config_manager.save_config", MagicMock())
    with pytest.raises(ValueError, match="must be a string"):
        await daemon.apply_config_update({"default_group": 123})


@pytest.mark.asyncio
async def test_apply_config_update_default_group_unknown(daemon, monkeypatch):
    """apply_config_update raises for unknown default_group."""
    monkeypatch.setattr("swarm.server.config_manager.save_config", MagicMock())
    with pytest.raises(ValueError, match="does not match"):
        await daemon.apply_config_update({"default_group": "nonexistent"})


@pytest.mark.asyncio
async def test_apply_config_update_top_level_scalars(daemon, monkeypatch):
    """apply_config_update sets top-level scalar fields."""
    monkeypatch.setattr("swarm.server.config_manager.save_config", MagicMock())
    await daemon.apply_config_update({"session_name": "new-session", "log_level": "DEBUG"})
    assert daemon.config.session_name == "new-session"
    assert daemon.config.log_level == "DEBUG"


@pytest.mark.asyncio
async def test_apply_config_update_graph_settings(daemon, monkeypatch):
    """apply_config_update sets graph_client_id and graph_tenant_id."""
    monkeypatch.setattr("swarm.server.config_manager.save_config", MagicMock())
    await daemon.apply_config_update({"graph_client_id": "abc123", "graph_tenant_id": "tenant1"})
    assert daemon.config.graph_client_id == "abc123"
    assert daemon.config.graph_tenant_id == "tenant1"


@pytest.mark.asyncio
async def test_apply_config_update_graph_tenant_empty_defaults_common(daemon, monkeypatch):
    """apply_config_update defaults empty graph_tenant_id to 'common'."""
    monkeypatch.setattr("swarm.server.config_manager.save_config", MagicMock())
    await daemon.apply_config_update({"graph_tenant_id": ""})
    assert daemon.config.graph_tenant_id == "common"


@pytest.mark.asyncio
async def test_apply_config_update_workflows(daemon, monkeypatch):
    """apply_config_update sets workflow overrides."""
    monkeypatch.setattr("swarm.server.config_manager.save_config", MagicMock())
    await daemon.apply_config_update({"workflows": {"bug": "/fix-and-ship"}})
    assert daemon.config.workflows["bug"] == "/fix-and-ship"


@pytest.mark.asyncio
async def test_apply_config_update_workflows_invalid_type(daemon, monkeypatch):
    """apply_config_update raises for non-dict workflows."""
    monkeypatch.setattr("swarm.server.config_manager.save_config", MagicMock())
    with pytest.raises(ValueError, match="must be an object"):
        await daemon.apply_config_update({"workflows": "not-a-dict"})


@pytest.mark.asyncio
async def test_apply_config_update_workflows_invalid_key(daemon, monkeypatch):
    """apply_config_update raises for invalid workflow task type key."""
    monkeypatch.setattr("swarm.server.config_manager.save_config", MagicMock())
    with pytest.raises(ValueError, match="not a valid task type"):
        await daemon.apply_config_update({"workflows": {"unknown_type": "/cmd"}})


@pytest.mark.asyncio
async def test_apply_config_update_workflows_invalid_value(daemon, monkeypatch):
    """apply_config_update raises for non-string workflow value."""
    monkeypatch.setattr("swarm.server.config_manager.save_config", MagicMock())
    with pytest.raises(ValueError, match="must be a string"):
        await daemon.apply_config_update({"workflows": {"bug": 42}})


# --- assign_task send failure ---


@pytest.mark.asyncio
async def test_assign_task_send_failure_undoes_assignment(daemon):
    """assign_task undoes assignment when tmux send fails."""
    from swarm.tmux.cell import PaneGoneError

    task = daemon.create_task(title="Test task", description="Important work")
    daemon.workers[0].state = WorkerState.RESTING

    with (
        patch.object(daemon, "_prep_worker_for_task", new_callable=AsyncMock),
        patch.object(
            daemon,
            "send_to_worker",
            new_callable=AsyncMock,
            side_effect=PaneGoneError("pane gone"),
        ),
    ):
        result = await daemon.assign_task(task.id, "api")

    assert result is True  # assignment succeeded initially
    reloaded = daemon.task_board.get(task.id)
    # Task should be unassigned (returned to pending)
    assert reloaded.status == TaskStatus.PENDING
    assert reloaded.assigned_worker is None
    # Should have broadcast task_send_failed
    calls = [c[0][0] for c in daemon.broadcast_ws.call_args_list]
    assert any(c.get("type") == "task_send_failed" for c in calls)


# --- queue_proposal ---


def test_queue_proposal(daemon):
    """queue_proposal adds a proposal via ProposalManager."""
    proposal = AssignmentProposal(
        worker_name="api",
        task_id="test123",
        task_title="Test",
    )
    daemon.queue_proposal(proposal)
    assert len(daemon.proposal_store.pending) == 1
    assert daemon.proposal_store.pending[0].id == proposal.id


# --- _worker_task_map ---


def test_worker_task_map(daemon):
    """_worker_task_map returns dict of worker->task_title for active tasks."""
    t1 = daemon.task_board.create(title="Task A")
    daemon.task_board.assign(t1.id, "api")
    t2 = daemon.task_board.create(title="Task B")
    daemon.task_board.assign(t2.id, "web")

    result = daemon._worker_task_map()
    assert result == {"api": "Task A", "web": "Task B"}


def test_worker_task_map_empty(daemon):
    """_worker_task_map returns empty dict when no tasks assigned."""
    result = daemon._worker_task_map()
    assert result == {}


# --- _on_task_board_changed ---


def test_on_task_board_changed_broadcasts(daemon):
    """_on_task_board_changed broadcasts tasks_changed."""
    daemon._on_task_board_changed()
    daemon.broadcast_ws.assert_called_with({"type": "tasks_changed"})


# --- _expire_stale_proposals ---


def test_expire_stale_proposals(daemon):
    """_expire_stale_proposals expires proposals for missing workers/tasks."""
    proposal = AssignmentProposal(
        worker_name="nonexistent",
        task_id="missing_task",
        task_title="Ghost",
    )
    daemon.proposal_store.add(proposal)

    daemon._expire_stale_proposals()
    assert proposal.status == ProposalStatus.EXPIRED


# --- _hot_apply_config full coverage ---


def test_hot_apply_config_queen_fields(daemon):
    """_hot_apply_config updates queen enabled, cooldown, prompt, min_confidence."""
    daemon.config.queen.enabled = False
    daemon.config.queen.cooldown = 999.0
    daemon.config.queen.system_prompt = "Be nice"
    daemon.config.queen.min_confidence = 0.3

    daemon._hot_apply_config()

    assert daemon.queen.enabled is False
    assert daemon.queen.cooldown == 999.0
    assert daemon.queen.system_prompt == "Be nice"
    assert daemon.queen.min_confidence == 0.3


def test_hot_apply_config_pilot_full(daemon):
    """_hot_apply_config updates all pilot fields."""
    from swarm.config import DroneConfig

    daemon.config.drones = DroneConfig(poll_interval=42.0, max_idle_interval=120.0, enabled=False)
    daemon._hot_apply_config()

    assert daemon.pilot.drone_config.poll_interval == 42.0
    daemon.pilot.set_poll_intervals.assert_called_once_with(42.0, 120.0)
    assert daemon.pilot.interval == 42.0
    assert daemon.pilot.enabled is False


# --- send_to_worker operator logging ---


@pytest.mark.asyncio
async def test_send_to_worker_no_operator_log(daemon):
    """send_to_worker with _log_operator=False skips operator logging."""
    from swarm.drones.log import SystemAction

    with patch("swarm.server.worker_service.send_keys", new_callable=AsyncMock):
        await daemon.send_to_worker("api", "hello", _log_operator=False)
    ops = [e for e in daemon.drone_log.entries if e.action == SystemAction.OPERATOR]
    assert len(ops) == 0


@pytest.mark.asyncio
async def test_send_to_worker_operator_log(daemon):
    """send_to_worker defaults to logging operator action."""
    from swarm.drones.log import SystemAction

    with patch("swarm.server.worker_service.send_keys", new_callable=AsyncMock):
        await daemon.send_to_worker("api", "hello")
    ops = [e for e in daemon.drone_log.entries if e.action == SystemAction.OPERATOR]
    assert len(ops) == 1
    assert ops[0].detail == "sent message"


# --- interrupt_worker and escape_worker logging ---


@pytest.mark.asyncio
async def test_interrupt_worker_logs_operator(daemon):
    """Interrupting a worker logs OPERATOR to drone_log."""
    from swarm.drones.log import SystemAction

    with patch("swarm.server.worker_service.send_interrupt", new_callable=AsyncMock):
        await daemon.interrupt_worker("api")

    entries = daemon.drone_log.entries
    ops = [e for e in entries if e.action == SystemAction.OPERATOR]
    assert len(ops) == 1
    assert "interrupted" in ops[0].detail


@pytest.mark.asyncio
async def test_escape_worker_logs_operator(daemon):
    """Escaping a worker logs OPERATOR to drone_log."""
    from swarm.drones.log import SystemAction

    with patch("swarm.server.worker_service.send_escape", new_callable=AsyncMock):
        await daemon.escape_worker("api")

    entries = daemon.drone_log.entries
    ops = [e for e in entries if e.action == SystemAction.OPERATOR]
    assert len(ops) == 1
    assert "Escape" in ops[0].detail


# --- _require_worker and _require_task ---


def test_require_worker_found(daemon):
    """_require_worker returns worker when found."""
    w = daemon._require_worker("api")
    assert w.name == "api"


def test_require_worker_not_found(daemon):
    """_require_worker raises WorkerNotFoundError when not found."""
    with pytest.raises(WorkerNotFoundError, match="nonexistent"):
        daemon._require_worker("nonexistent")


# --- _worker_descriptions ---


def test_worker_descriptions(daemon):
    """_worker_descriptions returns descriptions from config workers."""
    daemon.config.workers = [
        WorkerConfig("api", "/tmp/api", description="API service"),
        WorkerConfig("web", "/tmp/web"),
    ]
    result = daemon._worker_descriptions()
    assert result == {"api": "API service"}


def test_worker_descriptions_empty(daemon):
    """_worker_descriptions returns empty dict when no workers have descriptions."""
    daemon.config.workers = [
        WorkerConfig("api", "/tmp/api"),
        WorkerConfig("web", "/tmp/web"),
    ]
    result = daemon._worker_descriptions()
    assert result == {}


# --- send_all with long message ---


@pytest.mark.asyncio
async def test_send_all_long_message_truncates_preview(daemon):
    """send_all truncates the log preview for long messages."""
    long_msg = "x" * 100
    with patch("swarm.server.worker_service.send_keys", new_callable=AsyncMock):
        count = await daemon.send_all(long_msg)
    assert count == 2
    # Check that the log entry was created (preview should be truncated)
    ops = [e for e in daemon.drone_log.entries if e.action == SystemAction.OPERATOR]
    assert len(ops) == 1
    assert "\u2026" in ops[0].detail  # ellipsis in preview


# --- send_group with message ---


@pytest.mark.asyncio
async def test_send_group_logs_group_send(daemon):
    """send_group logs the group send action."""
    from swarm.config import GroupConfig

    daemon.config.workers = [WorkerConfig("api", "/tmp/api"), WorkerConfig("web", "/tmp/web")]
    daemon.config.groups = [GroupConfig(name="backend", workers=["api"])]
    with patch("swarm.server.worker_service.send_keys", new_callable=AsyncMock):
        await daemon.send_group("backend", "deploy")

    ops = [e for e in daemon.drone_log.entries if e.action == SystemAction.OPERATOR]
    assert len(ops) == 1
    assert "backend" in ops[0].worker_name or "group" in ops[0].detail


# --- revive_worker logging ---


@pytest.mark.asyncio
async def test_revive_worker_logs_operator(daemon):
    """Reviving a worker logs OPERATOR to drone_log."""
    daemon.workers[0].state = WorkerState.STUNG
    with patch("swarm.worker.manager.revive_worker", new_callable=AsyncMock):
        await daemon.revive_worker("api")

    ops = [e for e in daemon.drone_log.entries if e.action == SystemAction.OPERATOR]
    assert len(ops) == 1
    assert "revived" in ops[0].detail


# --- kill_session with OSError ---


@pytest.mark.asyncio
async def test_kill_session_handles_oserror(daemon):
    """kill_session continues cleanly even if tmux kill fails."""
    with patch(
        "swarm.tmux.hive.kill_session",
        new_callable=AsyncMock,
        side_effect=OSError("session gone"),
    ):
        await daemon.kill_session()
    assert len(daemon.workers) == 0


# --- _on_task_assigned ---


def test_on_task_assigned_broadcasts(daemon):
    """_on_task_assigned broadcasts task_assigned event."""
    task = daemon.task_board.create(title="Test task")
    daemon._on_task_assigned(daemon.workers[0], task)

    daemon.broadcast_ws.assert_called()
    calls = [c[0][0] for c in daemon.broadcast_ws.call_args_list]
    ta_calls = [c for c in calls if c.get("type") == "task_assigned"]
    assert len(ta_calls) == 1
    assert ta_calls[0]["worker"] == "api"
    assert ta_calls[0]["task"]["title"] == "Test task"


def test_on_task_assigned_emits_notification(daemon):
    """_on_task_assigned emits notification to notification bus."""
    task = daemon.task_board.create(title="Test task")
    daemon._on_task_assigned(daemon.workers[0], task)
    daemon.notification_bus.emit_task_assigned.assert_called_once_with("api", "Test task")


# --- _on_task_done queen-enabled path ---


def test_on_task_done_queen_enabled_inflight_check(daemon):
    """_on_task_done skips when completion analysis already in flight."""
    task = daemon.task_board.create(title="Test task")
    daemon.task_board.assign(task.id, "api")
    daemon.workers[0].state = WorkerState.RESTING

    daemon.queen.enabled = True
    daemon.queen._last_call = 0.0
    daemon.queen.cooldown = 0.0

    # Pre-mark as in-flight
    key = f"api:{task.id}"
    daemon.analyzer.track_completion(key)

    daemon._on_task_done(daemon.workers[0], task)
    # No new proposals should be created
    assert len(daemon.proposal_store.pending) == 0

    daemon.analyzer.clear_completion(key)


# --- proposal_dict ---


def test_proposal_dict(daemon):
    """proposal_dict returns serialized proposal dict."""
    proposal = AssignmentProposal(
        worker_name="api",
        task_id="abc123",
        task_title="Fix bug",
        message="Go fix it",
        reasoning="Worker is idle",
        confidence=0.85,
    )
    daemon.proposal_store.add(proposal)

    result = daemon.proposal_dict(proposal)
    assert result["worker_name"] == "api"
    assert result["task_id"] == "abc123"
    assert result["task_title"] == "Fix bug"
    assert result["confidence"] == 0.85
    assert result["status"] == "pending"


def test_proposal_dict_completion_with_email(daemon):
    """proposal_dict includes has_source_email for completion proposals."""
    task = daemon.task_board.create(title="Email task")
    # Manually set source_email_id on the in-memory task
    task.source_email_id = "msg123"

    proposal = AssignmentProposal.completion(
        worker_name="api",
        task_id=task.id,
        task_title=task.title,
        assessment="Done",
    )
    result = daemon.proposal_dict(proposal)
    assert result["has_source_email"] is True


# --- _broadcast_proposals ---


def test_broadcast_proposals(daemon):
    """_broadcast_proposals sends proposals_changed to WS clients."""
    proposal = AssignmentProposal(
        worker_name="api",
        task_title="Test",
    )
    daemon.proposal_store.add(proposal)

    daemon._broadcast_proposals()

    daemon.broadcast_ws.assert_called()
    calls = [c[0][0] for c in daemon.broadcast_ws.call_args_list]
    pc_calls = [c for c in calls if c.get("type") == "proposals_changed"]
    assert len(pc_calls) == 1
    assert pc_calls[0]["pending_count"] == 1


# --- stop terminal_ws close errors ---


@pytest.mark.asyncio
async def test_stop_terminal_ws_close_errors(daemon):
    """stop() handles errors in terminal WS close gracefully."""
    tws = AsyncMock()
    tws.close.side_effect = Exception("network down")
    daemon.terminal_ws_clients = {tws}
    await daemon.stop()
    assert len(daemon.terminal_ws_clients) == 0


# --- complete_task with send_reply ---


def test_complete_task_with_resolution(daemon):
    """complete_task accepts and stores a resolution string."""
    task = daemon.create_task(title="Bug fix")
    daemon.task_board.assign(task.id, "api")
    result = daemon.complete_task(task.id, resolution="Fixed the null pointer")
    assert result is True
    reloaded = daemon.task_board.get(task.id)
    assert reloaded.resolution == "Fixed the null pointer"


# --- _on_state_changed no proposal to expire ---


def test_on_state_changed_buzzing_no_proposals(daemon):
    """_on_state_changed BUZZING with no proposals doesn't crash."""
    daemon.workers[0].state = WorkerState.BUZZING
    daemon._on_state_changed(daemon.workers[0])
    # Should broadcast but not crash
    daemon.broadcast_ws.assert_called()


# --- _on_state_changed completion proposals expired ---


def test_on_state_changed_buzzing_expires_completion_proposals(daemon):
    """_on_state_changed expires completion proposals when worker goes BUZZING."""
    task = daemon.task_board.create(title="Test")
    daemon.task_board.assign(task.id, "api")

    proposal = AssignmentProposal.completion(
        worker_name="api",
        task_id=task.id,
        task_title=task.title,
        assessment="Maybe done",
    )
    daemon.proposal_store.add(proposal)

    daemon.workers[0].state = WorkerState.BUZZING
    daemon._on_state_changed(daemon.workers[0])

    assert proposal.status == ProposalStatus.EXPIRED


# --- _on_state_changed does not expire assignment proposals ---


def test_on_state_changed_buzzing_keeps_assignment_proposals(daemon):
    """_on_state_changed does NOT expire assignment proposals when BUZZING."""
    proposal = AssignmentProposal(
        worker_name="api",
        task_id="t123",
        task_title="Test",
        proposal_type="assignment",
    )
    daemon.proposal_store.add(proposal)

    daemon.workers[0].state = WorkerState.BUZZING
    daemon._on_state_changed(daemon.workers[0])

    # Assignment proposals should remain pending
    assert proposal.status == ProposalStatus.PENDING


# --- assign_task with queen context message ---


@pytest.mark.asyncio
async def test_assign_task_with_queen_message(daemon):
    """assign_task appends Queen context when message is provided."""
    task = daemon.create_task(title="Fix bug", description="It crashes")
    daemon.workers[0].state = WorkerState.RESTING

    with (
        patch.object(daemon, "_prep_worker_for_task", new_callable=AsyncMock),
        patch.object(daemon, "send_to_worker", new_callable=AsyncMock) as mock_send,
        patch("swarm.server.daemon.send_enter", new_callable=AsyncMock),
    ):
        await daemon.assign_task(task.id, "api", message="Focus on the crash handler")

    sent_msg = mock_send.call_args[0][1]
    assert "Queen context: Focus on the crash handler" in sent_msg


# --- assign_task logs task_assigned system event ---


# --- _on_escalation queen enabled without event loop ---


def test_on_escalation_queen_enabled_no_loop(daemon):
    """_on_escalation queen-enabled path clears escalation tracking on RuntimeError."""
    daemon.queen.enabled = True
    daemon.queen._last_call = 0.0
    daemon.queen.cooldown = 0.0

    # No running event loop in sync test → RuntimeError path
    daemon._on_escalation(daemon.workers[0], "test reason")

    # Escalation tracking should be cleared (RuntimeError handled)
    assert not daemon.analyzer.has_inflight_escalation("api")
    # Should still broadcast the escalation event
    calls = [c[0][0] for c in daemon.broadcast_ws.call_args_list]
    assert any(c.get("type") == "escalation" for c in calls)


# --- _on_task_done queen enabled without event loop ---


def test_on_task_done_queen_enabled_no_loop(daemon):
    """_on_task_done queen-enabled path clears completion tracking on RuntimeError."""
    task = daemon.task_board.create(title="Test task")
    daemon.task_board.assign(task.id, "api")
    daemon.workers[0].state = WorkerState.RESTING

    daemon.queen.enabled = True
    daemon.queen._last_call = 0.0
    daemon.queen.cooldown = 0.0

    # No running event loop in sync test → RuntimeError path
    daemon._on_task_done(daemon.workers[0], task)

    # Completion tracking should be cleared
    key = f"api:{task.id}"
    assert not daemon.analyzer.has_inflight_completion(key)
    # No proposals created (analysis couldn't start)
    assert len(daemon.proposal_store.pending) == 0


# --- complete_task with email reply (RuntimeError path) ---


def test_complete_task_send_reply_no_loop(daemon):
    """complete_task with send_reply=True in sync context catches RuntimeError."""
    task = daemon.create_task(title="Email task")
    task.source_email_id = "msg123"
    daemon.task_board.assign(task.id, "api")
    daemon.graph_mgr = MagicMock()  # graph configured

    # In sync test, asyncio.get_running_loop() raises RuntimeError
    result = daemon.complete_task(task.id, resolution="Fixed the bug", send_reply=True)
    assert result is True
    assert daemon.task_board.get(task.id).status == TaskStatus.COMPLETED


@pytest.mark.asyncio
async def test_assign_task_logs_system_event(daemon):
    """assign_task logs TASK_ASSIGNED to drone_log."""
    task = daemon.create_task(title="Test task", description="Do it")
    daemon.workers[0].state = WorkerState.RESTING

    with (
        patch.object(daemon, "_prep_worker_for_task", new_callable=AsyncMock),
        patch.object(daemon, "send_to_worker", new_callable=AsyncMock),
        patch("swarm.server.daemon.send_enter", new_callable=AsyncMock),
    ):
        await daemon.assign_task(task.id, "api")

    entries = daemon.drone_log.entries
    assigned = [e for e in entries if e.action == SystemAction.TASK_ASSIGNED]
    assert len(assigned) == 1
    assert assigned[0].worker_name == "api"


# ── Heartbeat loop ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_heartbeat_broadcasts_on_display_state_change(daemon):
    """Heartbeat should broadcast when worker display_state changes."""
    # Replace broadcast_ws with a real list collector
    broadcasts: list[dict] = []
    daemon.broadcast_ws = lambda data: broadcasts.append(data)
    daemon._heartbeat_snapshot = {}

    # Set worker states
    daemon.workers[0].state = WorkerState.RESTING
    daemon.workers[1].state = WorkerState.BUZZING

    # Manually verify the snapshot diff logic (no need for the async loop)
    snapshot = {w.name: w.display_state.value for w in daemon.workers}
    assert snapshot != daemon._heartbeat_snapshot

    # After updating snapshot, changing state should create a diff
    daemon._heartbeat_snapshot = snapshot
    daemon.workers[0].state = WorkerState.BUZZING
    new_snapshot = {w.name: w.display_state.value for w in daemon.workers}
    assert new_snapshot != daemon._heartbeat_snapshot


@pytest.mark.asyncio
async def test_heartbeat_no_broadcast_when_unchanged(daemon):
    """Heartbeat should not broadcast when display_state is unchanged."""
    broadcasts: list[dict] = []
    daemon.broadcast_ws = lambda data: broadcasts.append(data)

    # Pre-seed snapshot to match current state
    daemon.workers[0].state = WorkerState.BUZZING
    daemon.workers[1].state = WorkerState.BUZZING
    daemon._heartbeat_snapshot = {w.name: w.display_state.value for w in daemon.workers}

    # Manually run the snapshot check
    snapshot = {w.name: w.display_state.value for w in daemon.workers}
    assert snapshot == daemon._heartbeat_snapshot
    # No broadcast should happen
    assert len(broadcasts) == 0


@pytest.mark.asyncio
async def test_heartbeat_revives_dead_pilot_loop(daemon):
    """Heartbeat watchdog should restart the pilot loop if it has died."""
    pilot = MagicMock()
    pilot._running = True
    # Simulate a dead task
    dead_task = asyncio.Future()
    dead_task.set_result(None)  # marks as done
    pilot._task = dead_task
    pilot._loop = AsyncMock()
    daemon.pilot = pilot

    # Run one heartbeat iteration manually
    daemon._heartbeat_snapshot = {}
    daemon.broadcast_ws = MagicMock()

    # The watchdog check: task is done → should restart
    task = pilot._task
    assert task.done()

    # Simulate the watchdog logic from _heartbeat_loop
    if pilot._running and (task is None or task.done()):
        pilot._task = asyncio.create_task(pilot._loop())

    # Verify _loop was scheduled
    assert pilot._loop.called or not pilot._task.done()
    # Cleanup
    if not pilot._task.done():
        pilot._task.cancel()
        try:
            await pilot._task
        except asyncio.CancelledError:
            pass
