"""PressureManager — resource pressure response for the drone pilot."""

from __future__ import annotations

import asyncio
import math
import time
from typing import TYPE_CHECKING

from swarm.drones.log import LogCategory, SystemAction
from swarm.logging import get_logger
from swarm.worker.worker import Worker

if TYPE_CHECKING:
    from collections.abc import Callable

    from swarm.drones.log import DroneLog
    from swarm.resources.monitor import MemoryPressureLevel

_log = get_logger("drones.pressure")


# Task #236: minimum seconds between leaving nominal and re-entering
# HIGH/CRITICAL. The resource monitor samples every few seconds, and
# transient mem-pct jitters right at a threshold boundary produced
# 10–13 SUSPEND/RESUME cycles during a single npm install + deploy
# turn. Hysteresis suppresses the re-entry path until the system has
# been nominal for at least this long.
_HYSTERESIS_SECONDS = 30.0


class PressureManager:
    """Handles resource pressure response: suspend/resume workers.

    Extracted from :class:`~swarm.drones.pilot.DronePilot` to reduce
    pilot.py complexity.
    """

    def __init__(
        self,
        workers: list[Worker],
        log: DroneLog,
        pool: object | None,
        suspended: set[str],
        suspended_at: dict[str, float],
        emit: Callable[..., None],
        wake_worker: Callable[[str], bool] | None = None,
    ) -> None:
        self.workers = workers
        self.log = log
        self.pool = pool
        self._suspended = suspended
        self._suspended_at = suspended_at
        self._emit = emit
        # Callback into the state tracker's wake_worker (task #233).
        # Needed so pressure RESUME clears the content fingerprint
        # cache — without it, the RESTING short-circuit in the state
        # tracker keeps the worker tagged RESTING even after the PTY
        # shows "esc to interrupt" again. Falls back to a direct
        # suspended-set discard when no callback is wired (tests /
        # legacy callers).
        self._wake_worker = wake_worker
        # Resource pressure response
        self._pressure_level: str = "nominal"
        self._suspended_for_pressure: set[str] = set()
        # Task #236: track latest measured mem/swap values so SUSPEND and
        # RESUMED buzz entries report the actual pressure numbers that
        # triggered them. Operator flagged that oscillation logs were
        # useless without the underlying values.
        self._last_mem_pct: float | None = None
        self._last_swap_pct: float | None = None
        # Task #236: timestamp of the most recent RESUME (HIGH → NOMINAL).
        # Used by ``on_pressure_changed`` to suppress re-entry into HIGH/
        # CRITICAL within the hysteresis window.
        self._last_resume_at: float = 0.0

    @property
    def pressure_level(self) -> str:
        """Return the current pressure level string."""
        return self._pressure_level

    @property
    def pressure_suspended_workers(self) -> list[str]:
        """Return sorted list of workers currently suspended due to resource pressure."""
        return sorted(self._suspended_for_pressure)

    def _signal_worker_async(self, name: str, sig: int) -> None:
        """Send a signal to a worker via the pool, fire-and-forget."""
        if not self.pool:
            return
        try:
            loop = asyncio.get_running_loop()
            task = loop.create_task(self.pool.signal_worker(name, sig))
            task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)
        except RuntimeError:
            pass

    def _suspend_workers(self, names: list[str], reason: str) -> int:
        """Mark workers as pressure-suspended (soft -- no SIGTSTP).

        Workers are skipped in the poll loop but their processes continue
        running so they can wind down naturally.

        Returns the number of workers newly suspended.
        """
        measured = self._measured_pressure_suffix()
        count = 0
        for name in names:
            if name in self._suspended_for_pressure:
                continue
            _log.warning("pressure %s%s -- suspending worker: %s", reason, measured, name)
            self.log.add(
                SystemAction.SUSPENDED,
                name,
                f"pressure {reason}{measured}",
                category=LogCategory.SYSTEM,
            )
            self._suspended_for_pressure.add(name)
            self._suspended.add(name)
            self._suspended_at[name] = time.time()
            count += 1
        return count

    def _measured_pressure_suffix(self) -> str:
        """Format the measured mem/swap values for SUSPEND/RESUMED logs.

        Returns an empty string when no snapshot has been observed yet
        (legacy tests / cold start). Format: `` (mem=NN% swap=NN%)``
        so the existing log detail stays readable and callers can grep
        for specific thresholds later.
        """
        mem = self._last_mem_pct
        swap = self._last_swap_pct
        if mem is None and swap is None:
            return ""
        parts = []
        if mem is not None:
            parts.append(f"mem={mem:.0f}%")
        if swap is not None:
            parts.append(f"swap={swap:.0f}%")
        return f" ({' '.join(parts)})"

    def on_pressure_changed(
        self,
        level: MemoryPressureLevel,
        *,
        mem_pct: float | None = None,
        swap_pct: float | None = None,
    ) -> None:
        """Respond to a change in system resource pressure.

        ``mem_pct`` / ``swap_pct`` are the measured values that caused
        the level transition, captured on SUSPEND and RESUMED log
        entries (task #236). Hysteresis: after a RESUME, ignore re-
        entry into HIGH/CRITICAL for ``_HYSTERESIS_SECONDS`` so
        threshold-boundary jitter doesn't produce 10+ SUSPEND/RESUME
        cycles per turn.
        """
        level_str = level.value if hasattr(level, "value") else str(level)
        self._pressure_level = level_str
        if mem_pct is not None:
            self._last_mem_pct = mem_pct
        if swap_pct is not None:
            self._last_swap_pct = swap_pct

        if level_str == "nominal":
            self._resume_pressure_suspended()

        elif level_str == "elevated":
            _log.warning("resource pressure ELEVATED -- monitoring")
            self._resume_pressure_suspended()

        elif level_str in ("high", "critical"):
            if self._within_hysteresis_window():
                _log.info(
                    "pressure %s within hysteresis window (%.0fs since RESUME) — suppressing",
                    level_str.upper(),
                    time.time() - self._last_resume_at,
                )
                return
            if level_str == "high":
                self._suspend_on_high_pressure()
            else:
                self._suspend_on_critical_pressure()

    def _within_hysteresis_window(self) -> bool:
        """True when the last RESUME was too recent to allow re-suspend.

        A zero / default ``_last_resume_at`` means "no prior resume on
        this process" and always returns False (don't throttle the
        first HIGH event of the daemon's life).
        """
        if self._last_resume_at <= 0.0:
            return False
        return (time.time() - self._last_resume_at) < _HYSTERESIS_SECONDS

    def _resume_pressure_suspended(self) -> None:
        """Resume workers that were suspended due to pressure (soft -- no SIGCONT).

        Task #233: goes through ``wake_worker`` (when wired) instead of
        discarding directly so the state tracker's content-fingerprint
        cache is cleared too. Without that, a worker whose PTY state
        changed while suspended (e.g. idle → actively running a Bash
        tool) kept the pre-suspend fingerprint, hit the RESTING
        short-circuit on the next poll, and never re-classified as
        BUZZING — surfacing as the "RESTING while demonstrably
        mid-turn" bug in the operator dashboard.
        """
        if not self._suspended_for_pressure:
            # Still record the resume timestamp so a future HIGH→NOMINAL
            # transition that has no suspended workers still primes the
            # hysteresis guard. Without this, a worker that was never
            # suspended but happened to be RESTING during a brief HIGH
            # spike would still be vulnerable to immediate re-suspend.
            self._last_resume_at = time.time()
            return
        count = len(self._suspended_for_pressure)
        measured = self._measured_pressure_suffix()
        _log.info("pressure nominal%s -- resuming %d workers", measured, count)
        for name in list(self._suspended_for_pressure):
            self.log.add(
                SystemAction.RESUMED,
                name,
                f"pressure nominal{measured}",
                category=LogCategory.SYSTEM,
            )
            if self._wake_worker is not None:
                self._wake_worker(name)
            else:
                self._suspended.discard(name)
                self._suspended_at.pop(name, None)
        self._suspended_for_pressure.clear()
        self._last_resume_at = time.time()
        self._emit("workers_changed")

    def _suspend_on_high_pressure(self) -> None:
        """Suspend SLEEPING workers to target 60% active.

        Only SLEEPING workers are eligible -- RESTING workers may be
        between steps and should not be interrupted.
        """
        total = len(self.workers)
        target_active = math.ceil(total * 0.6)
        sleeping = sorted(
            [w for w in self.workers if w.display_state.value == "SLEEPING"],
            key=lambda w: -(w.state_duration or 0),
        )
        to_suspend = total - target_active
        names = [w.name for w in sleeping[:to_suspend]]
        if self._suspend_workers(names, "HIGH"):
            self._emit("workers_changed")

    def _suspend_on_critical_pressure(self) -> None:
        """Suspend SLEEPING/RESTING workers except the most recently active."""
        candidates = [w for w in self.workers if w.display_state.value in ("SLEEPING", "RESTING")]
        if not candidates:
            return
        most_recent = max(candidates, key=lambda w: w.state_since or 0.0)
        names = [w.name for w in candidates if w.name != most_recent.name]
        if self._suspend_workers(names, "CRITICAL"):
            self._emit("workers_changed")
