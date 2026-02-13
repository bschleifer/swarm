"""Tests for server/daemon.py — daemon operation methods."""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from swarm.config import HiveConfig, QueenConfig, WorkerConfig
from swarm.drones.log import DroneLog
from swarm.drones.pilot import DronePilot
from swarm.queen.queen import Queen
from swarm.server.daemon import (
    SwarmDaemon,
    SwarmOperationError,
    TaskOperationError,
    WorkerNotFoundError,
)
from swarm.server.analyzer import QueenAnalyzer
from swarm.server.proposals import ProposalManager
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
    d.start_time = 0.0
    d._broadcast_ws = MagicMock()
    d.graph_mgr = None
    d._config_mtime = 0.0
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
    daemon._broadcast_ws.assert_called()
    call_data = daemon._broadcast_ws.call_args[0][0]
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
    daemon._broadcast_ws.assert_called()


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
    daemon._broadcast_ws.assert_called()


# --- launch_workers ---


@pytest.mark.asyncio
async def test_launch_workers(daemon):
    launched = [Worker(name="new", path="/tmp/new", pane_id="%5")]
    with patch("swarm.worker.manager.launch_hive", new_callable=AsyncMock, return_value=launched):
        result = await daemon.launch_workers([WorkerConfig("new", "/tmp/new")])
    assert len(result) == 1
    assert result[0].name == "new"
    # Workers should be extended
    assert any(w.name == "new" for w in daemon.workers)
    daemon._broadcast_ws.assert_called()


@pytest.mark.asyncio
async def test_launch_workers_updates_pilot(daemon):
    launched = [Worker(name="new", path="/tmp/new", pane_id="%5")]
    with patch("swarm.worker.manager.launch_hive", new_callable=AsyncMock, return_value=launched):
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
    daemon._broadcast_ws.assert_called()


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
    daemon._broadcast_ws.assert_called()
    call_data = daemon._broadcast_ws.call_args[0][0]
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

    with patch("swarm.server.daemon.load_config") as mock_load:
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
    d._broadcast_ws = MagicMock()
    d._config_mtime = 0.0

    # Wire up on_change like __init__ does
    d._wire_task_board()

    # Now create a task — should trigger broadcast
    d.task_board.create(title="Test")
    d._broadcast_ws.assert_called_with({"type": "tasks_changed"})


# --- _hot_apply_config ---


def test_hot_apply_config(daemon):
    """_hot_apply_config updates pilot, queen, and notification bus."""
    from swarm.config import DroneConfig

    daemon.config.drones = DroneConfig(poll_interval=99.0)
    daemon._hot_apply_config()
    assert daemon.pilot.drone_config.poll_interval == 99.0
    assert daemon.pilot._base_interval == 99.0


def test_hot_apply_config_no_pilot(daemon):
    """_hot_apply_config doesn't crash without pilot."""
    daemon.pilot = None
    daemon._hot_apply_config()  # should not raise


# --- save_config ---


def test_save_config(daemon, tmp_path, monkeypatch):
    cfg_file = tmp_path / "swarm.yaml"
    cfg_file.write_text("session_name: test\n")
    daemon.config.source_path = str(cfg_file)

    monkeypatch.setattr("swarm.server.daemon.save_config", MagicMock())
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
    with patch("swarm.server.daemon.send_enter", new_callable=AsyncMock) as mock_enter:
        count = await daemon.continue_all()
    assert count == 1
    mock_enter.assert_called_once_with("%0")


@pytest.mark.asyncio
async def test_continue_all_none_resting(daemon):
    daemon.workers[0].state = WorkerState.BUZZING
    daemon.workers[1].state = WorkerState.BUZZING
    with patch("swarm.server.daemon.send_enter", new_callable=AsyncMock) as mock_enter:
        count = await daemon.continue_all()
    assert count == 0
    mock_enter.assert_not_called()


# --- send_all ---


@pytest.mark.asyncio
async def test_send_all(daemon):
    with patch("swarm.server.daemon.send_keys", new_callable=AsyncMock) as mock_keys:
        count = await daemon.send_all("hello")
    assert count == 2
    assert mock_keys.call_count == 2


# --- send_group ---


@pytest.mark.asyncio
async def test_send_group(daemon):
    from swarm.config import GroupConfig

    daemon.config.workers = [WorkerConfig("api", "/tmp/api"), WorkerConfig("web", "/tmp/web")]
    daemon.config.groups = [GroupConfig(name="backend", workers=["api"])]
    with patch("swarm.server.daemon.send_keys", new_callable=AsyncMock) as mock_keys:
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
    with patch("swarm.server.daemon.capture_pane", new_callable=AsyncMock, return_value="output"):
        ctx = await daemon.gather_hive_context()
    assert isinstance(ctx, str)
    assert "api" in ctx


# --- analyze_worker ---


@pytest.mark.asyncio
async def test_analyze_worker(daemon, monkeypatch):
    monkeypatch.setattr(
        daemon.queen, "analyze_worker", AsyncMock(return_value={"action": "continue"})
    )
    with patch("swarm.server.daemon.capture_pane", new_callable=AsyncMock, return_value="output"):
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
    with patch("swarm.server.daemon.capture_pane", new_callable=AsyncMock, return_value="output"):
        result = await daemon.coordinate_hive()
    assert result["plan"] == "done"


