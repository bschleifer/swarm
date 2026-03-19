"""TaskBoard — in-memory task store for the swarm."""

from __future__ import annotations

import threading
from collections.abc import Callable
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
        # RLock is required: _notify() emits events whose callbacks may
        # re-enter the board (e.g. expire_stale_proposals reads available_tasks).
        # All locked sections are fast in-memory operations (no I/O, no awaits).
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

    def on_change(self, callback: Callable[[], None]) -> None:
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
        """Create and add a new task.

        Raises ValueError if the depends_on list would create a cycle.
        """
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
        if task.depends_on and self._has_cycle(task.id, task.depends_on):
            raise ValueError("circular task dependency detected")
        return self.add(task)

    def _has_cycle(self, task_id: str, depends_on: list[str]) -> bool:
        """Detect circular dependencies using DFS."""
        visited: set[str] = set()

        def _dfs(tid: str) -> bool:
            if tid in visited:
                return False
            visited.add(tid)
            deps = depends_on if tid == task_id else getattr(self._tasks.get(tid), "depends_on", [])
            for dep_id in deps:
                if dep_id == task_id:
                    return True
                if _dfs(dep_id):
                    return True
            return False

        return _dfs(task_id)

    def get(self, task_id: str) -> SwarmTask | None:
        return self._tasks.get(task_id)

    def remove(self, task_id: str) -> bool:
        with self._lock:
            if task_id in self._tasks:
                del self._tasks[task_id]
                self._scrub_dependency(task_id)
                self._persist()
                self._notify()
            else:
                return False
        return True

    def remove_tasks(self, task_ids: set[str]) -> int:
        """Remove multiple tasks by ID. Returns count removed."""
        removed = 0
        with self._lock:
            for tid in task_ids:
                if tid in self._tasks:
                    del self._tasks[tid]
                    removed += 1
            if removed:
                for tid in task_ids:
                    self._scrub_dependency(tid)
                self._persist()
                self._notify()
        return removed

    def _scrub_dependency(self, task_id: str) -> None:
        """Remove *task_id* from all other tasks' ``depends_on`` lists."""
        for task in self._tasks.values():
            if task_id in task.depends_on:
                task.depends_on = [d for d in task.depends_on if d != task_id]

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
        source_worker: str | None = None,
        target_worker: str | None = None,
        dependency_type: str | None = None,
        acceptance_criteria: list[str] | None = None,
        context_refs: list[str] | None = None,
    ) -> bool:
        """Update fields on an existing task. Only non-None fields are changed."""
        import time

        with self._lock:
            task = self._tasks.get(task_id)
            if not task:
                return False
            self._apply_core_fields(
                task,
                title,
                description,
                priority,
                task_type,
                tags,
                attachments,
            )
            if depends_on is not None:
                if self._has_cycle(task_id, depends_on):
                    raise ValueError("circular task dependency detected")
                task.depends_on = depends_on
            self._apply_cross_fields(
                task,
                source_worker,
                target_worker,
                dependency_type,
                acceptance_criteria,
                context_refs,
            )
            task.updated_at = time.time()
            self._persist()
            self._notify()
        return True

    @staticmethod
    def _apply_core_fields(
        task: SwarmTask,
        title: str | None,
        description: str | None,
        priority: TaskPriority | None,
        task_type: TaskType | None,
        tags: list[str] | None,
        attachments: list[str] | None,
    ) -> None:
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

    @staticmethod
    def _apply_cross_fields(
        task: SwarmTask,
        source_worker: str | None,
        target_worker: str | None,
        dependency_type: str | None,
        acceptance_criteria: list[str] | None,
        context_refs: list[str] | None,
    ) -> None:
        if source_worker is not None:
            task.source_worker = source_worker
        if target_worker is not None:
            task.target_worker = target_worker
        if dependency_type is not None:
            task.dependency_type = dependency_type
        if acceptance_criteria is not None:
            task.acceptance_criteria = acceptance_criteria
        if context_refs is not None:
            task.context_refs = context_refs
        if source_worker or target_worker:
            task.is_cross_project = True

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

    def reopen(self, task_id: str) -> bool:
        """Reopen a completed or failed task, returning it to PENDING."""
        with self._lock:
            task = self._tasks.get(task_id)
            if not task:
                return False
            if task.status not in (TaskStatus.COMPLETED, TaskStatus.FAILED):
                return False
            task.reopen()
            _log.info("task %s reopened", task_id)
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

    def set_jira_key(self, task_id: str, jira_key: str) -> bool:
        """Set the jira_key on an existing task. Thread-safe."""
        import time

        with self._lock:
            task = self._tasks.get(task_id)
            if not task:
                return False
            task.jira_key = jira_key
            task.updated_at = time.time()
            self._persist()
            self._notify()
        return True

    def reassign_worker(self, old_name: str, new_name: str) -> None:
        """Reassign all tasks from one worker name to another (rename)."""
        with self._lock:
            for task in self._tasks.values():
                if task.assigned_worker == old_name:
                    task.assigned_worker = new_name
            self._persist()

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

    def create_cross_project(
        self,
        title: str,
        description: str = "",
        source_worker: str = "",
        target_worker: str = "",
        dependency_type: str = "blocks",
        priority: TaskPriority = TaskPriority.NORMAL,
        task_type: TaskType = TaskType.CHORE,
        acceptance_criteria: list[str] | None = None,
        context_refs: list[str] | None = None,
    ) -> SwarmTask:
        """Create a cross-project task in PROPOSED status."""
        task = SwarmTask(
            title=title,
            description=description,
            status=TaskStatus.PROPOSED,
            priority=priority,
            task_type=task_type,
            is_cross_project=True,
            source_worker=source_worker,
            target_worker=target_worker,
            dependency_type=dependency_type,
            acceptance_criteria=acceptance_criteria or [],
            context_refs=context_refs or [],
        )
        return self.add(task)

    def approve_task(self, task_id: str) -> bool:
        """Approve a PROPOSED task, transitioning to PENDING."""
        with self._lock:
            task = self._tasks.get(task_id)
            if not task:
                return False
            if task.status != TaskStatus.PROPOSED:
                return False
            task.approve()
            _log.info("task %s approved", task_id)
            self._persist()
            self._notify()
        return True

    def reject_task(self, task_id: str, resolution: str = "") -> bool:
        """Reject a PROPOSED task, transitioning to FAILED."""
        with self._lock:
            task = self._tasks.get(task_id)
            if not task:
                return False
            if task.status != TaskStatus.PROPOSED:
                return False
            task.reject(resolution)
            _log.info("task %s rejected", task_id)
            self._persist()
            self._notify()
        return True

    @property
    def proposed_tasks(self) -> list[SwarmTask]:
        """Tasks awaiting review (PROPOSED status)."""
        with self._lock:
            snapshot = list(self._tasks.values())
        return sorted(
            [t for t in snapshot if t.status == TaskStatus.PROPOSED],
            key=lambda t: (_PRIORITY_ORDER.get(t.priority, 2), t.created_at),
        )

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

    def active_tasks_for_worker(self, worker_name: str) -> list[SwarmTask]:
        """Get only ASSIGNED/IN_PROGRESS tasks for a worker (excludes completed)."""
        with self._lock:
            snapshot = list(self._tasks.values())
        return [
            t
            for t in snapshot
            if t.assigned_worker == worker_name
            and t.status in (TaskStatus.ASSIGNED, TaskStatus.IN_PROGRESS)
        ]

    def summary(self) -> str:
        """One-line summary of the board state."""
        with self._lock:
            snapshot = list(self._tasks.values())
        total = len(snapshot)
        proposed = sum(1 for t in snapshot if t.status == TaskStatus.PROPOSED)
        pending = sum(1 for t in snapshot if t.status == TaskStatus.PENDING)
        active = sum(
            1 for t in snapshot if t.status in (TaskStatus.ASSIGNED, TaskStatus.IN_PROGRESS)
        )
        done = sum(1 for t in snapshot if t.status == TaskStatus.COMPLETED)
        parts = [f"{total} tasks:"]
        if proposed:
            parts.append(f"{proposed} proposed,")
        parts.append(f"{pending} pending, {active} active, {done} done")
        return " ".join(parts)
