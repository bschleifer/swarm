"""System Log — structured action log for drones, queen, tasks, and system events."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from swarm.events import EventEmitter
from swarm.logging import get_logger

_log = get_logger("drones.log")

_DEFAULT_LOG_PATH = Path.home() / ".swarm" / "system.jsonl"
_LEGACY_LOG_PATH = Path.home() / ".swarm" / "drone.jsonl"
_DEFAULT_MAX_FILE_SIZE = 5 * 1024 * 1024  # 5MB
_DEFAULT_MAX_ROTATIONS = 2


class LogCategory(Enum):
    DRONE = "drone"
    TASK = "task"
    QUEEN = "queen"
    WORKER = "worker"
    SYSTEM = "system"
    OPERATOR = "operator"


class DroneAction(Enum):
    CONTINUED = "CONTINUED"
    REVIVED = "REVIVED"
    ESCALATED = "ESCALATED"
    OPERATOR = "OPERATOR"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    AUTO_ASSIGNED = "AUTO_ASSIGNED"
    PROPOSED_ASSIGNMENT = "PROPOSED_ASSIGNMENT"
    PROPOSED_COMPLETION = "PROPOSED_COMPLETION"
    PROPOSED_MESSAGE = "PROPOSED_MESSAGE"
    QUEEN_CONTINUED = "QUEEN_CONTINUED"
    QUEEN_PROPOSED_DONE = "QUEEN_PROPOSED_DONE"


class SystemAction(Enum):
    # Drone actions (superset)
    CONTINUED = "CONTINUED"
    REVIVED = "REVIVED"
    ESCALATED = "ESCALATED"
    OPERATOR = "OPERATOR"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    AUTO_ASSIGNED = "AUTO_ASSIGNED"
    PROPOSED_ASSIGNMENT = "PROPOSED_ASSIGNMENT"
    PROPOSED_COMPLETION = "PROPOSED_COMPLETION"
    PROPOSED_MESSAGE = "PROPOSED_MESSAGE"
    QUEEN_CONTINUED = "QUEEN_CONTINUED"
    QUEEN_PROPOSED_DONE = "QUEEN_PROPOSED_DONE"
    # Task events
    TASK_CREATED = "TASK_CREATED"
    TASK_ASSIGNED = "TASK_ASSIGNED"
    TASK_COMPLETED = "TASK_COMPLETED"
    TASK_FAILED = "TASK_FAILED"
    TASK_REMOVED = "TASK_REMOVED"
    TASK_SEND_FAILED = "TASK_SEND_FAILED"
    # Queen events
    QUEEN_PROPOSAL = "QUEEN_PROPOSAL"
    QUEEN_AUTO_ACTED = "QUEEN_AUTO_ACTED"
    QUEEN_ESCALATION = "QUEEN_ESCALATION"
    QUEEN_COMPLETION = "QUEEN_COMPLETION"
    # Worker events
    WORKER_STUNG = "WORKER_STUNG"
    # System events
    DRAFT_OK = "DRAFT_OK"
    DRAFT_FAILED = "DRAFT_FAILED"
    CONFIG_CHANGED = "CONFIG_CHANGED"


# Map DroneAction values to SystemAction for interop
_DRONE_TO_SYSTEM: dict[str, SystemAction] = {a.value: SystemAction(a.value) for a in DroneAction}


@dataclass
class DroneEntry:
    timestamp: float
    action: DroneAction
    worker_name: str
    detail: str = ""

    @property
    def formatted_time(self) -> str:
        return time.strftime("%I:%M:%S %p", time.localtime(self.timestamp))

    @property
    def display(self) -> str:
        parts = [self.formatted_time, self.action.value, self.worker_name]
        if self.detail:
            parts.append(f"({self.detail})")
        return " ".join(parts)


@dataclass
class SystemEntry:
    timestamp: float
    action: SystemAction
    worker_name: str
    detail: str = ""
    category: LogCategory = field(default=LogCategory.DRONE)
    is_notification: bool = False
    metadata: dict[str, object] = field(default_factory=dict)

    @property
    def formatted_time(self) -> str:
        return time.strftime("%I:%M:%S %p", time.localtime(self.timestamp))

    @property
    def display(self) -> str:
        parts = [self.formatted_time, self.action.value, self.worker_name]
        if self.detail:
            parts.append(f"({self.detail})")
        return " ".join(parts)


def _parse_action(value: str) -> SystemAction:
    """Parse an action string into SystemAction, tolerating old DroneAction values."""
    try:
        return SystemAction(value)
    except ValueError:
        return SystemAction.OPERATOR  # safe fallback


def _parse_category(value: str | None) -> LogCategory:
    """Parse a category string, defaulting to DRONE for legacy entries."""
    if not value:
        return LogCategory.DRONE
    try:
        return LogCategory(value)
    except ValueError:
        return LogCategory.DRONE


class SystemLog(EventEmitter):
    def __init__(
        self,
        max_entries: int = 200,
        log_file: Path | None = None,
        max_file_size: int = _DEFAULT_MAX_FILE_SIZE,
        max_rotations: int = _DEFAULT_MAX_ROTATIONS,
    ) -> None:
        self.__init_emitter__()
        self._entries: list[SystemEntry] = []
        self._max = max_entries
        self._log_file = log_file
        self._max_file_size = max_file_size
        self._max_rotations = max_rotations
        if self._log_file:
            self._load_history()

    def _load_history(self) -> None:
        """Load last N entries from JSONL file on startup.

        Performs one-time migration: if the configured log file doesn't exist
        but the legacy drone.jsonl does, load from the legacy file instead.
        """
        load_path = self._log_file
        if load_path and not load_path.exists():
            # One-time migration from legacy drone.jsonl
            legacy = load_path.parent / "drone.jsonl"
            if legacy.exists():
                load_path = legacy
                _log.info("migrating legacy drone.jsonl → %s", self._log_file)

        if not load_path or not load_path.exists():
            return
        try:
            lines = load_path.read_text().strip().splitlines()
            for line in lines[-self._max :]:
                try:
                    d = json.loads(line)
                    entry = SystemEntry(
                        timestamp=d["timestamp"],
                        action=_parse_action(d["action"]),
                        worker_name=d["worker_name"],
                        detail=d.get("detail", ""),
                        category=_parse_category(d.get("category")),
                        is_notification=d.get("is_notification", False),
                        metadata=d.get("metadata", {}),
                    )
                    self._entries.append(entry)
                except (json.JSONDecodeError, KeyError, ValueError):
                    continue
            _log.info(
                "loaded %d system log entries from %s",
                len(self._entries),
                load_path,
            )
        except OSError:
            _log.warning("failed to load system log from %s", load_path, exc_info=True)

    def _append_to_file(self, entry: SystemEntry) -> None:
        """Append a single entry to the JSONL log file.

        Offloads the blocking file I/O to a thread when an event loop is
        running, keeping the main async loop unblocked.
        """
        if not self._log_file:
            return
        try:
            loop = asyncio.get_running_loop()
            loop.call_soon_threadsafe(
                lambda: asyncio.ensure_future(asyncio.to_thread(self._write_entry, entry))
            )
        except RuntimeError:
            # No event loop — write synchronously (startup / tests)
            self._write_entry(entry)

    def _write_entry(self, entry: SystemEntry) -> None:
        """Synchronously write a log entry to the JSONL file."""
        if not self._log_file:
            return
        try:
            self._log_file.parent.mkdir(parents=True, exist_ok=True)
            record: dict[str, object] = {
                "timestamp": entry.timestamp,
                "action": entry.action.value,
                "worker_name": entry.worker_name,
                "detail": entry.detail,
                "category": entry.category.value,
                "is_notification": entry.is_notification,
            }
            if entry.metadata:
                record["metadata"] = entry.metadata
            line = json.dumps(record)
            with open(self._log_file, "a") as f:
                f.write(line + "\n")
            self._rotate_if_needed()
        except OSError:
            _log.warning("failed to append to system log %s", self._log_file, exc_info=True)

    def _rotate_if_needed(self) -> None:
        """Rotate log file if it exceeds max size."""
        if not self._log_file or not self._log_file.exists():
            return
        try:
            if self._log_file.stat().st_size <= self._max_file_size:
                return
            for i in range(self._max_rotations, 0, -1):
                src = self._log_file.with_suffix(f".jsonl.{i}") if i > 0 else self._log_file
                if i == self._max_rotations:
                    rotated = self._log_file.with_suffix(f".jsonl.{i}")
                    if rotated.exists():
                        rotated.unlink()
                    continue
                dst = self._log_file.with_suffix(f".jsonl.{i + 1}")
                if src.exists():
                    src.rename(dst)
            if self._log_file.exists():
                self._log_file.rename(self._log_file.with_suffix(".jsonl.1"))
            _log.info("rotated system log %s", self._log_file)
        except OSError:
            _log.warning("failed to rotate system log", exc_info=True)

    def add(
        self,
        action: DroneAction | SystemAction,
        worker_name: str,
        detail: str = "",
        *,
        category: LogCategory | None = None,
        is_notification: bool = False,
        metadata: dict[str, object] | None = None,
    ) -> SystemEntry:
        # Convert DroneAction to SystemAction
        if isinstance(action, DroneAction):
            sys_action = _DRONE_TO_SYSTEM[action.value]
            resolved_category = category or LogCategory.DRONE
        else:
            sys_action = action
            resolved_category = category or LogCategory.SYSTEM

        entry = SystemEntry(
            timestamp=time.time(),
            action=sys_action,
            worker_name=worker_name,
            detail=detail,
            category=resolved_category,
            is_notification=is_notification,
            metadata=metadata or {},
        )
        self._entries.append(entry)
        if len(self._entries) > self._max:
            self._entries = self._entries[-self._max :]
        self._append_to_file(entry)
        self.emit("entry", entry)
        return entry

    def on_entry(self, callback: Callable[[SystemEntry], None]) -> None:
        self.on("entry", callback)

    def clear(self) -> None:
        """Clear all entries from memory and truncate the log file."""
        self._entries.clear()
        if self._log_file and self._log_file.exists():
            self._log_file.write_text("")
        self.emit("clear")

    def clear_since(self, since: float) -> int:
        """Remove all entries with timestamp >= *since*. Returns count removed."""
        before = len(self._entries)
        self._entries = [e for e in self._entries if e.timestamp < since]
        removed = before - len(self._entries)
        if removed:
            self.emit("clear")
        return removed

    @property
    def entries(self) -> list[SystemEntry]:
        return list(self._entries)

    @property
    def drone_entries(self) -> list[SystemEntry]:
        """Return only drone-category entries."""
        return [e for e in self._entries if e.category == LogCategory.DRONE]

    @property
    def notification_entries(self) -> list[SystemEntry]:
        """Return only notification-worthy entries."""
        return [e for e in self._entries if e.is_notification]

    @property
    def last(self) -> SystemEntry | None:
        return self._entries[-1] if self._entries else None


# Backward-compat alias
DroneLog = SystemLog
