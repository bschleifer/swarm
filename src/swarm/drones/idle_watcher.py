"""Idle-watcher drone — nudge RESTING workers sitting on assigned tasks.

Phase 2 of task #225. Phase 1 of the same ticket fixed the common case —
``swarm_create_task(target_worker=X)`` now dispatches the task into X's PTY
on assignment. But that only covers the happy path. If a worker drops a
task mid-turn (crash, compact, network hiccup) or the Queen hand-assigns
via a path that doesn't go through ``assign_and_start_task``, the worker
can still end up RESTING with an ASSIGNED/IN_PROGRESS task it's not
actually working on. This watcher sweeps periodically and catches those.

Scope: intentionally narrow. The watcher doesn't diagnose — it just pokes
the worker with a pointer at its own tools (``swarm_task_status mine``,
``swarm_check_messages``) so the worker can decide whether to resume or
report a blocker. Every nudge is logged to the buzz log so the operator
can tune cadence or catch runaway prompting.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from swarm.drones.log import DroneAction, LogCategory
from swarm.logging import get_logger
from swarm.worker.worker import WorkerState

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from swarm.drones.log import DroneLog
    from swarm.tasks.board import TaskBoard
    from swarm.worker.worker import Worker


_log = get_logger("drones.idle_watcher")


# States where a worker is "idle" from the watcher's perspective.  BUZZING
# means the worker is already producing output so we leave it alone.
# WAITING is an approval prompt — operator/drone rules handle that path.
# STUNG means the worker process has exited; revive is a separate concern.
_IDLE_STATES: frozenset[WorkerState] = frozenset({WorkerState.RESTING, WorkerState.SLEEPING})


def _nudge_message(task_numbers: list[int]) -> str:
    """Build the PTY message sent to an idle worker.

    Kept short and tool-centric — we want the worker to call its existing
    status + message MCP tools rather than treat this like a new prompt.
    """
    if len(task_numbers) == 1:
        task_ref = f"#{task_numbers[0]}"
    else:
        task_ref = ", ".join(f"#{n}" for n in task_numbers)
    return (
        f"You have {task_ref} active but appear idle. "
        "Run `swarm_task_status filter=mine` and `swarm_check_messages`, "
        "then resume or report a blocker."
    )


class IdleWatcher:
    """Periodic sweep: idle workers with active tasks get a nudge.

    Parameters
    ----------
    drone_config:
        Owns ``idle_nudge_interval_seconds`` and
        ``idle_nudge_debounce_seconds``. ``interval <= 0`` disables the
        watcher entirely.
    task_board:
        Source of truth for "does this worker have an active task".
    drone_log:
        Every nudge is appended as ``AUTO_NUDGE`` under ``LogCategory.DRONE``.
    send_to_worker:
        Async callable ``(worker_name, message, *, _log_operator=False) -> None``.
        Mirrors ``SwarmDaemon.send_to_worker`` — injected so tests can
        substitute a fake without dragging in a full daemon.
    rate_limit_check:
        Optional ``(worker_name) -> bool``.  Returning ``True`` skips
        the nudge for that worker (e.g. hit the 5hr Claude quota —
        prompting would stack stale work behind a dead quota).
    """

    def __init__(
        self,
        *,
        drone_config,
        task_board: TaskBoard | None,
        drone_log: DroneLog,
        send_to_worker: Callable[..., Awaitable[None]],
        rate_limit_check: Callable[[str], bool] | None = None,
    ) -> None:
        self._config = drone_config
        self._task_board = task_board
        self._drone_log = drone_log
        self._send_to_worker = send_to_worker
        self._rate_limit_check = rate_limit_check
        # (worker_name, task_id) → last-nudge monotonic timestamp
        self._last_nudge: dict[tuple[str, str], float] = {}
        self._last_sweep: float = 0.0

    @property
    def interval_seconds(self) -> float:
        return float(self._config.idle_nudge_interval_seconds or 0.0)

    @property
    def debounce_seconds(self) -> float:
        return float(self._config.idle_nudge_debounce_seconds or 0.0)

    @property
    def enabled(self) -> bool:
        return self.interval_seconds > 0 and self._task_board is not None

    def due(self, *, now: float | None = None) -> bool:
        """Has enough wall time elapsed since the last sweep?"""
        if not self.enabled:
            return False
        now = now if now is not None else time.monotonic()
        return (now - self._last_sweep) >= self.interval_seconds

    async def sweep(self, workers: list[Worker], *, now: float | None = None) -> int:
        """Run one sweep.  Returns the number of nudges actually sent.

        Safe to call more often than ``interval_seconds``; no-ops when not
        due. Caller can force a sweep by passing a ``now`` value that pushes
        past the threshold.
        """
        if not self.enabled:
            return 0
        now = now if now is not None else time.monotonic()
        if (now - self._last_sweep) < self.interval_seconds:
            return 0
        self._last_sweep = now

        sent = 0
        for worker in workers:
            if not self._should_nudge(worker, now=now):
                continue
            active = self._task_board.active_tasks_for_worker(worker.name)
            if not active:
                continue
            numbers = sorted({t.number for t in active})
            # Debounce per (worker, task_id) — don't spam the same work.
            task_ids = [t.id for t in active]
            fresh_keys = [
                (worker.name, tid) for tid in task_ids if self._is_fresh(worker.name, tid, now=now)
            ]
            if not fresh_keys:
                continue
            message = _nudge_message(numbers)
            try:
                await self._send_to_worker(worker.name, message, _log_operator=False)
            except Exception:
                # Don't let one failed worker kill the sweep — log and move on.
                _log.warning(
                    "idle_watcher: send_to_worker failed for %s", worker.name, exc_info=True
                )
                continue
            for key in fresh_keys:
                self._last_nudge[key] = now
            self._drone_log.add(
                DroneAction.AUTO_NUDGE,
                worker.name,
                f"idle with active task(s): {', '.join(f'#{n}' for n in numbers)}",
                category=LogCategory.DRONE,
            )
            sent += 1
        return sent

    def _should_nudge(self, worker: Worker, *, now: float) -> bool:
        """Cheap filters applied BEFORE we look at the task board."""
        if worker.display_state not in _IDLE_STATES:
            return False
        if self._rate_limit_check is not None:
            try:
                if self._rate_limit_check(worker.name):
                    return False
            except Exception:
                _log.debug(
                    "idle_watcher: rate_limit_check raised for %s", worker.name, exc_info=True
                )
                return False
        return True

    def _is_fresh(self, worker_name: str, task_id: str, *, now: float) -> bool:
        """True when ``(worker, task)`` hasn't been nudged within the debounce."""
        last = self._last_nudge.get((worker_name, task_id))
        if last is None:
            return True
        if self.debounce_seconds <= 0:
            return True
        return (now - last) >= self.debounce_seconds
