"""System Log — structured action log for drones, queen, tasks, and system events."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from swarm.drones.store import LogStore
from swarm.events import EventEmitter
from swarm.logging import get_logger

_log = get_logger("drones.log")

_DEFAULT_LOG_PATH = Path.home() / ".swarm" / "system.jsonl"
_DEFAULT_MAX_FILE_SIZE = 5 * 1024 * 1024  # 5MB
_DEFAULT_MAX_ROTATIONS = 2
_MAX_PENDING_WRITES = 32  # backpressure cap for async log writes


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
    QUEEN_BLOCKED = "QUEEN_BLOCKED"
    QUEEN_ESCALATION = "QUEEN_ESCALATION"
    QUEEN_COMPLETION = "QUEEN_COMPLETION"
    # Worker events
    WORKER_STUNG = "WORKER_STUNG"
    # Oversight events
    OVERSIGHT_SIGNAL = "OVERSIGHT_SIGNAL"
    OVERSIGHT_INTERVENTION = "OVERSIGHT_INTERVENTION"
    OVERSIGHT_RATE_LIMITED = "OVERSIGHT_RATE_LIMITED"
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
    overridden: bool = False
    override_action: str = ""
    store_id: int | None = None  # SQLite row ID for override tracking

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
        db_path: Path | None = None,
    ) -> None:
        self.__init_emitter__()
        self._entries: list[SystemEntry] = []
        self._max = max_entries
        self._log_file = log_file
        self._max_file_size = max_file_size
        self._max_rotations = max_rotations
        self._write_semaphore = asyncio.Semaphore(_MAX_PENDING_WRITES)

        # SQLite store for queryable analytics (None = disabled)
        self._store: LogStore | None = None
        if db_path is not None:
            self._store = LogStore(db_path=db_path)

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
        running, keeping the main async loop unblocked.  A bounded semaphore
        caps the number of in-flight write tasks to prevent unbounded growth
        when disk I/O is slow.
        """
        if not self._log_file:
            return
        try:
            loop = asyncio.get_running_loop()
            task = loop.create_task(self._write_entry_bounded(entry))
            task.add_done_callback(lambda t: t.result() if not t.cancelled() else None)
        except RuntimeError:
            # No event loop — write synchronously (startup / tests)
            self._write_entry(entry)

    async def _write_entry_bounded(self, entry: SystemEntry) -> None:
        """Write with backpressure — at most _MAX_PENDING_WRITES concurrent."""
        if not self._write_semaphore.locked():
            async with self._write_semaphore:
                await asyncio.to_thread(self._write_entry, entry)
        else:
            # Semaphore full — try to acquire, but drop entry on contention
            acquired = False
            try:
                await asyncio.wait_for(self._write_semaphore.acquire(), timeout=2.0)
                acquired = True
                await asyncio.to_thread(self._write_entry, entry)
            except TimeoutError:
                _log.debug("log write backpressure — dropping entry")
            finally:
                if acquired:
                    self._write_semaphore.release()

    def _write_entry(self, entry: SystemEntry) -> None:
        """Synchronously write a log entry to the JSONL file."""
        import fcntl

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
            # File lock serializes writes + rotation across threads
            with open(self._log_file, "a") as f:
                fcntl.flock(f, fcntl.LOCK_EX)
                try:
                    f.write(line + "\n")
                finally:
                    fcntl.flock(f, fcntl.LOCK_UN)
            self._rotate_if_needed()
        except OSError:
            _log.warning("failed to append to system log %s", self._log_file, exc_info=True)

    def _rotate_if_needed(self) -> None:
        """Rotate log file if it exceeds max size.

        Uses file locking to prevent races when multiple threads rotate
        concurrently.
        """
        import fcntl

        if not self._log_file or not self._log_file.exists():
            return
        try:
            if self._log_file.stat().st_size <= self._max_file_size:
                return
            # Acquire exclusive lock for the rotation operation
            with open(self._log_file, "a") as f:
                fcntl.flock(f, fcntl.LOCK_EX)
                try:
                    # Re-check size under lock (another thread may have rotated)
                    if self._log_file.stat().st_size <= self._max_file_size:
                        return
                    # Delete oldest rotation
                    oldest = self._log_file.with_suffix(f".jsonl.{self._max_rotations}")
                    if oldest.exists():
                        oldest.unlink()
                    # Shift existing rotations up by one
                    for i in range(self._max_rotations - 1, 0, -1):
                        src = self._log_file.with_suffix(f".jsonl.{i}")
                        dst = self._log_file.with_suffix(f".jsonl.{i + 1}")
                        if src.exists():
                            src.rename(dst)
                    # Rotate current file to .1
                    if self._log_file.exists():
                        self._log_file.rename(self._log_file.with_suffix(".jsonl.1"))
                finally:
                    fcntl.flock(f, fcntl.LOCK_UN)
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

        # Write-through to SQLite store
        if self._store is not None:
            entry.store_id = self._store.insert(
                timestamp=entry.timestamp,
                action=entry.action.value,
                worker_name=entry.worker_name,
                detail=entry.detail,
                category=entry.category.value,
                is_notification=entry.is_notification,
                metadata=entry.metadata if entry.metadata else None,
            )

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

    # -- Override tracking (Phase 1/2 foundation) --

    def mark_overridden(self, entry: SystemEntry, override_action: str) -> bool:
        """Mark a log entry as overridden by the user.

        Updates both the in-memory entry and the SQLite store.
        """
        entry.overridden = True
        entry.override_action = override_action
        if self._store is not None and entry.store_id is not None:
            return self._store.mark_overridden(entry.store_id, override_action)
        return True

    def mark_recent_overridden(
        self,
        worker_name: str,
        override_action: str,
        *,
        within_seconds: float = 300.0,
        action_filter: list[str] | None = None,
    ) -> bool:
        """Mark the most recent matching entry for a worker as overridden.

        Searches both in-memory entries and the SQLite store.
        """
        # Update in-memory entry
        now = time.time()
        for entry in reversed(self._entries):
            if entry.worker_name != worker_name:
                continue
            if entry.overridden:
                continue
            if now - entry.timestamp > within_seconds:
                break
            if action_filter and entry.action.value not in action_filter:
                continue
            entry.overridden = True
            entry.override_action = override_action
            break

        # Update SQLite store
        if self._store is not None:
            row_id = self._store.mark_recent_overridden(
                worker_name,
                override_action,
                within_seconds=within_seconds,
                action_filter=action_filter,
            )
            return row_id is not None
        return True

    # -- Query methods (delegate to SQLite store) --

    def query(
        self,
        *,
        worker_name: str | None = None,
        action: str | None = None,
        category: str | None = None,
        since: float | None = None,
        until: float | None = None,
        overridden: bool | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict]:
        """Query log entries with filters.  Requires SQLite store."""
        if self._store is None:
            return []
        return self._store.query(
            worker_name=worker_name,
            action=action,
            category=category,
            since=since,
            until=until,
            overridden=overridden,
            limit=limit,
            offset=offset,
        )

    def query_count(
        self,
        *,
        worker_name: str | None = None,
        action: str | None = None,
        since: float | None = None,
        overridden: bool | None = None,
    ) -> int:
        """Count entries matching filters.  Requires SQLite store."""
        if self._store is None:
            return 0
        return self._store.count(
            worker_name=worker_name,
            action=action,
            since=since,
            overridden=overridden,
        )

    def prune_store(self, max_age_days: int | None = None) -> int:
        """Prune old entries from the SQLite store."""
        if self._store is None:
            return 0
        return self._store.prune(max_age_days)

    @property
    def store(self) -> LogStore | None:
        """Access the underlying SQLite store (if configured)."""
        return self._store


# Backward-compat alias
DroneLog = SystemLog
