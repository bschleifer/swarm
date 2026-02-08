"""SwarmTask — internal task model for agent coordination."""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum


class TaskStatus(Enum):
    PENDING = "pending"
    ASSIGNED = "assigned"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"


class TaskPriority(Enum):
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    URGENT = "urgent"


@dataclass
class SwarmTask:
    """A unit of work that can be assigned to a worker."""

    title: str
    description: str = ""
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    status: TaskStatus = TaskStatus.PENDING
    priority: TaskPriority = TaskPriority.NORMAL
    assigned_worker: str | None = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    completed_at: float | None = None
    depends_on: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)

    def assign(self, worker_name: str) -> None:
        self.assigned_worker = worker_name
        self.status = TaskStatus.ASSIGNED
        self.updated_at = time.time()

    def start(self) -> None:
        self.status = TaskStatus.IN_PROGRESS
        self.updated_at = time.time()

    def complete(self) -> None:
        self.status = TaskStatus.COMPLETED
        self.completed_at = time.time()
        self.updated_at = time.time()

    def fail(self) -> None:
        self.status = TaskStatus.FAILED
        self.updated_at = time.time()

    @property
    def is_available(self) -> bool:
        """True when task is pending."""
        return self.status == TaskStatus.PENDING

    @property
    def age(self) -> float:
        return time.time() - self.created_at


# Canonical display constants — single source of truth for all UIs
STATUS_ICON = {
    TaskStatus.PENDING: "○",
    TaskStatus.ASSIGNED: "◐",
    TaskStatus.IN_PROGRESS: "●",
    TaskStatus.COMPLETED: "✓",
    TaskStatus.FAILED: "✗",
}

PRIORITY_LABEL = {
    TaskPriority.URGENT: "!!",
    TaskPriority.HIGH: "!",
    TaskPriority.NORMAL: "",
    TaskPriority.LOW: "↓",
}
