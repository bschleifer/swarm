from __future__ import annotations

import pytest
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

from swarm.server.daemon import TaskOperationError, SwarmOperationError
from swarm.server.task_manager import TaskManager
from swarm.tasks.board import TaskBoard
from swarm.tasks.history import TaskHistory, TaskAction
from swarm.drones.log import DroneLog, SystemAction, LogCategory
from swarm.drones.pilot import DronePilot
from swarm.tasks.task import TaskPriority, TaskStatus, TaskType


@pytest.fixture
def mgr():
    board = TaskBoard()
    history = TaskHistory(log_file=Path(tempfile.mktemp(suffix=".jsonl")))
    drone_log = DroneLog()
    pilot = MagicMock(spec=DronePilot)
    return TaskManager(board, history, drone_log, pilot)


def test_require_task_found(mgr):
    task = mgr.create_task("Test Task")
    result = mgr.require_task(task.id)
    assert result == task


def test_require_task_not_found(mgr):
    with pytest.raises(TaskOperationError, match="not found"):
        mgr.require_task("nonexistent")


def test_require_task_wrong_status(mgr):
    task = mgr.create_task("Test Task")
    with pytest.raises(TaskOperationError, match="cannot be modified"):
        mgr.require_task(task.id, {TaskStatus.COMPLETED})


def test_create_task_basic(mgr):
    task = mgr.create_task(
        title="Basic Task",
        description="Description",
        priority=TaskPriority.HIGH,
        task_type=TaskType.BUG,
        tags=["tag1", "tag2"],
        actor="test-actor",
    )
    assert task.title == "Basic Task"
    assert task.description == "Description"
    assert task.priority == TaskPriority.HIGH
    assert task.task_type == TaskType.BUG
    assert task.tags == ["tag1", "tag2"]

    history_entries = mgr.task_history.get_events(task.id)
    assert len(history_entries) == 1
    assert history_entries[0].action == TaskAction.CREATED
    assert history_entries[0].actor == "test-actor"

    log_entries = mgr.drone_log.entries
    task_created = [e for e in log_entries if e.action == SystemAction.TASK_CREATED]
    assert len(task_created) == 1
    assert task_created[0].worker_name == "test-actor"
    assert task_created[0].detail == "Basic Task"
    assert task_created[0].category == LogCategory.TASK


@pytest.mark.asyncio
async def test_create_task_smart_with_title(mgr):
    task = await mgr.create_task_smart(
        title="Smart Task",
        description="Some description",
        priority=TaskPriority.LOW,
        actor="smart-actor",
    )
    assert task.title == "Smart Task"
    assert task.description == "Some description"
    assert task.priority == TaskPriority.LOW
    assert task.task_type in list(TaskType)


@pytest.mark.asyncio
async def test_create_task_smart_without_title(mgr):
    with patch("swarm.server.task_manager.smart_title") as mock_smart_title:
        mock_smart_title.return_value = "Generated Title"
        task = await mgr.create_task_smart(
            description="Description without title",
            actor="smart-actor",
        )
        assert task.title == "Generated Title"
        mock_smart_title.assert_called_once_with("Description without title")


@pytest.mark.asyncio
async def test_create_task_smart_no_title_no_description(mgr):
    with pytest.raises(SwarmOperationError, match="title or description required"):
        await mgr.create_task_smart()


@pytest.mark.asyncio
async def test_create_task_smart_explicit_type(mgr):
    task = await mgr.create_task_smart(
        title="Explicit Type Task",
        task_type=TaskType.FEATURE,
    )
    assert task.task_type == TaskType.FEATURE


@pytest.mark.asyncio
async def test_create_task_smart_auto_classify_type(mgr):
    with patch("swarm.server.task_manager.auto_classify_type") as mock_classify:
        mock_classify.return_value = TaskType.BUG
        task = await mgr.create_task_smart(
            title="Fix something broken",
            description="It's broken",
        )
        assert task.task_type == TaskType.BUG
        mock_classify.assert_called_once_with("Fix something broken", "It's broken")


def test_unassign_task_success(mgr):
    task = mgr.create_task("Assigned Task")
    mgr.task_board.assign(task.id, "worker1")

    result = mgr.unassign_task(task.id, actor="test-actor")

    assert result is True
    assert task.status == TaskStatus.PENDING
    mgr._pilot.clear_proposed_completion.assert_called_once_with(task.id)

    history_entries = mgr.task_history.get_events(task.id)
    edited = [e for e in history_entries if e.action == TaskAction.EDITED]
    assert len(edited) == 1
    assert edited[0].detail == "unassigned"


def test_unassign_task_wrong_status(mgr):
    task = mgr.create_task("Pending Task")
    with pytest.raises(TaskOperationError, match="cannot be modified"):
        mgr.unassign_task(task.id)


