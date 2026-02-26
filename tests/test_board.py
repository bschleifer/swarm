"""Direct tests for TaskBoard — covers lifecycle, deps, locking, scrub."""

from __future__ import annotations

import threading

import pytest

from swarm.tasks.board import TaskBoard
from swarm.tasks.task import SwarmTask, TaskPriority, TaskStatus, TaskType

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_board() -> TaskBoard:
    """Create a board with no persistence store."""
    return TaskBoard()


def _quick_task(title: str = "test", **kwargs: object) -> SwarmTask:
    return SwarmTask(title=title, **kwargs)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Basic CRUD
# ---------------------------------------------------------------------------


class TestAddAndGet:
    def test_add_assigns_number(self):
        board = _make_board()
        task = board.add(_quick_task("first"))
        assert task.number == 1

    def test_sequential_numbers(self):
        board = _make_board()
        t1 = board.add(_quick_task("a"))
        t2 = board.add(_quick_task("b"))
        assert t1.number == 1
        assert t2.number == 2

    def test_get_returns_task(self):
        board = _make_board()
        t = board.add(_quick_task("x"))
        assert board.get(t.id) is t

    def test_get_missing_returns_none(self):
        board = _make_board()
        assert board.get("nonexistent") is None


class TestCreate:
    def test_create_returns_task(self):
        board = _make_board()
        t = board.create("do the thing")
        assert t.title == "do the thing"
        assert t.status == TaskStatus.PENDING

    def test_create_with_priority(self):
        board = _make_board()
        t = board.create("urgent", priority=TaskPriority.URGENT)
        assert t.priority == TaskPriority.URGENT

    def test_create_with_type(self):
        board = _make_board()
        t = board.create("fix bug", task_type=TaskType.BUG)
        assert t.task_type == TaskType.BUG


class TestRemove:
    def test_remove_existing(self):
        board = _make_board()
        t = board.add(_quick_task("rm me"))
        assert board.remove(t.id) is True
        assert board.get(t.id) is None

    def test_remove_missing(self):
        board = _make_board()
        assert board.remove("nope") is False

    def test_remove_tasks_bulk(self):
        board = _make_board()
        t1 = board.add(_quick_task("a"))
        t2 = board.add(_quick_task("b"))
        board.add(_quick_task("c"))
        removed = board.remove_tasks({t1.id, t2.id})
        assert removed == 2
        assert len(board.all_tasks) == 1


# ---------------------------------------------------------------------------
# Lifecycle transitions
# ---------------------------------------------------------------------------


class TestLifecycle:
    def test_assign_pending_task(self):
        board = _make_board()
        t = board.create("work")
        assert board.assign(t.id, "alice") is True
        assert t.status == TaskStatus.ASSIGNED
        assert t.assigned_worker == "alice"

    def test_assign_already_assigned_fails(self):
        board = _make_board()
        t = board.create("work")
        board.assign(t.id, "alice")
        assert board.assign(t.id, "bob") is False

    def test_complete_assigned_task(self):
        board = _make_board()
        t = board.create("work")
        board.assign(t.id, "alice")
        assert board.complete(t.id, resolution="done") is True
        assert t.status == TaskStatus.COMPLETED
        assert t.resolution == "done"

    def test_complete_pending_fails(self):
        board = _make_board()
        t = board.create("work")
        assert board.complete(t.id) is False

    def test_fail_assigned_task(self):
        board = _make_board()
        t = board.create("work")
        board.assign(t.id, "alice")
        assert board.fail(t.id) is True
        assert t.status == TaskStatus.FAILED

    def test_reopen_completed(self):
        board = _make_board()
        t = board.create("work")
        board.assign(t.id, "alice")
        board.complete(t.id)
        assert board.reopen(t.id) is True
        assert t.status == TaskStatus.PENDING
        assert t.assigned_worker is None

    def test_reopen_pending_fails(self):
        board = _make_board()
        t = board.create("work")
        assert board.reopen(t.id) is False

    def test_unassign_returns_to_pending(self):
        board = _make_board()
        t = board.create("work")
        board.assign(t.id, "alice")
        assert board.unassign(t.id) is True
        assert t.status == TaskStatus.PENDING
        assert t.assigned_worker is None

    def test_unassign_pending_fails(self):
        board = _make_board()
        t = board.create("work")
        assert board.unassign(t.id) is False

    def test_unassign_worker_releases_all(self):
        board = _make_board()
        t1 = board.create("a")
        t2 = board.create("b")
        board.assign(t1.id, "alice")
        board.assign(t2.id, "alice")
        board.unassign_worker("alice")
        assert t1.status == TaskStatus.PENDING
        assert t2.status == TaskStatus.PENDING


# ---------------------------------------------------------------------------
# Dependency management
# ---------------------------------------------------------------------------


