"""Notification bus — event routing for swarm notifications."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum

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
    RESOURCE_PRESSURE = "resource_pressure"
    DSTATE_DETECTED = "dstate_detected"
    CONTEXT_PRESSURE = "context_pressure"


class Severity(Enum):
    INFO = "info"
    WARNING = "warning"
    URGENT = "urgent"


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
            except (OSError, TimeoutError, ConnectionError):
                _log.warning("notification backend %s failed", backend, exc_info=True)
            except Exception:
                _log.warning("unexpected error in notification backend %s", backend, exc_info=True)

    def emit_worker_idle(self, worker_name: str) -> None:
        self.emit(
            NotifyEvent(
                event_type=EventType.WORKER_IDLE,
                title=f"{worker_name} is idle",
                message=f"Worker {worker_name} is waiting for input",
                severity=Severity.INFO,
                worker_name=worker_name,
            )
        )

    def emit_worker_stung(self, worker_name: str) -> None:
        self.emit(
            NotifyEvent(
                event_type=EventType.WORKER_STUNG,
                title=f"{worker_name} exited",
                message=f"Worker {worker_name} has exited unexpectedly",
                severity=Severity.WARNING,
                worker_name=worker_name,
            )
        )

    def emit_escalation(self, worker_name: str, reason: str) -> None:
        self.emit(
            NotifyEvent(
                event_type=EventType.WORKER_ESCALATED,
                title=f"{worker_name} escalated",
                message=f"Drones escalated {worker_name}: {reason}",
                severity=Severity.URGENT,
                worker_name=worker_name,
            )
        )

    def emit_task_assigned(self, worker_name: str, task_title: str) -> None:
        self.emit(
            NotifyEvent(
                event_type=EventType.TASK_ASSIGNED,
                title=f"Task → {worker_name}",
                message=f"Assigned '{task_title}' to {worker_name}",
                severity=Severity.INFO,
                worker_name=worker_name,
            )
        )

    def emit_task_completed(self, worker_name: str, task_title: str) -> None:
        self.emit(
            NotifyEvent(
                event_type=EventType.TASK_COMPLETED,
                title=f"Task done: {task_title}",
                message=f"{worker_name} completed '{task_title}'",
                severity=Severity.INFO,
                worker_name=worker_name,
            )
        )

    def emit_resource_pressure(self, level: str, mem_pct: float, swap_pct: float) -> None:
        self.emit(
            NotifyEvent(
                event_type=EventType.RESOURCE_PRESSURE,
                title=f"Memory pressure: {level}",
                message=f"Memory {mem_pct:.0f}% / Swap {swap_pct:.0f}% — pressure level: {level}",
                severity=Severity.WARNING if level != "critical" else Severity.URGENT,
            )
        )

    def emit_dstate_detected(self, pid: int, comm: str, worker_name: str) -> None:
        self.emit(
            NotifyEvent(
                event_type=EventType.DSTATE_DETECTED,
                title=f"D-state process: {comm}",
                message=f"PID {pid} ({comm}) in uninterruptible sleep under {worker_name}",
                severity=Severity.URGENT,
                worker_name=worker_name,
            )
        )

    def emit_context_pressure(self, worker_name: str, usage_pct: float, level: str) -> None:
        severity = Severity.URGENT if level == "critical" else Severity.WARNING
        self.emit(
            NotifyEvent(
                event_type=EventType.CONTEXT_PRESSURE,
                title=f"Context {level}: {worker_name}",
                message=(
                    f"Worker {worker_name} context window at {usage_pct:.0%}"
                    f" — pressure level: {level}"
                ),
                severity=severity,
                worker_name=worker_name,
            )
        )
