"""Tests for tasks/store.py — file-based task persistence."""

import pytest

from swarm.tasks.board import TaskBoard
from swarm.tasks.store import FileTaskStore
from swarm.tasks.task import SwarmTask, TaskPriority, TaskStatus


@pytest.fixture
def store(tmp_path):
    return FileTaskStore(path=tmp_path / "tasks.json")


class TestFileTaskStore:
    def test_save_and_load(self, store):
        """Tasks should survive save/load cycle."""
        tasks = {
            "abc": SwarmTask(id="abc", title="Fix bug", priority=TaskPriority.HIGH),
            "def": SwarmTask(id="def", title="Add feature"),
        }
        store.save(tasks)
        loaded = store.load()
        assert len(loaded) == 2
        assert loaded["abc"].title == "Fix bug"
        assert loaded["abc"].priority == TaskPriority.HIGH

    def test_load_missing_file(self, tmp_path):
        """load() should return empty dict if file doesn't exist."""
        s = FileTaskStore(path=tmp_path / "nonexistent.json")
        assert s.load() == {}

    def test_load_corrupt_file(self, tmp_path):
        """load() should return empty dict on corrupt JSON."""
        path = tmp_path / "tasks.json"
        path.write_text("not valid json{{{")
        s = FileTaskStore(path=path)
        assert s.load() == {}

    def test_preserves_all_fields(self, store):
        """All task fields should survive persistence."""
        task = SwarmTask(
            id="test123",
            title="Test task",
            description="Detailed description",
            status=TaskStatus.ASSIGNED,
            priority=TaskPriority.URGENT,
            assigned_worker="api",
            depends_on=["dep1", "dep2"],
            tags=["bug", "critical"],
        )
        store.save({"test123": task})
        loaded = store.load()
        t = loaded["test123"]
        assert t.title == "Test task"
        assert t.description == "Detailed description"
        assert t.status == TaskStatus.ASSIGNED
        assert t.priority == TaskPriority.URGENT
        assert t.assigned_worker == "api"
        assert t.depends_on == ["dep1", "dep2"]
        assert t.tags == ["bug", "critical"]


class TestTaskBoardWithStore:
    def test_board_auto_saves(self, store):
        """Board mutations should auto-save."""
        board = TaskBoard(store=store)
        task = board.create("Fix bug")
        # Load a fresh board and verify
        loaded = store.load()
        assert task.id in loaded

    def test_board_loads_on_init(self, store):
        """Board should load existing tasks on init."""
        # Save some tasks
        tasks = {"abc": SwarmTask(id="abc", title="Existing task")}
        store.save(tasks)

        board = TaskBoard(store=store)
        assert board.get("abc") is not None
        assert board.get("abc").title == "Existing task"

    def test_board_survives_restart(self, store):
        """Tasks should survive board recreation (simulating restart)."""
        board1 = TaskBoard(store=store)
        t = board1.create("Persistent task", priority=TaskPriority.HIGH)
        board1.assign(t.id, "api")

        # "Restart" — create new board with same store
        board2 = TaskBoard(store=store)
        restored = board2.get(t.id)
        assert restored is not None
        assert restored.title == "Persistent task"
        assert restored.priority == TaskPriority.HIGH
        assert restored.assigned_worker == "api"
        assert restored.status == TaskStatus.ASSIGNED