def test_reopen_task_from_completed(mgr):
    task = mgr.create_task("Completed Task")
    mgr.task_board.assign(task.id, "worker1")
    mgr.task_board.complete(task.id)

    result = mgr.reopen_task(task.id, actor="test-actor")

    assert result is True
    assert task.status == TaskStatus.PENDING
    mgr._pilot.clear_proposed_completion.assert_called_once_with(task.id)

    history_entries = mgr.task_history.get_events(task.id)
    reopened = [e for e in history_entries if e.action == TaskAction.REOPENED]
    assert len(reopened) == 1
    assert reopened[0].actor == "test-actor"


def test_reopen_task_from_failed(mgr):
    task = mgr.create_task("Failed Task")
    mgr.task_board.fail(task.id)

    result = mgr.reopen_task(task.id, actor="test-actor")

    assert result is True
    assert task.status == TaskStatus.PENDING


def test_reopen_task_wrong_status(mgr):
    task = mgr.create_task("Pending Task")
    with pytest.raises(TaskOperationError, match="cannot be modified"):
        mgr.reopen_task(task.id)


def test_fail_task_success(mgr):
    task = mgr.create_task("Task to Fail")

    result = mgr.fail_task(task.id, actor="test-actor")

    assert result is True
    assert task.status == TaskStatus.FAILED

    history_entries = mgr.task_history.get_events(task.id)
    failed = [e for e in history_entries if e.action == TaskAction.FAILED]
    assert len(failed) == 1
    assert failed[0].actor == "test-actor"

    log_entries = mgr.drone_log.entries
    task_failed = [e for e in log_entries if e.action == SystemAction.TASK_FAILED]
    assert len(task_failed) == 1
    assert task_failed[0].detail == "Task to Fail"
    assert task_failed[0].category == LogCategory.TASK
    assert task_failed[0].is_notification is True


def test_fail_task_not_found(mgr):
    with pytest.raises(TaskOperationError, match="not found"):
        mgr.fail_task("nonexistent")


def test_remove_task_success(mgr):
    task = mgr.create_task("Task to Remove")

    result = mgr.remove_task(task.id, actor="test-actor")

    assert result is True
    assert mgr.task_board.get(task.id) is None

    history_entries = mgr.task_history.get_events(task.id)
    removed = [e for e in history_entries if e.action == TaskAction.REMOVED]
    assert len(removed) == 1

    log_entries = mgr.drone_log.entries
    task_removed = [e for e in log_entries if e.action == SystemAction.TASK_REMOVED]
    assert len(task_removed) == 1
    assert task_removed[0].detail == "Task to Remove"
    assert task_removed[0].category == LogCategory.TASK


def test_remove_task_not_found(mgr):
    with pytest.raises(TaskOperationError, match="not found"):
        mgr.remove_task("nonexistent")


def test_edit_task_success(mgr):
    task = mgr.create_task("Original Title", description="Original description")

    result = mgr.edit_task(
        task.id,
        title="Updated Title",
        description="Updated description",
        priority=TaskPriority.HIGH,
        task_type=TaskType.FEATURE,
        tags=["new-tag"],
        actor="test-actor",
    )

    assert result is True
    assert task.title == "Updated Title"
    assert task.description == "Updated description"
    assert task.priority == TaskPriority.HIGH
    assert task.task_type == TaskType.FEATURE
    assert task.tags == ["new-tag"]

    history_entries = mgr.task_history.get_events(task.id)
    edited = [e for e in history_entries if e.action == TaskAction.EDITED]
    assert len(edited) == 1
    assert edited[0].actor == "test-actor"


def test_edit_task_partial_update(mgr):
    task = mgr.create_task("Original Title", description="Original description")

    result = mgr.edit_task(task.id, title="New Title")

    assert result is True
    assert task.title == "New Title"
    assert task.description == "Original description"


def test_edit_task_not_found(mgr):
    with pytest.raises(TaskOperationError, match="not found"):
        mgr.edit_task("nonexistent", title="New Title")


def test_manager_without_pilot(mgr):
    mgr_no_pilot = TaskManager(mgr.task_board, mgr.task_history, mgr.drone_log, pilot=None)

    task = mgr_no_pilot.create_task("Test Task")
    mgr_no_pilot.task_board.assign(task.id, "worker1")

    result = mgr_no_pilot.unassign_task(task.id)
    assert result is True


def test_unassign_task_no_pilot_no_crash(mgr):
    mgr._pilot = None
    task = mgr.create_task("Test Task")
    mgr.task_board.assign(task.id, "worker1")

    result = mgr.unassign_task(task.id)
    assert result is True


def test_reopen_task_no_pilot_no_crash(mgr):
    mgr._pilot = None
    task = mgr.create_task("Test Task")
    mgr.task_board.assign(task.id, "worker1")
    mgr.task_board.complete(task.id)

    result = mgr.reopen_task(task.id)
    assert result is True
