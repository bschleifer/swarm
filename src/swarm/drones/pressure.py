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
    ) -> None:
        self.workers = workers
        self.log = log
        self.pool = pool
        self._suspended = suspended
        self._suspended_at = suspended_at
        self._emit = emit
        # Resource pressure response
        self._pressure_level: str = "nominal"
        self._suspended_for_pressure: set[str] = set()

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
        count = 0
        for name in names:
            if name in self._suspended_for_pressure:
                continue
            _log.warning("pressure %s -- suspending worker: %s", reason, name)
            self.log.add(
                SystemAction.SUSPENDED, name, f"pressure {reason}", category=LogCategory.SYSTEM
            )
            self._suspended_for_pressure.add(name)
            self._suspended.add(name)
            self._suspended_at[name] = time.time()
            count += 1
        return count

    def on_pressure_changed(self, level: MemoryPressureLevel) -> None:
        """Respond to a change in system resource pressure."""
        level_str = level.value if hasattr(level, "value") else str(level)
        self._pressure_level = level_str

        if level_str == "nominal":
            self._resume_pressure_suspended()

        elif level_str == "elevated":
            _log.warning("resource pressure ELEVATED -- monitoring")
            self._resume_pressure_suspended()

        elif level_str == "high":
            self._suspend_on_high_pressure()

        elif level_str == "critical":
            self._suspend_on_critical_pressure()

    def _resume_pressure_suspended(self) -> None:
        """Resume workers that were suspended due to pressure (soft -- no SIGCONT)."""
        if not self._suspended_for_pressure:
            return
        count = len(self._suspended_for_pressure)
        _log.info("pressure nominal -- resuming %d workers", count)
        for name in list(self._suspended_for_pressure):
            self.log.add(
                SystemAction.RESUMED, name, "pressure nominal", category=LogCategory.SYSTEM
            )
            self._suspended.discard(name)
            self._suspended_at.pop(name, None)
        self._suspended_for_pressure.clear()
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
