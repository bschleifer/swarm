"""TaskBoard — in-memory task store for the swarm."""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

from swarm.events import EventEmitter
from swarm.logging import get_logger
from swarm.tasks.task import SwarmTask, TaskPriority, TaskStatus

if TYPE_CHECKING:
    from swarm.tasks.store import TaskStore

_log = get_logger("tasks.board")


class TaskBoard(EventEmitter):
    """In-memory task board for tracking and assigning work."""

    def __init__(self, store: TaskStore | None = None) -> None:
        self.__init_emitter__()
        self._tasks: dict[str, SwarmTask] = {}
        self._lock = threading.RLock()
        self._store = store
        if store:
            self._tasks = store.load()

    def on_change(self, callback) -> None:
        """Register callback for task board changes."""
        self.on("change", callback)

    def _notify(self) -> None:
        self.emit("change")

    def _persist(self) -> None:
        """Save tasks to store if configured."""
        if self._store:
            self._store.save(self._tasks)

    def add(self, task: SwarmTask) -> SwarmTask:
        """Add a task to the board."""
        with self._lock:
            self._tasks[task.id] = task
            _log.info("task added: %s — %s", task.id, task.title)
            self._persist()
            self._notify()
        return task

    def create(
        self,
        title: str,
        description: str = "",
        priority: TaskPriority = TaskPriority.NORMAL,
        depends_on: list[str] | None = None,
        tags: list[str] | None = None,
    ) -> SwarmTask:
        """Create and add a new task."""
        task = SwarmTask(
            title=title,
            description=description,
            priority=priority,
            depends_on=depends_on or [],
            tags=tags or [],
        )
        return self.add(task)

    def get(self, task_id: str) -> SwarmTask | None:
        return self._tasks.get(task_id)

    def remove(self, task_id: str) -> bool:
        with self._lock:
            if task_id in self._tasks:
                del self._tasks[task_id]
                self._persist()
                self._notify()
            else:
                return False
        return True

    def assign(self, task_id: str, worker_name: str) -> bool:
        """Assign a task to a worker."""
        with self._lock:
            task = self._tasks.get(task_id)
            if not task:
                return False
            if not task.is_available:
                return False
            task.assign(worker_name)
            _log.info("task %s assigned to %s", task_id, worker_name)
            self._persist()
            self._notify()
        return True

    def complete(self, task_id: str) -> bool:
        with self._lock:
            task = self._tasks.get(task_id)
            if not task:
                return False
            if task.status not in (TaskStatus.ASSIGNED, TaskStatus.IN_PROGRESS):
                _log.warning("cannot complete task %s — status is %s", task_id, task.status.value)
                return False
            task.complete()
            _log.info("task %s completed", task_id)
            self._persist()
            self._notify()
        return True

    def fail(self, task_id: str) -> bool:
        with self._lock:
            task = self._tasks.get(task_id)
            if not task:
                return False
            task.fail()
            _log.info("task %s failed", task_id)
            self._persist()
            self._notify()
        return True

    def unassign_worker(self, worker_name: str) -> None:
        """Unassign all tasks from a worker (e.g., when worker dies)."""
        with self._lock:
            for task in self._tasks.values():
                if task.assigned_worker == worker_name and task.status in (
                    TaskStatus.ASSIGNED,
                    TaskStatus.IN_PROGRESS,
                ):
                    task.status = TaskStatus.PENDING
                    task.assigned_worker = None
                    _log.info("unassigned task %s from dead worker %s", task.id, worker_name)
            self._persist()
            self._notify()

    @property
    def all_tasks(self) -> list[SwarmTask]:
        """All tasks sorted by priority (urgent first) then creation time."""
        priority_order = {
            TaskPriority.URGENT: 0,
            TaskPriority.HIGH: 1,
            TaskPriority.NORMAL: 2,
            TaskPriority.LOW: 3,
        }
        with self._lock:
            snapshot = list(self._tasks.values())
        return sorted(
            snapshot,
            key=lambda t: (priority_order.get(t.priority, 2), t.created_at),
        )

    @property
    def available_tasks(self) -> list[SwarmTask]:
        """Tasks that are pending and have all dependencies met."""
        with self._lock:
            snapshot = list(self._tasks.values())
        completed_ids = {t.id for t in snapshot if t.status == TaskStatus.COMPLETED}
        sorted_tasks = sorted(
            snapshot,
            key=lambda t: (
                {
                    TaskPriority.URGENT: 0,
                    TaskPriority.HIGH: 1,
                    TaskPriority.NORMAL: 2,
                    TaskPriority.LOW: 3,
                }.get(t.priority, 2),
                t.created_at,
            ),
        )
        return [
            t
            for t in sorted_tasks
            if t.is_available and all(d in completed_ids for d in t.depends_on)
        ]

    @property
    def active_tasks(self) -> list[SwarmTask]:
        """Tasks currently assigned or in progress."""
        with self._lock:
            snapshot = list(self._tasks.values())
        return [t for t in snapshot if t.status in (TaskStatus.ASSIGNED, TaskStatus.IN_PROGRESS)]

    def tasks_for_worker(self, worker_name: str) -> list[SwarmTask]:
        """Get all tasks assigned to a specific worker."""
        with self._lock:
            snapshot = list(self._tasks.values())
        return [t for t in snapshot if t.assigned_worker == worker_name]

    def summary(self) -> str:
        """One-line summary of the board state."""
        with self._lock:
            snapshot = list(self._tasks.values())
        total = len(snapshot)
        pending = sum(1 for t in snapshot if t.status == TaskStatus.PENDING)
        active = sum(
            1 for t in snapshot if t.status in (TaskStatus.ASSIGNED, TaskStatus.IN_PROGRESS)
        )
        done = sum(1 for t in snapshot if t.status == TaskStatus.COMPLETED)
        return f"{total} tasks: {pending} pending, {active} active, {done} done"
