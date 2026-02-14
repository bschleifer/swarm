"""Worker dataclass — represents a single Claude Code agent in a pane."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum


class WorkerState(Enum):
    BUZZING = "BUZZING"  # Actively working (Claude processing)
    WAITING = "WAITING"  # Actionable prompt (choice/plan/empty) — needs attention
    RESTING = "RESTING"  # Idle, waiting for input
    SLEEPING = "SLEEPING"  # Display-only: RESTING for >= SLEEPING_THRESHOLD
    STUNG = "STUNG"  # Exited / crashed

    @property
    def indicator(self) -> str:
        return {
            "BUZZING": ".",
            "WAITING": "?",
            "RESTING": "~",
            "SLEEPING": "z",
            "STUNG": "!",
        }[self.value]

    @property
    def display(self) -> str:
        return self.value.lower()


# Workers RESTING for longer than this become SLEEPING (display-only).
SLEEPING_THRESHOLD = 300.0  # 5 minutes


def format_duration(seconds: float) -> str:
    """Format a duration as a compact human-readable string."""
    seconds = max(0.0, seconds)
    if seconds < 60:
        return f"{int(seconds)}s"
    if seconds < 3600:
        return f"{int(seconds // 60)}m"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h"
    return f"{int(seconds // 86400)}d"


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
    _revive_at: float = field(default=0.0, repr=False)

    # How long after a revive to ignore STUNG readings (seconds).
    _REVIVE_GRACE: float = 15.0

    def update_state(self, new_state: WorkerState) -> bool:
        """Update state, return True if state changed.

        Applies hysteresis: requires 2 consecutive RESTING/WAITING readings
        before accepting a BUZZING→RESTING or BUZZING→WAITING transition
        (prevents flicker).

        After a revive, ignores STUNG readings for ``_REVIVE_GRACE`` seconds
        so Claude has time to start before the poll loop re-marks the pane.
        """
        # Grace period: ignore STUNG right after a revive
        if (
            new_state == WorkerState.STUNG
            and self._revive_at > 0
            and time.time() - self._revive_at < self._REVIVE_GRACE
        ):
            return False

        _idle_states = (WorkerState.RESTING, WorkerState.WAITING)
        if new_state in _idle_states and self.state == WorkerState.BUZZING:
            self._resting_confirmations += 1
            if self._resting_confirmations < 2:
                return False
        if new_state not in _idle_states:
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
        self._revive_at = time.time()

    @property
    def resting_duration(self) -> float:
        if self.state in (WorkerState.RESTING, WorkerState.WAITING):
            return time.time() - self.state_since
        return 0.0

    @property
    def state_duration(self) -> float:
        """How long the worker has been in its current state."""
        return time.time() - self.state_since

    @property
    def display_state(self) -> WorkerState:
        """State for display purposes: RESTING becomes SLEEPING after threshold."""
        if self.state == WorkerState.RESTING and self.state_duration >= SLEEPING_THRESHOLD:
            return WorkerState.SLEEPING
        return self.state


def worker_state_counts(workers: list[Worker]) -> dict[str, int]:
    """Count workers by display state."""
    buzzing = sum(1 for w in workers if w.display_state == WorkerState.BUZZING)
    waiting = sum(1 for w in workers if w.display_state == WorkerState.WAITING)
    resting = sum(1 for w in workers if w.display_state == WorkerState.RESTING)
    sleeping = sum(1 for w in workers if w.display_state == WorkerState.SLEEPING)
    stung = sum(1 for w in workers if w.display_state == WorkerState.STUNG)
    return {
        "total": len(workers),
        "buzzing": buzzing,
        "waiting": waiting,
        "resting": resting,
        "sleeping": sleeping,
        "stung": stung,
    }
