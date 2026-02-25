"""Worker dataclass — represents a single Claude Code agent."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, TypedDict

if TYPE_CHECKING:
    from swarm.pty.process import WorkerProcess


class WorkerDict(TypedDict):
    """Typed shape of Worker.to_api_dict() output."""

    name: str
    path: str
    provider: str
    worker_id: str
    state: str
    state_duration: float
    revive_count: int
    usage: dict[str, object]


# (indicator, css_class, priority) keyed by state value
_STATE_PROPS: dict[str, tuple[str, str, int]] = {
    "BUZZING": (".", "text-leaf", 2),
    "WAITING": ("?", "text-honey", 1),
    "RESTING": ("~", "text-lavender", 4),
    "SLEEPING": ("z", "text-muted", 3),
    "STUNG": ("!", "text-poppy", 0),
}


class WorkerState(Enum):
    BUZZING = "BUZZING"  # Actively working (Claude processing)
    WAITING = "WAITING"  # Actionable prompt (choice/plan/empty) — needs attention
    RESTING = "RESTING"  # Idle, waiting for input
    SLEEPING = "SLEEPING"  # Display-only: RESTING for >= SLEEPING_THRESHOLD
    STUNG = "STUNG"  # Exited / crashed

    @property
    def indicator(self) -> str:
        return _STATE_PROPS[self.value][0]

    @property
    def display(self) -> str:
        return self.value.lower()

    @property
    def css_class(self) -> str:
        """CSS class for dashboard rendering."""
        return _STATE_PROPS[self.value][1]

    @property
    def priority(self) -> int:
        """Sort priority for group worst-state display (lower = more urgent)."""
        return _STATE_PROPS[self.value][2]


# Workers RESTING for longer than this become SLEEPING (display-only).
SLEEPING_THRESHOLD = 300.0  # 5 minutes

# STUNG workers are auto-removed after this many seconds.
STUNG_REAP_TIMEOUT = 30.0


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
class TokenUsage:
    """Accumulated token usage for a worker or the queen."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    cost_usd: float = 0.0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def add(self, other: TokenUsage) -> None:
        """Accumulate usage from another TokenUsage."""
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.cache_read_tokens += other.cache_read_tokens
        self.cache_creation_tokens += other.cache_creation_tokens
        self.cost_usd += other.cost_usd

    def to_dict(self) -> dict[str, object]:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_creation_tokens": self.cache_creation_tokens,
            "total_tokens": self.total_tokens,
            "cost_usd": round(self.cost_usd, 6),
        }


@dataclass
class Worker:
    name: str
    path: str
    provider_name: str = "claude"
    process: WorkerProcess | None = field(default=None, repr=False)
    state: WorkerState = WorkerState.BUZZING
    state_since: float = field(default_factory=time.time)
    revive_count: int = field(default=0, repr=False)
    usage: TokenUsage = field(default_factory=TokenUsage, repr=False)
    _resting_confirmations: int = field(default=0, repr=False)
    _stung_confirmations: int = field(default=0, repr=False)
    _revive_at: float = field(default=0.0, repr=False)

    # How long after a revive to ignore STUNG readings (seconds).
    _REVIVE_GRACE: float = 15.0

    def update_state(self, new_state: WorkerState) -> bool:
        """Update state, return True if state changed.

        Applies hysteresis: requires 2 consecutive RESTING/WAITING readings
        before accepting a BUZZING→RESTING or BUZZING→WAITING transition
        (prevents flicker).

        After a revive, ignores STUNG readings for ``_REVIVE_GRACE`` seconds
        so Claude has time to start before the poll loop re-marks the worker.
        """
        # Grace period: ignore STUNG right after a revive
        if (
            new_state == WorkerState.STUNG
            and self._revive_at > 0
            and time.time() - self._revive_at < self._REVIVE_GRACE
        ):
            return False

        # STUNG hysteresis: require 2 consecutive STUNG readings to prevent
        # spurious revives when Claude Code briefly exits between operations
        # (shell becomes foreground for one poll cycle).
        if new_state == WorkerState.STUNG:
            self._stung_confirmations += 1
            if self._stung_confirmations < 2:
                return False
        else:
            self._stung_confirmations = 0

        _idle_states = (WorkerState.RESTING, WorkerState.WAITING)
        if new_state in _idle_states and self.state == WorkerState.BUZZING:
            self._resting_confirmations += 1
            if self._resting_confirmations < 3:
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

    def force_state(self, new_state: WorkerState) -> None:
        """Set state directly, bypassing hysteresis and grace period.

        Used when the holder confirms a process death — no debounce needed.
        Clears the revive grace window so STUNG detection isn't suppressed.
        """
        if self.state != new_state:
            if new_state == WorkerState.BUZZING and self.state != WorkerState.BUZZING:
                self.revive_count = 0
            self.state = new_state
            self.state_since = time.time()
            self._resting_confirmations = 0
            self._stung_confirmations = 0
            self._revive_at = 0.0

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

    def to_api_dict(self) -> WorkerDict:
        """Serialize worker state for API/WebSocket responses."""
        return WorkerDict(
            name=self.name,
            path=self.path,
            provider=self.provider_name,
            worker_id=self.name,
            state=self.display_state.value,
            state_duration=round(self.state_duration, 1),
            revive_count=self.revive_count,
            usage=self.usage.to_dict(),
        )


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
