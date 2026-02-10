"""Tests for tasks/task.py, tasks/board.py, and tasks/store.py."""

import json
import tempfile
from pathlib import Path

from swarm.tasks.board import TaskBoard
from swarm.tasks.store import FileTaskStore
from swarm.tasks.task import SwarmTask, TaskPriority, TaskStatus


class TestSwarmTask:
    def test_defaults(self):
        t = SwarmTask(title="Fix bug")
        assert t.status == TaskStatus.PENDING
        assert t.priority == TaskPriority.NORMAL
        assert t.assigned_worker is None
        assert t.is_available is True
        assert len(t.id) == 12

    def test_default_empty_attachments(self):
        t = SwarmTask(title="Test")
        assert t.attachments == []

    def test_assign(self):
        t = SwarmTask(title="Fix bug")
        t.assign("api")
        assert t.status == TaskStatus.ASSIGNED
        assert t.assigned_worker == "api"
        assert t.is_available is False

    def test_lifecycle(self):
        t = SwarmTask(title="Fix bug")
        t.assign("api")
        t.start()
        assert t.status == TaskStatus.IN_PROGRESS
        t.complete()
        assert t.status == TaskStatus.COMPLETED
        assert t.completed_at is not None

    def test_fail(self):
        t = SwarmTask(title="Fix bug")
        t.assign("api")
        t.start()
        t.fail()
        assert t.status == TaskStatus.FAILED


class TestTaskBoard:
    def test_create_and_get(self):
        board = TaskBoard()
        task = board.create("Fix bug", description="Fix the login bug")
        assert board.get(task.id) is not None
        assert board.get(task.id).title == "Fix bug"

    def test_available_tasks(self):
        board = TaskBoard()
        t1 = board.create("Task 1")
        board.create("Task 2")
        assert len(board.available_tasks) == 2
        board.assign(t1.id, "api")
        assert len(board.available_tasks) == 1

    def test_dependency_blocks_availability(self):
        board = TaskBoard()
        t1 = board.create("Build API")
        t2 = board.create("Build frontend", depends_on=[t1.id])
        avail = board.available_tasks
        assert t1 in avail
        assert t2 not in avail  # blocked by t1

    def test_dependency_unblocks_on_complete(self):
        board = TaskBoard()
        t1 = board.create("Build API")
        t2 = board.create("Build frontend", depends_on=[t1.id])
        board.assign(t1.id, "worker-1")
        board.complete(t1.id)
        avail = board.available_tasks
        assert t2 in avail

    def test_priority_ordering(self):
        board = TaskBoard()
        board.create("Low", priority=TaskPriority.LOW)
        board.create("Urgent", priority=TaskPriority.URGENT)
        board.create("Normal", priority=TaskPriority.NORMAL)
        tasks = board.all_tasks
        assert tasks[0].priority == TaskPriority.URGENT
        assert tasks[-1].priority == TaskPriority.LOW

    def test_tasks_for_worker(self):
        board = TaskBoard()
        t1 = board.create("Task 1")
        t2 = board.create("Task 2")
        board.assign(t1.id, "api")
        board.assign(t2.id, "web")
        assert len(board.tasks_for_worker("api")) == 1
        assert board.tasks_for_worker("api")[0].id == t1.id

    def test_remove(self):
        board = TaskBoard()
        t = board.create("Temp task")
        assert board.remove(t.id) is True
        assert board.get(t.id) is None

    def test_summary(self):
        board = TaskBoard()
        board.create("A")
        t2 = board.create("B")
        board.assign(t2.id, "api")
        s = board.summary()
        assert "2 tasks" in s
        assert "1 pending" in s
        assert "1 active" in s

    def test_on_change_callback(self):
        board = TaskBoard()
        changes = []
        board.on_change(lambda: changes.append(1))
        board.create("Test")
        assert len(changes) == 1
        board.assign(board.all_tasks[0].id, "api")
        assert len(changes) == 2

    def test_active_tasks(self):
        board = TaskBoard()
        t1 = board.create("A")
        board.create("B")
        board.assign(t1.id, "api")
        assert len(board.active_tasks) == 1
        board.complete(t1.id)
        assert len(board.active_tasks) == 0

    def test_update_title(self):
        board = TaskBoard()
        t = board.create("Old title")
        assert board.update(t.id, title="New title")
        assert board.get(t.id).title == "New title"

    def test_update_description(self):
        board = TaskBoard()
        t = board.create("Task", description="old desc")
        assert board.update(t.id, description="new desc")
        assert board.get(t.id).description == "new desc"

    def test_update_priority(self):
        board = TaskBoard()
        t = board.create("Task")
        assert board.update(t.id, priority=TaskPriority.URGENT)
        assert board.get(t.id).priority == TaskPriority.URGENT

    def test_update_tags(self):
        board = TaskBoard()
        t = board.create("Task", tags=["a"])
        assert board.update(t.id, tags=["b", "c"])
        assert board.get(t.id).tags == ["b", "c"]

    def test_update_attachments(self):
        board = TaskBoard()
        t = board.create("Task")
        assert board.update(t.id, attachments=["/tmp/img.png"])
        assert board.get(t.id).attachments == ["/tmp/img.png"]

    def test_update_nonexistent(self):
        board = TaskBoard()
        assert board.update("nonexistent", title="Nope") is False

    def test_update_fires_change(self):
        board = TaskBoard()
        changes = []
        board.on_change(lambda: changes.append(1))
        t = board.create("Task")
        changes.clear()
        board.update(t.id, title="Updated")
        assert len(changes) == 1

    def test_update_partial(self):
        """Updating one field should not affect others."""
        board = TaskBoard()
        t = board.create("Task", description="desc", priority=TaskPriority.HIGH, tags=["x"])
        board.update(t.id, title="New Title")
        updated = board.get(t.id)
        assert updated.title == "New Title"
        assert updated.description == "desc"
        assert updated.priority == TaskPriority.HIGH
        assert updated.tags == ["x"]

    def test_create_with_attachments(self):
        board = TaskBoard()
        t = board.create("Task", attachments=["/tmp/a.png", "/tmp/b.png"])
        assert t.attachments == ["/tmp/a.png", "/tmp/b.png"]


class TestTaskStore:
    def test_store_backward_compat(self):
        """Loading old JSON without attachments field should default to empty list."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(
                [
                    {
                        "id": "abc123",
                        "title": "Old task",
                        "status": "pending",
                    }
                ],
                f,
            )
            f.flush()
            store = FileTaskStore(path=Path(f.name))
            tasks = store.load()
            assert "abc123" in tasks
            assert tasks["abc123"].attachments == []
            assert tasks["abc123"].tags == []

    def test_roundtrip_with_attachments(self):
        """Save and load tasks with attachments should roundtrip correctly."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "tasks.json"
            store = FileTaskStore(path=path)
            board = TaskBoard(store=store)
            t = board.create("Task", attachments=["/tmp/img.png"])
            board.update(t.id, tags=["tag1"])

            # Reload from disk
            store2 = FileTaskStore(path=path)
            loaded = store2.load()
            assert t.id in loaded
            assert loaded[t.id].attachments == ["/tmp/img.png"]
            assert loaded[t.id].tags == ["tag1"]
