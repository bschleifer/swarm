"""Worker dataclass — represents a single Claude Code agent in a pane."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum


class WorkerState(Enum):
    BUZZING = "BUZZING"  # Actively working (Claude processing)
    RESTING = "RESTING"  # Idle, waiting for input
    STUNG = "STUNG"  # Exited / crashed

    @property
    def indicator(self) -> str:
        return {"BUZZING": ".", "RESTING": "~", "STUNG": "!"}[self.value]

    @property
    def display(self) -> str:
        return self.value.lower()


@dataclass
class Worker:
    name: str
    path: str
    pane_id: str
    window_index: str = "0"
    state: WorkerState = WorkerState.BUZZING
    state_since: float = field(default_factory=time.time)
    revive_count: int = field(default=0, repr=False)
    _resting_confirmations: int = field(default=0, repr=False)

    def update_state(self, new_state: WorkerState) -> bool:
        """Update state, return True if state changed.

        Applies hysteresis: requires 2 consecutive RESTING readings
        before accepting a BUZZING→RESTING transition (prevents flicker).
        """
        if new_state == WorkerState.RESTING and self.state == WorkerState.BUZZING:
            self._resting_confirmations += 1
            if self._resting_confirmations < 2:
                return False
        if new_state != WorkerState.RESTING:
            self._resting_confirmations = 0
        if self.state != new_state:
            # Reset revive count when worker starts working successfully
            if new_state == WorkerState.BUZZING and self.state != WorkerState.BUZZING:
                self.revive_count = 0
            self.state = new_state
            self.state_since = time.time()
            self._resting_confirmations = 0
            return True
        return False

    def record_revive(self) -> None:
        """Record a revive attempt."""
        self.revive_count += 1

    @property
    def resting_duration(self) -> float:
        if self.state == WorkerState.RESTING:
            return time.time() - self.state_since
        return 0.0

    @property
    def state_duration(self) -> float:
        """How long the worker has been in its current state."""
        return time.time() - self.state_since


def worker_state_counts(workers: list[Worker]) -> dict[str, int]:
    """Count workers by state. Returns dict with total, buzzing, resting, stung."""
    buzzing = sum(1 for w in workers if w.state == WorkerState.BUZZING)
    resting = sum(1 for w in workers if w.state == WorkerState.RESTING)
    stung = sum(1 for w in workers if w.state == WorkerState.STUNG)
    return {"total": len(workers), "buzzing": buzzing, "resting": resting, "stung": stung}
