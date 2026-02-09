"""Drone Log — structured action log for background drones activity."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from swarm.events import EventEmitter
from swarm.logging import get_logger

_log = get_logger("drones.log")

_DEFAULT_LOG_PATH = Path.home() / ".swarm" / "drone.jsonl"
_DEFAULT_MAX_FILE_SIZE = 5 * 1024 * 1024  # 5MB
_DEFAULT_MAX_ROTATIONS = 2


class DroneAction(Enum):
    CONTINUED = "CONTINUED"
    REVIVED = "REVIVED"
    ESCALATED = "ESCALATED"


@dataclass
class DroneEntry:
    timestamp: float
    action: DroneAction
    worker_name: str
    detail: str = ""

    @property
    def formatted_time(self) -> str:
        return time.strftime("%H:%M", time.localtime(self.timestamp))

    @property
    def display(self) -> str:
        parts = [self.formatted_time, self.action.value, self.worker_name]
        if self.detail:
            parts.append(f"({self.detail})")
        return " ".join(parts)


class DroneLog(EventEmitter):
    def __init__(
        self,
        max_entries: int = 200,
        log_file: Path | None = None,
        max_file_size: int = _DEFAULT_MAX_FILE_SIZE,
        max_rotations: int = _DEFAULT_MAX_ROTATIONS,
    ) -> None:
        self.__init_emitter__()
        self._entries: list[DroneEntry] = []
        self._max = max_entries
        self._log_file = log_file
        self._max_file_size = max_file_size
        self._max_rotations = max_rotations
        if self._log_file:
            self._load_history()

    def _load_history(self) -> None:
        """Load last N entries from JSONL file on startup."""
        if not self._log_file or not self._log_file.exists():
            return
        try:
            lines = self._log_file.read_text().strip().splitlines()
            for line in lines[-self._max :]:
                try:
                    d = json.loads(line)
                    entry = DroneEntry(
                        timestamp=d["timestamp"],
                        action=DroneAction(d["action"]),
                        worker_name=d["worker_name"],
                        detail=d.get("detail", ""),
                    )
                    self._entries.append(entry)
                except (json.JSONDecodeError, KeyError, ValueError):
                    continue
            _log.info("loaded %d drone log entries from %s", len(self._entries), self._log_file)
        except OSError:
            _log.warning("failed to load drone log from %s", self._log_file, exc_info=True)

    def _append_to_file(self, entry: DroneEntry) -> None:
        """Append a single entry to the JSONL log file."""
        if not self._log_file:
            return
        try:
            self._log_file.parent.mkdir(parents=True, exist_ok=True)
            line = json.dumps(
                {
                    "timestamp": entry.timestamp,
                    "action": entry.action.value,
                    "worker_name": entry.worker_name,
                    "detail": entry.detail,
                }
            )
            with open(self._log_file, "a") as f:
                f.write(line + "\n")
            self._rotate_if_needed()
        except OSError:
            _log.warning("failed to append to drone log %s", self._log_file, exc_info=True)

    def _rotate_if_needed(self) -> None:
        """Rotate log file if it exceeds max size."""
        if not self._log_file or not self._log_file.exists():
            return
        try:
            if self._log_file.stat().st_size <= self._max_file_size:
                return
            # Rotate: drone.jsonl -> drone.jsonl.1 -> drone.jsonl.2
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
            # Rename current to .1
            if self._log_file.exists():
                self._log_file.rename(self._log_file.with_suffix(".jsonl.1"))
            _log.info("rotated drone log %s", self._log_file)
        except OSError:
            _log.warning("failed to rotate drone log", exc_info=True)

    def add(self, action: DroneAction, worker_name: str, detail: str = "") -> DroneEntry:
        entry = DroneEntry(
            timestamp=time.time(),
            action=action,
            worker_name=worker_name,
            detail=detail,
        )
        self._entries.append(entry)
        if len(self._entries) > self._max:
            self._entries = self._entries[-self._max :]
        self._append_to_file(entry)
        self.emit("entry", entry)
        return entry

    def on_entry(self, callback) -> None:
        self.on("entry", callback)

    def clear(self) -> None:
        """Clear all entries from memory and truncate the log file."""
        self._entries.clear()
        if self._log_file and self._log_file.exists():
            self._log_file.write_text("")
        self.emit("clear")

    @property
    def entries(self) -> list[DroneEntry]:
        return list(self._entries)

    @property
    def last(self) -> DroneEntry | None:
        return self._entries[-1] if self._entries else None
