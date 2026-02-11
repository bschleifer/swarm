"""Task persistence — save/load task board state to disk."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Protocol

from swarm.logging import get_logger
from swarm.tasks.task import SwarmTask, TaskPriority, TaskStatus, TaskType

_log = get_logger("tasks.store")

_DEFAULT_PATH = Path.home() / ".swarm" / "tasks.json"


class TaskStore(Protocol):
    """Protocol for task persistence backends."""

    def save(self, tasks: dict[str, SwarmTask]) -> None: ...
    def load(self) -> dict[str, SwarmTask]: ...


class FileTaskStore:
    """Persist tasks as JSON to a file."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or _DEFAULT_PATH

    def save(self, tasks: dict[str, SwarmTask]) -> None:
        """Write all tasks to disk atomically."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = [_task_to_dict(t) for t in tasks.values()]
        try:
            tmp = self.path.with_suffix(f".tmp.{os.getpid()}")
            tmp.write_text(json.dumps(data, indent=2))
            os.replace(tmp, self.path)
        except OSError:
            _log.warning("failed to save tasks to %s", self.path, exc_info=True)

    def load(self) -> dict[str, SwarmTask]:
        """Read tasks from disk. Returns empty dict if file missing or corrupt."""
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text())
            if not isinstance(data, list):
                _log.warning("tasks file %s does not contain a list", self.path)
                return {}
            tasks: dict[str, SwarmTask] = {}
            for item in data:
                task = _dict_to_task(item)
                tasks[task.id] = task
            _log.info("loaded %d tasks from %s", len(tasks), self.path)
            return tasks
        except (json.JSONDecodeError, OSError, KeyError, TypeError, ValueError):
            _log.warning("failed to load tasks from %s", self.path, exc_info=True)
            return {}


def _task_to_dict(task: SwarmTask) -> dict:
    return {
        "id": task.id,
        "title": task.title,
        "description": task.description,
        "status": task.status.value,
        "priority": task.priority.value,
        "task_type": task.task_type.value,
        "assigned_worker": task.assigned_worker,
        "created_at": task.created_at,
        "updated_at": task.updated_at,
        "completed_at": task.completed_at,
        "depends_on": task.depends_on,
        "tags": task.tags,
        "attachments": task.attachments,
    }


def _dict_to_task(d: dict) -> SwarmTask:
    return SwarmTask(
        id=d["id"],
        title=d["title"],
        description=d.get("description", ""),
        status=TaskStatus(d["status"]),
        priority=TaskPriority(d.get("priority", "normal")),
        task_type=TaskType(d.get("task_type", "chore")),
        assigned_worker=d.get("assigned_worker"),
        created_at=d.get("created_at", 0.0),
        updated_at=d.get("updated_at", 0.0),
        completed_at=d.get("completed_at"),
        depends_on=d.get("depends_on", []),
        tags=d.get("tags", []),
        attachments=d.get("attachments", []),
    )
