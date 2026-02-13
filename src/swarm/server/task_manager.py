"""TaskManager — task lifecycle operations extracted from SwarmDaemon."""

from __future__ import annotations

from typing import TYPE_CHECKING

from swarm.drones.log import LogCategory, SystemAction
from swarm.logging import get_logger
from swarm.tasks.history import TaskAction
from swarm.tasks.task import (
    SwarmTask,
    TaskPriority,
    TaskStatus,
    TaskType,
    auto_classify_type,
    smart_title,
)

if TYPE_CHECKING:
    from swarm.drones.log import DroneLog
    from swarm.drones.pilot import DronePilot
    from swarm.tasks.board import TaskBoard
    from swarm.tasks.history import TaskHistory

_log = get_logger("server.tasks")


class TaskManager:
    """Handles pure task lifecycle operations: create, edit, status transitions.

    Does NOT handle assign (requires worker/tmux coordination) or complete
    (requires email/graph side effects). Those stay on SwarmDaemon.
    """

    def __init__(
        self,
        task_board: TaskBoard,
        task_history: TaskHistory,
        drone_log: DroneLog,
        pilot: DronePilot | None = None,
    ) -> None:
        self.task_board = task_board
        self.task_history = task_history
        self.drone_log = drone_log
        self._pilot = pilot

    def require_task(
        self, task_id: str, allowed_statuses: set[TaskStatus] | None = None
    ) -> SwarmTask:
        """Get a task by ID or raise TaskOperationError.

        If *allowed_statuses* is given, also validates the task's current status.
        """
        from swarm.server.daemon import TaskOperationError

        task = self.task_board.get(task_id)
        if not task:
            raise TaskOperationError(f"Task '{task_id}' not found")
        if allowed_statuses and task.status not in allowed_statuses:
            raise TaskOperationError(f"Task '{task_id}' cannot be modified ({task.status.value})")
        return task

    def create_task(
        self,
        title: str,
        description: str = "",
        priority: TaskPriority = TaskPriority.NORMAL,
        task_type: TaskType = TaskType.CHORE,
        tags: list[str] | None = None,
        depends_on: list[str] | None = None,
        attachments: list[str] | None = None,
        source_email_id: str = "",
        actor: str = "user",
    ) -> SwarmTask:
        """Create a task. Broadcast happens via task_board.on_change."""
        task = self.task_board.create(
            title=title,
            description=description,
            priority=priority,
            task_type=task_type,
            tags=tags,
            depends_on=depends_on,
            attachments=attachments,
            source_email_id=source_email_id,
        )
        self.task_history.append(task.id, TaskAction.CREATED, actor=actor, detail=title)
        self.drone_log.add(
            SystemAction.TASK_CREATED,
            actor,
            title,
            category=LogCategory.TASK,
        )
        return task

    async def create_task_smart(
        self,
        title: str = "",
        description: str = "",
        priority: TaskPriority = TaskPriority.NORMAL,
        task_type: TaskType | None = None,
        tags: list[str] | None = None,
        depends_on: list[str] | None = None,
        attachments: list[str] | None = None,
        source_email_id: str = "",
        actor: str = "user",
    ) -> SwarmTask:
        """Create a task with auto-title generation and type classification.

        If *title* is empty, uses Claude to generate one from the description.
        If *task_type* is None, auto-classifies from title + description.
        """
        from swarm.server.daemon import SwarmOperationError

        if not title and description:
            title = await smart_title(description) or ""
        if not title:
            raise SwarmOperationError("title or description required")
        if task_type is None:
            task_type = auto_classify_type(title, description)
        return self.create_task(
            title=title,
            description=description,
            priority=priority,
            task_type=task_type,
            tags=tags,
            depends_on=depends_on,
            attachments=attachments,
            source_email_id=source_email_id,
            actor=actor,
        )

    def unassign_task(self, task_id: str, actor: str = "user") -> bool:
        """Unassign a task, returning it to PENDING. Raises if not found or wrong state."""
        self.require_task(task_id, {TaskStatus.ASSIGNED, TaskStatus.IN_PROGRESS})
        result = self.task_board.unassign(task_id)
        if result and self._pilot:
            self._pilot.clear_proposed_completion(task_id)
            self.task_history.append(task_id, TaskAction.EDITED, actor=actor, detail="unassigned")
        return result

    def reopen_task(self, task_id: str, actor: str = "user") -> bool:
        """Reopen a completed or failed task, returning it to PENDING."""
        self.require_task(task_id, {TaskStatus.COMPLETED, TaskStatus.FAILED})
        result = self.task_board.reopen(task_id)
        if result and self._pilot:
            self._pilot.clear_proposed_completion(task_id)
            self.task_history.append(task_id, TaskAction.REOPENED, actor=actor)
        return result

    def fail_task(self, task_id: str, actor: str = "user") -> bool:
        """Fail a task. Raises if not found."""
        task = self.require_task(task_id)
        result = self.task_board.fail(task_id)
        if result:
            self.task_history.append(task_id, TaskAction.FAILED, actor=actor)
            self.drone_log.add(
                SystemAction.TASK_FAILED,
                actor,
                task.title,
                category=LogCategory.TASK,
                is_notification=True,
            )
        return result

    def remove_task(self, task_id: str, actor: str = "user") -> bool:
        """Remove a task. Raises if not found."""
        task = self.require_task(task_id)
        self.task_board.remove(task_id)
        self.task_history.append(task_id, TaskAction.REMOVED, actor=actor)
        self.drone_log.add(
            SystemAction.TASK_REMOVED,
            actor,
            task.title,
            category=LogCategory.TASK,
        )
        return True

    def edit_task(
        self,
        task_id: str,
        title: str | None = None,
        description: str | None = None,
        priority: TaskPriority | None = None,
        task_type: TaskType | None = None,
        tags: list[str] | None = None,
        attachments: list[str] | None = None,
        depends_on: list[str] | None = None,
        actor: str = "user",
    ) -> bool:
        """Edit a task. Raises if not found."""
        self.require_task(task_id)
        result = self.task_board.update(
            task_id,
            title=title,
            description=description,
            priority=priority,
            task_type=task_type,
            tags=tags,
            attachments=attachments,
            depends_on=depends_on,
        )
        if result:
            self.task_history.append(task_id, TaskAction.EDITED, actor=actor)
        return result
