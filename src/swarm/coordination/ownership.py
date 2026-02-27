"""File ownership tracking — maps files to the worker that owns them."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from swarm.logging import get_logger

_log = get_logger("coordination.ownership")


class OwnershipMode(Enum):
    """How file ownership violations are handled."""

    OFF = "off"
    WARNING = "warning"
    HARD_BLOCK = "hard-block"


@dataclass
class Overlap:
    """A file touched by a worker that is owned by another."""

    file_path: str
    owner: str
    intruder: str
    timestamp: float = field(default_factory=time.time)


class FileOwnershipMap:
    """Track per-file ownership across workers.

    Ownership is derived from two sources:
    1. Explicit claims (e.g., Queen assigns files during task distribution)
    2. Runtime tracking (conflict detection feeds changed files)
    """

    def __init__(self, mode: OwnershipMode = OwnershipMode.WARNING) -> None:
        self._mode = mode
        # file_path → worker_name
        self._owners: dict[str, str] = {}
        # worker_name → set of owned file paths
        self._worker_files: dict[str, set[str]] = {}
        # Recent overlaps for status reporting
        self._overlaps: list[Overlap] = []

    @property
    def mode(self) -> OwnershipMode:
        return self._mode

    @mode.setter
    def mode(self, value: OwnershipMode) -> None:
        self._mode = value

    def claim(self, worker_name: str, files: set[str]) -> list[Overlap]:
        """Register file ownership for a worker.

        Returns any overlaps detected (files owned by other workers).
        """
        overlaps: list[Overlap] = []

        for f in files:
            existing = self._owners.get(f)
            if existing and existing != worker_name:
                overlap = Overlap(
                    file_path=f,
                    owner=existing,
                    intruder=worker_name,
                )
                overlaps.append(overlap)
                self._overlaps.append(overlap)
                _log.warning(
                    "file overlap: %s owned by %s, touched by %s",
                    f,
                    existing,
                    worker_name,
                )
            else:
                self._owners[f] = worker_name
                self._worker_files.setdefault(worker_name, set()).add(f)

        # Cap overlap history
        if len(self._overlaps) > 100:
            self._overlaps = self._overlaps[-100:]

        return overlaps

    def release(self, worker_name: str) -> int:
        """Release all files owned by a worker. Returns count released."""
        files = self._worker_files.pop(worker_name, set())
        count = 0
        for f in files:
            if self._owners.get(f) == worker_name:
                del self._owners[f]
                count += 1
        return count

    def release_file(self, file_path: str) -> str | None:
        """Release a single file. Returns the previous owner or None."""
        owner = self._owners.pop(file_path, None)
        if owner:
            worker_files = self._worker_files.get(owner)
            if worker_files:
                worker_files.discard(file_path)
        return owner

    def get_owner(self, file_path: str) -> str | None:
        """Get the current owner of a file."""
        return self._owners.get(file_path)

    def get_worker_files(self, worker_name: str) -> set[str]:
        """Get all files owned by a worker."""
        return set(self._worker_files.get(worker_name, set()))

    def check_overlap(self, worker_name: str, files: set[str]) -> list[Overlap]:
        """Check if files would conflict without claiming them."""
        overlaps: list[Overlap] = []
        for f in files:
            existing = self._owners.get(f)
            if existing and existing != worker_name:
                overlaps.append(
                    Overlap(
                        file_path=f,
                        owner=existing,
                        intruder=worker_name,
                    )
                )
        return overlaps

    def update_from_conflicts(self, changed_files: dict[str, set[str]]) -> list[Overlap]:
        """Update ownership from runtime file detection.

        Args:
            changed_files: Mapping of worker_name → set of changed files.

        Returns overlaps where files are touched by non-owners.
        """
        all_overlaps: list[Overlap] = []
        for worker_name, files in changed_files.items():
            overlaps = self.claim(worker_name, files)
            all_overlaps.extend(overlaps)
        return all_overlaps

    def transfer(self, file_path: str, new_owner: str) -> str | None:
        """Transfer ownership of a file. Returns old owner."""
        old_owner = self.release_file(file_path)
        self._owners[file_path] = new_owner
        self._worker_files.setdefault(new_owner, set()).add(file_path)
        return old_owner

    def to_dict(self) -> dict[str, Any]:
        """Serialize for API/WS responses."""
        return {
            "mode": self._mode.value,
            "files": dict(self._owners),
            "workers": {name: sorted(files) for name, files in self._worker_files.items()},
            "recent_overlaps": [
                {
                    "file": o.file_path,
                    "owner": o.owner,
                    "intruder": o.intruder,
                    "timestamp": o.timestamp,
                }
                for o in self._overlaps[-20:]
            ],
        }

    def clear(self) -> None:
        """Clear all ownership data."""
        self._owners.clear()
        self._worker_files.clear()
        self._overlaps.clear()
