"""Integration tests — full flow with mocked tmux."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from swarm.drones.log import DroneAction, DroneLog
from swarm.drones.pilot import DronePilot
from swarm.config import DroneConfig
from swarm.tasks.board import TaskBoard
from swarm.tasks.task import TaskPriority, TaskStatus
from swarm.worker.worker import Worker, WorkerState


@pytest.fixture
def mock_tmux(monkeypatch):
    """Mock all tmux operations for integration testing."""
    monkeypatch.setattr("swarm.drones.pilot.pane_exists", AsyncMock(return_value=True))
    monkeypatch.setattr("swarm.drones.pilot.get_pane_command", AsyncMock(return_value="claude"))
    monkeypatch.setattr(
        "swarm.drones.pilot.capture_pane", AsyncMock(return_value="esc to interrupt"),
    )
    monkeypatch.setattr("swarm.drones.pilot.send_enter", AsyncMock())
    monkeypatch.setattr("swarm.drones.pilot.send_keys", AsyncMock())
    monkeypatch.setattr("swarm.drones.pilot.set_pane_option", AsyncMock())
    monkeypatch.setattr("swarm.drones.pilot.discover_workers", AsyncMock(return_value=[]))
    monkeypatch.setattr("swarm.drones.pilot.update_window_names", AsyncMock())
    monkeypatch.setattr("swarm.drones.pilot.set_terminal_title", AsyncMock())
    monkeypatch.setattr("swarm.drones.pilot.revive_worker", AsyncMock())


@pytest.mark.asyncio
async def test_full_poll_cycle(mock_tmux):
    """Test a complete poll cycle: workers detected, states classified, actions taken."""
    workers = [
        Worker(name="api", path="/tmp/api", pane_id="%0"),
        Worker(name="web", path="/tmp/web", pane_id="%1"),
    ]
    log = DroneLog()
    board = TaskBoard()
    pilot = DronePilot(
        workers, log, interval=1.0, session_name="test",
        drone_config=DroneConfig(), task_board=board,
    )
    pilot.enabled = True

    # Run a poll cycle — both workers should be BUZZING (default content = "esc to interrupt")
    await pilot.poll_once()
    assert len(log.entries) == 0  # No actions needed for BUZZING workers


@pytest.mark.asyncio
async def test_stung_to_revive_to_buzzing(mock_tmux, monkeypatch):
    """Test lifecycle: STUNG → revive → BUZZING."""
    workers = [Worker(name="api", path="/tmp/api", pane_id="%0")]
    log = DroneLog()
    pilot = DronePilot(workers, log, interval=1.0, session_name="test",
                      drone_config=DroneConfig())
    pilot.enabled = True

    # Phase 1: Worker exits (STUNG)
    monkeypatch.setattr("swarm.drones.pilot.get_pane_command", AsyncMock(return_value="bash"))
    monkeypatch.setattr("swarm.drones.pilot.capture_pane", AsyncMock(return_value="$ "))

    await pilot.poll_once()
    assert workers[0].state == WorkerState.STUNG
    assert any(e.action == DroneAction.REVIVED for e in log.entries)

    # Phase 2: Worker comes back (BUZZING)
    monkeypatch.setattr("swarm.drones.pilot.get_pane_command", AsyncMock(return_value="claude"))
    monkeypatch.setattr(
        "swarm.drones.pilot.capture_pane", AsyncMock(return_value="esc to interrupt"),
    )

    await pilot.poll_once()
    assert workers[0].state == WorkerState.BUZZING
    assert workers[0].revive_count == 0  # Reset on BUZZING transition


@pytest.mark.asyncio
async def test_task_lifecycle(mock_tmux):
    """Test task lifecycle: create → assign → complete."""
    board = TaskBoard()

    # Create
    task = board.create("Fix API bug", priority=TaskPriority.HIGH)
    assert task.status == TaskStatus.PENDING
    assert len(board.available_tasks) == 1

    # Assign
    board.assign(task.id, "api")
    assert task.status == TaskStatus.ASSIGNED
    assert len(board.available_tasks) == 0
    assert len(board.active_tasks) == 1

    # Complete
    board.complete(task.id)
    assert task.status == TaskStatus.COMPLETED
    assert len(board.active_tasks) == 0


@pytest.mark.asyncio
async def test_task_dependency_flow(mock_tmux):
    """Test that dependent tasks become available when dependencies complete."""
    board = TaskBoard()

    t1 = board.create("Build API")
    t2 = board.create("Build frontend", depends_on=[t1.id])
    t3 = board.create("Run tests", depends_on=[t1.id, t2.id])

    # Only t1 is available initially
    available = board.available_tasks
    assert t1 in available
    assert t2 not in available
    assert t3 not in available

    # Complete t1 → t2 becomes available
    board.complete(t1.id)
    available = board.available_tasks
    assert t2 in available
    assert t3 not in available

    # Complete t2 → t3 becomes available
    board.complete(t2.id)
    available = board.available_tasks
    assert t3 in available


@pytest.mark.asyncio
async def test_dead_worker_unassigns_tasks():
    """When a worker dies, its tasks should be unassigned."""
    board = TaskBoard()
    task = board.create("Fix bug")
    board.assign(task.id, "api")
    assert task.assigned_worker == "api"

    board.unassign_worker("api")
    assert task.status == TaskStatus.PENDING
    assert task.assigned_worker is None


@pytest.mark.asyncio
async def test_worker_state_change_callbacks(mock_tmux, monkeypatch):
    """State change callbacks should fire correctly."""
    workers = [Worker(name="api", path="/tmp/api", pane_id="%0")]
    log = DroneLog()
    pilot = DronePilot(workers, log, interval=1.0, session_name="test",
                      drone_config=DroneConfig())

    state_changes = []
    pilot.on_state_changed(lambda w: state_changes.append((w.name, w.state)))

    # Make worker STUNG
    monkeypatch.setattr("swarm.drones.pilot.get_pane_command", AsyncMock(return_value="bash"))
    monkeypatch.setattr("swarm.drones.pilot.capture_pane", AsyncMock(return_value="$ "))

    await pilot.poll_once()
    assert len(state_changes) == 1
    assert state_changes[0] == ("api", WorkerState.STUNG)


@pytest.mark.asyncio
async def test_cannot_complete_failed_task():
    """Cannot complete a task that's already failed."""
    board = TaskBoard()
    task = board.create("Doomed task")
    board.assign(task.id, "api")
    board.fail(task.id)
    assert task.status == TaskStatus.FAILED

    result = board.complete(task.id)
    assert result is False
    assert task.status == TaskStatus.FAILED
