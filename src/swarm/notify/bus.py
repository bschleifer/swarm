"""Notification bus — event routing for swarm notifications."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable

from swarm.logging import get_logger

_log = get_logger("notify.bus")


class EventType(Enum):
    WORKER_IDLE = "worker_idle"
    WORKER_STUNG = "worker_stung"
    WORKER_ESCALATED = "worker_escalated"
    DRONE_ACTION = "drone_action"
    QUEEN_RESPONSE = "queen_response"
    TASK_ASSIGNED = "task_assigned"
    TASK_COMPLETED = "task_completed"


class Severity(Enum):
    INFO = "info"
    WARNING = "warning"
    URGENT = "urgent"


# Map event types to default severities
_DEFAULT_SEVERITY = {
    EventType.WORKER_IDLE: Severity.INFO,
    EventType.WORKER_STUNG: Severity.WARNING,
    EventType.WORKER_ESCALATED: Severity.URGENT,
    EventType.DRONE_ACTION: Severity.INFO,
    EventType.QUEEN_RESPONSE: Severity.INFO,
    EventType.TASK_ASSIGNED: Severity.INFO,
    EventType.TASK_COMPLETED: Severity.INFO,
}


@dataclass
class NotifyEvent:
    event_type: EventType
    title: str
    message: str
    severity: Severity = Severity.INFO
    worker_name: str | None = None
    timestamp: float = field(default_factory=time.time)


# Backend protocol: any callable that takes a NotifyEvent
NotifyBackend = Callable[[NotifyEvent], None]


class NotificationBus:
    """Central event bus for routing notifications to backends."""

    def __init__(self, debounce_seconds: float = 5.0) -> None:
        self._backends: list[NotifyBackend] = []
        self._debounce = debounce_seconds
        self._last_sent: dict[str, float] = {}

    def add_backend(self, backend: NotifyBackend) -> None:
        self._backends.append(backend)

    def emit(self, event: NotifyEvent) -> None:
        """Emit an event to all registered backends, with debouncing."""
        key = f"{event.event_type.value}:{event.worker_name or ''}"
        now = time.time()
        last = self._last_sent.get(key, 0.0)

        if now - last < self._debounce:
            _log.debug("debounced notification: %s", key)
            return

        self._last_sent[key] = now
        _log.info("notify: %s — %s", event.event_type.value, event.title)

        for backend in self._backends:
            try:
                backend(event)
            except Exception:
                _log.warning("notification backend failed", exc_info=True)

    def emit_worker_idle(self, worker_name: str) -> None:
        self.emit(NotifyEvent(
            event_type=EventType.WORKER_IDLE,
            title=f"{worker_name} is idle",
            message=f"Worker {worker_name} is waiting for input",
            severity=Severity.INFO,
            worker_name=worker_name,
        ))

    def emit_worker_stung(self, worker_name: str) -> None:
        self.emit(NotifyEvent(
            event_type=EventType.WORKER_STUNG,
            title=f"{worker_name} exited",
            message=f"Worker {worker_name} has exited unexpectedly",
            severity=Severity.WARNING,
            worker_name=worker_name,
        ))

    def emit_escalation(self, worker_name: str, reason: str) -> None:
        self.emit(NotifyEvent(
            event_type=EventType.WORKER_ESCALATED,
            title=f"{worker_name} escalated",
            message=f"Drones escalated {worker_name}: {reason}",
            severity=Severity.URGENT,
            worker_name=worker_name,
        ))

    def emit_task_assigned(self, worker_name: str, task_title: str) -> None:
        self.emit(NotifyEvent(
            event_type=EventType.TASK_ASSIGNED,
            title=f"Task → {worker_name}",
            message=f"Assigned '{task_title}' to {worker_name}",
            severity=Severity.INFO,
            worker_name=worker_name,
        ))

    def emit_task_completed(self, worker_name: str, task_title: str) -> None:
        self.emit(NotifyEvent(
            event_type=EventType.TASK_COMPLETED,
            title=f"Task done: {task_title}",
            message=f"{worker_name} completed '{task_title}'",
            severity=Severity.INFO,
            worker_name=worker_name,
        ))
