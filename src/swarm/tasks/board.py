"""TaskBoard — in-memory task store for the swarm."""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

from swarm.events import EventEmitter
from swarm.logging import get_logger
from swarm.tasks.task import SwarmTask, TaskPriority, TaskStatus, TaskType

if TYPE_CHECKING:
    from swarm.tasks.store import TaskStore

_log = get_logger("tasks.board")

_PRIORITY_ORDER = {
    TaskPriority.URGENT: 0,
    TaskPriority.HIGH: 1,
    TaskPriority.NORMAL: 2,
    TaskPriority.LOW: 3,
}


class TaskBoard(EventEmitter):
    """In-memory task board for tracking and assigning work."""

    def __init__(self, store: TaskStore | None = None) -> None:
        self.__init_emitter__()
        self._tasks: dict[str, SwarmTask] = {}
        self._lock = threading.RLock()
        self._store = store
        if store:
            self._tasks = store.load()
        # Derive next number from existing tasks; backfill any with number=0
        existing = [t.number for t in self._tasks.values() if t.number > 0]
        self._next_number: int = max(existing, default=0) + 1
        backfilled = False
        for task in sorted(self._tasks.values(), key=lambda t: t.created_at):
            if task.number == 0:
                task.number = self._next_number
                self._next_number += 1
                backfilled = True
        if backfilled:
            self._persist()

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
            if task.number == 0:
                task.number = self._next_number
                self._next_number += 1
            self._tasks[task.id] = task
            _log.info("task #%d added: %s — %s", task.number, task.id, task.title)
            self._persist()
            self._notify()
        return task

    def create(
        self,
        title: str,
        description: str = "",
        priority: TaskPriority = TaskPriority.NORMAL,
        task_type: TaskType = TaskType.CHORE,
        depends_on: list[str] | None = None,
        tags: list[str] | None = None,
        attachments: list[str] | None = None,
        source_email_id: str = "",
    ) -> SwarmTask:
        """Create and add a new task."""
        task = SwarmTask(
            title=title,
            description=description,
            priority=priority,
            task_type=task_type,
            depends_on=depends_on or [],
            tags=tags or [],
            attachments=attachments or [],
            source_email_id=source_email_id,
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

    def update(
        self,
        task_id: str,
        title: str | None = None,
        description: str | None = None,
        priority: TaskPriority | None = None,
        task_type: TaskType | None = None,
        tags: list[str] | None = None,
        attachments: list[str] | None = None,
        depends_on: list[str] | None = None,
    ) -> bool:
        """Update fields on an existing task. Only non-None fields are changed."""
        import time

        with self._lock:
            task = self._tasks.get(task_id)
            if not task:
                return False
            if title is not None:
                task.title = title
            if description is not None:
                task.description = description
            if priority is not None:
                task.priority = priority
            if task_type is not None:
                task.task_type = task_type
            if tags is not None:
                task.tags = tags
            if attachments is not None:
                task.attachments = attachments
            if depends_on is not None:
                task.depends_on = depends_on
            task.updated_at = time.time()
            self._persist()
            self._notify()
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

    def complete(self, task_id: str, resolution: str = "") -> bool:
        with self._lock:
            task = self._tasks.get(task_id)
            if not task:
                return False
            if task.status not in (TaskStatus.ASSIGNED, TaskStatus.IN_PROGRESS):
                _log.warning("cannot complete task %s — status is %s", task_id, task.status.value)
                return False
            task.complete(resolution=resolution)
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

    def unassign(self, task_id: str) -> bool:
        """Unassign a single task, returning it to PENDING."""
        with self._lock:
            task = self._tasks.get(task_id)
            if not task:
                return False
            if task.status not in (TaskStatus.ASSIGNED, TaskStatus.IN_PROGRESS):
                return False
            task.unassign()
            _log.info("task %s unassigned", task_id)
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
        with self._lock:
            snapshot = list(self._tasks.values())
        return sorted(
            snapshot,
            key=lambda t: (_PRIORITY_ORDER.get(t.priority, 2), t.created_at),
        )

    @property
    def available_tasks(self) -> list[SwarmTask]:
        """Tasks that are pending and have all dependencies met."""
        with self._lock:
            snapshot = list(self._tasks.values())
        completed_ids = {t.id for t in snapshot if t.status == TaskStatus.COMPLETED}
        sorted_tasks = sorted(
            snapshot,
            key=lambda t: (_PRIORITY_ORDER.get(t.priority, 2), t.created_at),
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