# --- launch_workers inits pilot if none ---


@pytest.mark.asyncio
async def test_launch_workers_inits_pilot(daemon, monkeypatch):
    daemon.pilot = None
    monkeypatch.setattr("swarm.queen.queen.load_session", lambda _: None)
    monkeypatch.setattr("swarm.queen.queen.save_session", lambda *a: None)
    monkeypatch.setattr(DronePilot, "start", lambda self: None)
    launched = [Worker(name="new", path="/tmp/new", pane_id="%5")]
    with patch("swarm.worker.manager.launch_hive", new_callable=AsyncMock, return_value=launched):
        await daemon.launch_workers([WorkerConfig("new", "/tmp/new")])
    assert daemon.pilot is not None


# --- discover ---


@pytest.mark.asyncio
async def test_discover(daemon):
    mock_workers = [Worker(name="found", path="/tmp/found", pane_id="%9")]
    with patch(
        "swarm.server.daemon.discover_workers",
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
    with patch("swarm.server.daemon.send_keys", new_callable=AsyncMock) as mock_keys:
        await daemon.send_to_worker("api", "hello")
    mock_keys.assert_called_once_with("%0", "hello")


@pytest.mark.asyncio
async def test_send_to_worker_not_found(daemon):
    with pytest.raises(WorkerNotFoundError):
        await daemon.send_to_worker("nonexistent", "hello")


@pytest.mark.asyncio
async def test_continue_worker(daemon):
    with patch("swarm.server.daemon.send_enter", new_callable=AsyncMock) as mock_enter:
        await daemon.continue_worker("api")
    mock_enter.assert_called_once_with("%0")


@pytest.mark.asyncio
async def test_continue_worker_not_found(daemon):
    with pytest.raises(WorkerNotFoundError):
        await daemon.continue_worker("nonexistent")


@pytest.mark.asyncio
async def test_interrupt_worker(daemon):
    with patch("swarm.server.daemon.send_interrupt", new_callable=AsyncMock) as mock_int:
        await daemon.interrupt_worker("api")
    mock_int.assert_called_once_with("%0")


@pytest.mark.asyncio
async def test_interrupt_worker_not_found(daemon):
    with pytest.raises(WorkerNotFoundError):
        await daemon.interrupt_worker("nonexistent")


@pytest.mark.asyncio
async def test_escape_worker(daemon):
    with patch("swarm.server.daemon.send_escape", new_callable=AsyncMock) as mock_esc:
        await daemon.escape_worker("api")
    mock_esc.assert_called_once_with("%0")


@pytest.mark.asyncio
async def test_escape_worker_not_found(daemon):
    with pytest.raises(WorkerNotFoundError):
        await daemon.escape_worker("nonexistent")


@pytest.mark.asyncio
async def test_capture_worker_output(daemon):
    with patch(
        "swarm.server.daemon.capture_pane", new_callable=AsyncMock, return_value="pane content"
    ) as mock_cap:
        result = await daemon.capture_worker_output("api")
    assert result == "pane content"
    mock_cap.assert_called_once_with("%0", lines=80)


@pytest.mark.asyncio
async def test_capture_worker_output_custom_lines(daemon):
    with patch(
        "swarm.server.daemon.capture_pane", new_callable=AsyncMock, return_value="content"
    ) as mock_cap:
        await daemon.capture_worker_output("api", lines=20)
    mock_cap.assert_called_once_with("%0", lines=20)


@pytest.mark.asyncio
async def test_capture_worker_output_not_found(daemon):
    with pytest.raises(WorkerNotFoundError):
        await daemon.capture_worker_output("nonexistent")


# --- _broadcast_ws safety ---


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
        patch("swarm.server.daemon.capture_pane", new_callable=AsyncMock, return_value="output"),
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
        patch("swarm.server.daemon.capture_pane", new_callable=AsyncMock, return_value="output"),
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
    with patch("swarm.server.daemon.capture_pane", new_callable=AsyncMock, return_value="output"):
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
    with patch("swarm.server.daemon.capture_pane", new_callable=AsyncMock, return_value="output"):
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
        patch("swarm.server.daemon.capture_pane", new_callable=AsyncMock, return_value="output"),
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
async def test_broadcast_ws_dead_client(monkeypatch):
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

    # Create a mock WS that is "closed"
    dead_ws = MagicMock()
    dead_ws.closed = True
    d.ws_clients = {dead_ws}

    # Use real _broadcast_ws (not mocked)
    SwarmDaemon._broadcast_ws(d, {"type": "test"})

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

    with patch("swarm.server.daemon.send_enter", new_callable=AsyncMock):
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
    with patch("swarm.server.daemon.send_enter", new_callable=AsyncMock):
        await daemon.continue_all()

    entries = daemon.drone_log.entries
    ops = [e for e in entries if e.action == SystemAction.OPERATOR]
    assert len(ops) == 1
    assert ops[0].worker_name == "all"
    assert "2 worker(s)" in ops[0].detail
