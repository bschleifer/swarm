"""SQLite-backed task history — drop-in replacement for JSONL TaskHistory."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from swarm.db.base_store import BaseStore
from swarm.logging import get_logger
from swarm.tasks.history import TaskAction, TaskEvent

if TYPE_CHECKING:
    from swarm.db.core import SwarmDB

_log = get_logger("db.task_history")


class SqliteTaskHistory(BaseStore):
    """Task audit log backed by the task_history table in swarm.db.

    Drop-in replacement for :class:`~swarm.tasks.history.TaskHistory`.
    No file rotation needed — old entries are pruned by ``prune()``.
    """

    def __init__(self, db: SwarmDB) -> None:
        self._db = db

    def append(
        self,
        task_id: str,
        action: TaskAction,
        actor: str = "user",
        detail: str = "",
    ) -> TaskEvent:
        """Record a task event. Returns the created event."""
        now = time.time()
        self._db.insert(
            "task_history",
            {
                "task_id": task_id,
                "action": action.value,
                "actor": actor,
                "detail": detail,
                "created_at": now,
            },
        )
        return TaskEvent(
            timestamp=now,
            task_id=task_id,
            action=action,
            actor=actor,
            detail=detail,
        )

    def get_events(self, task_id: str, limit: int = 50) -> list[TaskEvent]:
        """Get events for a specific task, newest first."""
        rows = self._db.fetchall(
            "SELECT * FROM task_history WHERE task_id = ? ORDER BY created_at DESC LIMIT ?",
            (task_id, limit),
        )
        events = []
        for r in rows:
            try:
                events.append(
                    TaskEvent(
                        timestamp=r["created_at"],
                        task_id=r["task_id"],
                        action=TaskAction(r["action"]),
                        actor=r["actor"] or "user",
                        detail=r["detail"] or "",
                    )
                )
            except (KeyError, ValueError):
                continue
        # Return in chronological order
        events.reverse()
        return events

    def search(
        self,
        query: str = "",
        action: str = "",
        actor: str = "",
        since: float = 0,
        until: float = 0,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[list[TaskEvent], int]:
        """Search across all task history. Returns (events, total_count)."""
        conditions = []
        params: list[object] = []
        if query:
            conditions.append("(detail LIKE ? OR task_id LIKE ?)")
            params.extend([f"%{query}%", f"%{query}%"])
        if action:
            conditions.append("action = ?")
            params.append(action)
        if actor:
            conditions.append("actor = ?")
            params.append(actor)
        if since:
            conditions.append("created_at >= ?")
            params.append(since)
        if until:
            conditions.append("created_at <= ?")
            params.append(until)

        where = " AND ".join(conditions) if conditions else "1=1"

        count_row = self._db.fetchone(
            f"SELECT COUNT(*) AS cnt FROM task_history WHERE {where}",
            tuple(params),
        )
        total = count_row["cnt"] if count_row else 0

        rows = self._db.fetchall(
            f"SELECT * FROM task_history WHERE {where} ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (*params, limit, offset),
        )
        events = []
        for r in rows:
            try:
                events.append(
                    TaskEvent(
                        timestamp=r["created_at"],
                        task_id=r["task_id"],
                        action=TaskAction(r["action"]),
                        actor=r["actor"] or "user",
                        detail=r["detail"] or "",
                    )
                )
            except (KeyError, ValueError):
                continue
        return events, total

    def prune(self, max_age_days: int = 90) -> int:
        """Delete entries older than max_age_days. Returns count deleted."""
        return self._prune_older_than("task_history", "created_at", max_age_days)