class TestDependencies:
    def test_simple_dependency(self):
        board = _make_board()
        t1 = board.create("first")
        t2 = board.create("second", depends_on=[t1.id])
        assert t2.depends_on == [t1.id]

    def test_available_tasks_respects_deps(self):
        board = _make_board()
        t1 = board.create("first")
        board.create("second", depends_on=[t1.id])
        available = board.available_tasks
        # Only t1 should be available (t2 blocked by t1)
        assert len(available) == 1
        assert available[0].id == t1.id

    def test_deps_unblock_after_completion(self):
        board = _make_board()
        t1 = board.create("first")
        t2 = board.create("second", depends_on=[t1.id])
        board.assign(t1.id, "alice")
        board.complete(t1.id)
        available = board.available_tasks
        assert t2.id in [t.id for t in available]

    def test_circular_dependency_direct(self):
        """A depends on B, B depends on A."""
        board = _make_board()
        t1 = board.create("a")
        with pytest.raises(ValueError, match="circular"):
            board.create("b", depends_on=[t1.id])
            # Now try to make t1 depend on t2
            t2 = board.all_tasks[-1]
            board.update(t1.id, depends_on=[t2.id])

    def test_circular_dependency_via_update(self):
        """A -> B -> C, then try to make C -> A."""
        board = _make_board()
        t1 = board.create("a")
        t2 = board.create("b", depends_on=[t1.id])
        t3 = board.create("c", depends_on=[t2.id])
        with pytest.raises(ValueError, match="circular"):
            board.update(t1.id, depends_on=[t3.id])

    def test_self_referential_dependency(self):
        """A task cannot depend on itself."""
        board = _make_board()
        t = board.create("self")
        with pytest.raises(ValueError, match="circular"):
            board.update(t.id, depends_on=[t.id])

    def test_scrub_dependency_on_remove(self):
        """When a task is removed, it should be scrubbed from depends_on."""
        board = _make_board()
        t1 = board.create("dep")
        t2 = board.create("dependent", depends_on=[t1.id])
        board.remove(t1.id)
        assert t2.depends_on == []

    def test_scrub_dependency_on_bulk_remove(self):
        board = _make_board()
        t1 = board.create("dep")
        t2 = board.create("dependent", depends_on=[t1.id])
        board.remove_tasks({t1.id})
        assert t2.depends_on == []


# ---------------------------------------------------------------------------
# Query methods
# ---------------------------------------------------------------------------


class TestQueries:
    def test_all_tasks_sorted_by_priority(self):
        board = _make_board()
        low = board.create("low", priority=TaskPriority.LOW)
        urgent = board.create("urgent", priority=TaskPriority.URGENT)
        normal = board.create("normal", priority=TaskPriority.NORMAL)
        tasks = board.all_tasks
        assert tasks[0].id == urgent.id
        assert tasks[1].id == normal.id
        assert tasks[2].id == low.id

    def test_active_tasks(self):
        board = _make_board()
        t1 = board.create("a")
        t2 = board.create("b")
        board.assign(t1.id, "alice")
        active = board.active_tasks
        assert len(active) == 1
        assert active[0].id == t1.id
        assert t2.id not in [t.id for t in active]

    def test_tasks_for_worker(self):
        board = _make_board()
        t1 = board.create("a")
        board.create("b")
        board.assign(t1.id, "alice")
        assert len(board.tasks_for_worker("alice")) == 1
        assert len(board.tasks_for_worker("bob")) == 0

    def test_active_tasks_for_worker_excludes_completed(self):
        board = _make_board()
        t1 = board.create("a")
        t2 = board.create("b")
        board.assign(t1.id, "alice")
        board.assign(t2.id, "alice")
        board.complete(t1.id)
        active = board.active_tasks_for_worker("alice")
        assert len(active) == 1
        assert active[0].id == t2.id

    def test_summary(self):
        board = _make_board()
        board.create("a")
        t2 = board.create("b")
        board.assign(t2.id, "alice")
        summary = board.summary()
        assert "2 tasks" in summary
        assert "1 pending" in summary
        assert "1 active" in summary


# ---------------------------------------------------------------------------
# Update
# ---------------------------------------------------------------------------


class TestUpdate:
    def test_update_title(self):
        board = _make_board()
        t = board.create("old")
        board.update(t.id, title="new")
        assert t.title == "new"

    def test_update_missing_returns_false(self):
        board = _make_board()
        assert board.update("nope", title="x") is False

    def test_update_preserves_unset_fields(self):
        board = _make_board()
        t = board.create("work", priority=TaskPriority.HIGH)
        board.update(t.id, title="updated")
        assert t.priority == TaskPriority.HIGH


# ---------------------------------------------------------------------------
# Event emission
# ---------------------------------------------------------------------------


class TestEvents:
    def test_add_emits_change(self):
        board = _make_board()
        events: list[str] = []
        board.on_change(lambda: events.append("change"))
        board.add(_quick_task("x"))
        assert "change" in events

    def test_remove_emits_change(self):
        board = _make_board()
        t = board.add(_quick_task("x"))
        events: list[str] = []
        board.on_change(lambda: events.append("change"))
        board.remove(t.id)
        assert "change" in events

    def test_assign_emits_change(self):
        board = _make_board()
        t = board.create("x")
        events: list[str] = []
        board.on_change(lambda: events.append("change"))
        board.assign(t.id, "alice")
        assert "change" in events


# ---------------------------------------------------------------------------
# RLock reentrancy — callback re-enters board during emit
# ---------------------------------------------------------------------------


class TestRLockReentrancy:
    def test_callback_can_read_board_during_emit(self):
        """Event callback that reads available_tasks should not deadlock."""
        board = _make_board()
        results: list[int] = []

        def on_change():
            # This re-enters the board's RLock via available_tasks
            results.append(len(board.available_tasks))

        board.on_change(on_change)
        board.create("test")
        assert len(results) == 1
        assert results[0] >= 0  # didn't deadlock


# ---------------------------------------------------------------------------
# Thread safety
# ---------------------------------------------------------------------------


class TestConcurrency:
    def test_concurrent_add_no_duplicate_numbers(self):
        """Adding tasks from multiple threads should not produce duplicate numbers."""
        board = _make_board()
        barrier = threading.Barrier(10)

        def add_task():
            barrier.wait()
            board.create(f"task-{threading.current_thread().name}")

        threads = [threading.Thread(target=add_task) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        numbers = [t.number for t in board.all_tasks]
        assert len(numbers) == 10
        assert len(set(numbers)) == 10  # all unique
