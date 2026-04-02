"""SQLite-backed task store — drop-in replacement for FileTaskStore."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from swarm.db.base_store import BaseStore
from swarm.logging import get_logger
from swarm.tasks.task import SwarmTask, TaskPriority, TaskStatus, TaskType

if TYPE_CHECKING:
    from swarm.db.core import SwarmDB

_log = get_logger("db.task_store")


class SqliteTaskStore(BaseStore):
    """Persist tasks to the unified swarm.db.

    Conforms to the ``TaskStore`` protocol (``save`` / ``load``),
    but also offers single-row helpers so the task board can
    update individual tasks without rewriting everything.
    """

    def __init__(self, db: SwarmDB) -> None:
        self._db = db

    # ------------------------------------------------------------------
    # TaskStore protocol
    # ------------------------------------------------------------------

    def save(self, tasks: dict[str, SwarmTask]) -> None:
        """Write all tasks to the DB (full replace)."""
        for task in tasks.values():
            self.save_one(task)

    def load(self) -> dict[str, SwarmTask]:
        """Load all tasks from the DB."""
        rows = self._db.fetchall("SELECT * FROM tasks")
        tasks: dict[str, SwarmTask] = {}
        for row in rows:
            try:
                task = _row_to_task(row)
                tasks[task.id] = task
            except (KeyError, ValueError):
                _log.warning(
                    "skipping corrupt task row: %s",
                    row["id"] if "id" in row.keys() else "?",
                )
        _log.info("loaded %d tasks from swarm.db", len(tasks))
        return tasks

    # ------------------------------------------------------------------
    # Single-row operations
    # ------------------------------------------------------------------

    def save_one(self, task: SwarmTask) -> None:
        """Insert or update a single task."""
        data = _task_to_row(task)
        cols = ", ".join(data.keys())
        placeholders = ", ".join("?" for _ in data)
        conflict = ", ".join(f"{k} = ?" for k in data)
        sql = (
            f"INSERT INTO tasks ({cols}) VALUES ({placeholders}) "
            f"ON CONFLICT(id) DO UPDATE SET {conflict}"
        )
        params = tuple(data.values()) + tuple(data.values())
        self._db.execute(sql, params)
        self._db.commit()

    def delete_one(self, task_id: str) -> bool:
        """Delete a task by ID. Returns True if deleted."""
        return self._db.delete("tasks", "id = ?", (task_id,)) > 0

    def backup(self, max_backups: int = 5) -> None:
        """DB-level backup handled by SwarmDB.backup() — no-op here."""


def _task_to_row(task: SwarmTask) -> dict[str, Any]:
    return {
        "id": task.id,
        "number": task.number,
        "title": task.title,
        "description": task.description,
        "status": task.status.value,
        "priority": task.priority.value,
        "task_type": task.task_type.value,
        "assigned_worker": task.assigned_worker,
        "created_at": task.created_at,
        "updated_at": task.updated_at,
        "completed_at": task.completed_at,
        "resolution": task.resolution,
        "tags": json.dumps(task.tags),
        "attachments": json.dumps(task.attachments),
        "depends_on": json.dumps(task.depends_on),
        "source_email_id": task.source_email_id,
        "jira_key": task.jira_key,
        "is_cross_project": 1 if task.is_cross_project else 0,
        "source_worker": task.source_worker,
        "target_worker": task.target_worker,
        "dependency_type": task.dependency_type,
        "acceptance_criteria": json.dumps(task.acceptance_criteria),
        "context_refs": json.dumps(task.context_refs),
        "cost_budget": task.cost_budget,
        "cost_spent": task.cost_spent,
        "learnings": task.learnings,
    }


def _row_to_task(row: Any) -> SwarmTask:
    _jl = BaseStore._parse_json_field

    return SwarmTask(
        id=row["id"],
        title=row["title"],
        description=row["description"] or "",
        status=TaskStatus(row["status"]),
        priority=TaskPriority(row["priority"] or "normal"),
        task_type=TaskType(row["task_type"] or "chore"),
        assigned_worker=row["assigned_worker"],
        created_at=row["created_at"] or 0.0,
        updated_at=row["updated_at"] or 0.0,
        completed_at=row["completed_at"],
        depends_on=_jl(row["depends_on"], []),
        tags=_jl(row["tags"], []),
        attachments=_jl(row["attachments"], []),
        resolution=row["resolution"] or "",
        source_email_id=row["source_email_id"] or "",
        jira_key=row["jira_key"] or "",
        number=row["number"] or 0,
        is_cross_project=bool(row["is_cross_project"]),
        source_worker=row["source_worker"] or "",
        target_worker=row["target_worker"] or "",
        dependency_type=row["dependency_type"] or "blocks",
        acceptance_criteria=_jl(row["acceptance_criteria"], []),
        context_refs=_jl(row["context_refs"], []),
        cost_budget=row["cost_budget"] or 0.0,
        cost_spent=row["cost_spent"] or 0.0,
        learnings=row["learnings"] or "",
    )
