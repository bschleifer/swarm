"""JiraService — Jira import/export/sync operations extracted from SwarmDaemon."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from swarm.logging import get_logger

if TYPE_CHECKING:
    from swarm.integrations.jira import JiraSyncService
    from swarm.tasks.board import TaskBoard
    from swarm.tasks.task import TaskStatus

_log = get_logger("server.jira_service")


def _log_task_exception(task: asyncio.Task[object]) -> None:
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        _log.error("fire-and-forget task failed: %s", exc, exc_info=exc)


class JiraService:
    """Manages Jira import/export/sync operations."""

    def __init__(
        self,
        *,
        get_jira: Callable[[], JiraSyncService],
        task_board: TaskBoard,
        broadcast_ws: Callable[[dict[str, Any]], None],
        drone_log: Any,
        track_task: Callable[[asyncio.Task[object]], None],
        get_sync_interval: Callable[[], int],
    ) -> None:
        self._get_jira = get_jira
        self._task_board = task_board
        self._broadcast_ws = broadcast_ws
        self._drone_log = drone_log
        self._track_task = track_task
        self._get_sync_interval = get_sync_interval

    async def run_import(self) -> int:
        """Execute a single Jira import cycle. Returns count of new tasks."""
        from swarm.drones.log import LogCategory, SystemAction

        jira = self._get_jira()
        existing = {t.id: t for t in self._task_board.all_tasks}
        new_tasks = await jira.import_issues(existing)
        for task in new_tasks:
            self._task_board.add(task)
            self._drone_log.add(
                SystemAction.TASK_CREATED,
                "system",
                detail=f"imported from Jira: {task.jira_key}",
                category=LogCategory.SYSTEM,
            )
        if new_tasks:
            self._broadcast_ws({"type": "jira_import", "count": len(new_tasks)})
        return len(new_tasks)

    async def export_status(self, task_id: str, new_status: TaskStatus) -> bool:
        """Export a task status change to Jira."""
        task = self._task_board.get(task_id)
        if not task or not task.jira_key:
            return False
        jira = self._get_jira()
        return await jira.export_status(task, new_status)

    def fire_jira(self, task_id: str, action: str, coro_factory: Callable[..., Any]) -> None:
        """Schedule a Jira operation as fire-and-forget background task.

        Shared guard: checks Jira is enabled and task has a Jira key.
        """
        jira = self._get_jira()
        if not jira or not jira.enabled:
            return
        task = self._task_board.get(task_id)
        if not task or not task.jira_key:
            return

        async def _do() -> None:
            try:
                await coro_factory(jira, task)
            except Exception:
                _log.warning("jira %s failed for %s", action, task_id, exc_info=True)

        self._track_task(asyncio.create_task(_do()))

    def fire_export(self, task_id: str, new_status: str) -> None:
        """Schedule Jira status export as fire-and-forget background task."""
        from swarm.tasks.task import TaskStatus

        status = TaskStatus(new_status)
        self.fire_jira(task_id, "export", lambda jira, task: jira.export_status(task, status))

    def fire_assign(self, task_id: str) -> None:
        """Schedule Jira issue assignment as fire-and-forget background task."""
        self.fire_jira(task_id, "assign", lambda jira, task: jira.assign_to_me(task))

    def fire_completion(self, task_id: str) -> None:
        """Schedule Jira completion comment as fire-and-forget background task."""
        self.fire_jira(
            task_id,
            "comment",
            lambda jira, task: jira.post_completion_comment(task),
        )

    async def sync_loop(self) -> None:
        """Periodically import Jira issues into the task board."""
        try:
            while True:
                interval = self._get_sync_interval()
                await asyncio.sleep(interval)
                await self.run_import()
        except asyncio.CancelledError:
            return
