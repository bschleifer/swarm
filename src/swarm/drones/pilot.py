"""Drone background drones — async polling loop + decision engine."""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

from swarm.drones.log import DroneAction, DroneLog, LogCategory, SystemAction
from swarm.drones.rules import Decision, decide
from swarm.tasks.proposal import QueenAction
from swarm.config import DroneConfig
from swarm.events import EventEmitter
from swarm.logging import get_logger
from swarm.pty.process import ProcessError
from swarm.worker.manager import revive_worker
from swarm.tasks.task import TaskStatus
from swarm.worker.worker import Worker, WorkerState

if TYPE_CHECKING:
    from collections.abc import Callable

    from swarm.drones.rules import DroneDecision
    from swarm.providers import LLMProvider
    from swarm.pty.pool import ProcessPool
    from swarm.queen.queen import Queen
    from swarm.tasks.board import TaskBoard
    from swarm.tasks.proposal import AssignmentProposal
    from swarm.tasks.task import SwarmTask

_log = get_logger("drones.pilot")

# Run Queen coordination every N poll cycles (default: every 12 cycles = ~60s at 5s interval)
_COORDINATION_INTERVAL = 12

# classify_worker_output examines <=30 lines; 35 gives margin for context.
_STATE_DETECT_LINES = 35


class DronePilot(EventEmitter):
    def __init__(
        self,
        workers: list[Worker],
        log: DroneLog,
        interval: float = 5.0,
        pool: ProcessPool | None = None,
        drone_config: DroneConfig | None = None,
        task_board: TaskBoard | None = None,
        queen: Queen | None = None,
        worker_descriptions: dict[str, str] | None = None,
    ) -> None:
        self.__init_emitter__()
        self.workers = workers
        self.log = log
        self.interval = interval
        self.pool = pool
        self.drone_config = drone_config or DroneConfig()
        self._provider_cache: dict[str, LLMProvider] = {}
        self._auto_complete_min_idle = self.drone_config.auto_complete_min_idle
        self.task_board = task_board
        self.queen = queen
        self.worker_descriptions = worker_descriptions or {}
        self.enabled = False
        self._running = False  # loop lifecycle (separate from action gating)
        self._task: asyncio.Task | None = None
        self._prev_states: dict[str, WorkerState] = {}
        self._escalated: set[str] = set()
        self._tick: int = 0
        # Adaptive polling
        self._idle_streak: int = 0
        self._base_interval: float = interval
        self._max_interval: float = self.drone_config.max_idle_interval
        # Per-worker circuit breaker
        self._poll_failures: dict[str, int] = {}
        # Prevent concurrent poll_once execution
        self._poll_lock = asyncio.Lock()
        # Focus tracking: when a user is viewing a worker, poll faster
        self._focused_workers: set[str] = set()
        self._focus_interval: float = 2.0
        # Track whether any substantive (non-escalation) action happened this tick.
        # Escalations should NOT reset the adaptive backoff — the drone is just
        # waiting for the user and polling is wasted.
        self._had_substantive_action: bool = False
        # Track whether any worker transitioned TO BUZZING this tick.
        # Used for idle streak reset — only active transitions reset backoff,
        # not RESTING flickers or RESTING→WAITING changes.
        self._any_became_active: bool = False
        # Test mode: emit drone_decision events with full context
        self._emit_decisions: bool = False
        # Hive-complete detection — only fires after a task is completed
        # during this pilot session (not stale completions from disk).
        self._all_done_streak: int = 0
        self._saw_completion: bool = False
        # Track task IDs already proposed for completion (prevent re-proposing).
        # Maps task_id → timestamp of last proposal.  Allows re-proposing after
        # a cooldown so tasks aren't permanently stuck if the Queen initially
        # said "not done".
        self._proposed_completions: dict[str, float] = {}
        # Proposal support: callback to check if pending proposals exist
        self._pending_proposals_check: Callable[[], bool] | None = None
        # Per-worker proposal check: returns True if the named worker has pending proposals
        self._pending_proposals_for_worker: Callable[[str], bool] | None = None
        # Per-worker consecutive idle poll counter for idle-escalation
        self._idle_consecutive: dict[str, int] = {}
        # Event-driven assign: set when a worker transitions to RESTING
        self._needs_assign_check: bool = False
        # Coordination skip: snapshot of worker states + task counts.
        # When identical to the previous cycle, the Queen call is skipped.
        self._prev_coordination_snapshot: dict[str, str | int] | None = None
        # Per-worker last full-poll timestamp (for sleeping worker throttling)
        self._last_full_poll: dict[str, float] = {}
        # Content fingerprinting: hash of last 5 lines to detect unchanged output
        self._content_fingerprints: dict[str, int] = {}
        self._unchanged_streak: dict[str, int] = {}
        # Suspension: fully skip sleeping workers with unchanged content
        self._suspended: set[str] = set()  # worker names
        self._suspended_at: dict[str, float] = {}  # name -> timestamp
        self._suspend_safety_interval: float = 60.0  # safety-net poll interval

    def _get_provider(self, worker: Worker) -> LLMProvider:
        """Return the LLMProvider for a worker, caching by provider name."""
        name = worker.provider_name
        if name not in self._provider_cache:
            from swarm.providers import get_provider

            self._provider_cache[name] = get_provider(name)
        return self._provider_cache[name]

    # --- Public encapsulation methods ---

    def get_diagnostics(self) -> dict[str, object]:
        """Return pilot health/diagnostic info for status endpoints."""
        task = self._task
        info: dict[str, object] = {
            "running": self._running,
            "enabled": self.enabled,
            "task_alive": task is not None and not task.done(),
            "tick": self._tick,
            "idle_streak": self._idle_streak,
            "suspended_count": len(self._suspended),
            "suspended_workers": sorted(self._suspended),
        }
        if task and task.done():
            try:
                exc = task.exception() if not task.cancelled() else "cancelled"
            except Exception:
                exc = "unknown"
            info["task_exception"] = str(exc) if exc else None
        return info

    def set_focused_workers(self, workers: set[str]) -> None:
        """Set which workers should be polled at accelerated interval."""
        # Wake any newly focused workers that are suspended
        for name in workers - self._focused_workers:
            self.wake_worker(name)
        self._focused_workers = workers

    def set_pending_proposals_check(self, callback: Callable[[], bool] | None) -> None:
        """Register callback to check if pending proposals exist."""
        self._pending_proposals_check = callback

    def set_pending_proposals_for_worker(self, callback: Callable[[str], bool] | None) -> None:
        """Register callback to check if a specific worker has pending proposals."""
        self._pending_proposals_for_worker = callback

    def set_poll_intervals(self, base: float, max_val: float) -> None:
        """Update polling intervals without restarting the poll loop."""
        self._base_interval = base
        self._max_interval = max_val

    def set_emit_decisions(self, enabled: bool) -> None:
        """Enable/disable emission of drone_decision events (for test mode)."""
        self._emit_decisions = enabled

    def set_auto_complete_idle(self, seconds: float) -> None:
        """Override the minimum idle time before proposing task completion."""
        self._auto_complete_min_idle = seconds

    def mark_completion_seen(self) -> None:
        """Signal that a task completion occurred during this pilot session."""
        self._saw_completion = True

    def wake_worker(self, name: str) -> bool:
        """Wake a suspended worker so it's polled on the next tick.

        Returns ``True`` if the worker was actually suspended.
        """
        if name not in self._suspended:
            return False
        self._suspended.discard(name)
        self._suspended_at.pop(name, None)
        # Clear content fingerprint + unchanged streak to force full classify
        self._content_fingerprints.pop(name, None)
        self._unchanged_streak.pop(name, None)
        _log.info("woke suspended worker: %s", name)
        return True

    def _maybe_suspend_worker(self, worker: Worker) -> None:
        """Suspend a sleeping worker if it has been unchanged long enough."""
        if worker.display_state != WorkerState.SLEEPING:
            return
        if worker.name in self._focused_workers:
            return
        if self._unchanged_streak.get(worker.name, 0) < 3:
            return
        if worker.name in self._suspended:
            return
        self._suspended.add(worker.name)
        self._suspended_at[worker.name] = time.time()
        _log.info("suspended sleeping worker: %s", worker.name)

    def is_loop_running(self) -> bool:
        """Check if the pilot poll loop task is currently executing."""
        return self._running and self._task is not None and not self._task.done()

    def needs_restart(self) -> bool:
        """True when the pilot should be running but the loop task has died."""
        return self._running and not self.is_loop_running()

    def restart_loop(self) -> None:
        """Restart the poll loop task. Safe to call if already running."""
        if self._task and not self._task.done():
            self._task.cancel()
        self._task = asyncio.create_task(self._loop())
        self._task.add_done_callback(self._on_loop_done)

    def clear_proposed_completion(self, task_id: str) -> None:
        """Remove a task from the proposed-completions tracker.

        Called by the daemon when a completion proposal is rejected or the
        task is unassigned, allowing the pilot to re-propose later.
        """
        self._proposed_completions.pop(task_id, None)

    def on_proposal(self, callback: Callable[[AssignmentProposal], None]) -> None:
        """Register callback for when the Queen proposes an assignment."""
        self.on("proposal", callback)

    def on_escalate(self, callback: Callable[[Worker, str], None]) -> None:
        self.on("escalate", callback)

    def on_workers_changed(self, callback: Callable[[], None]) -> None:
        """Register callback for when workers list changes (add/remove)."""
        self.on("workers_changed", callback)

    def on_task_assigned(self, callback: Callable[..., None]) -> None:
        """Register callback for when a task is auto-assigned to a worker."""
        self.on("task_assigned", callback)

    def on_task_done(self, callback: Callable[[Worker, SwarmTask, str], None]) -> None:
        """Register callback for when a task appears complete (worker idle with active task)."""
        self.on("task_done", callback)

    def on_state_changed(self, callback: Callable[[Worker], None]) -> None:
        """Register callback for any worker state change."""
        self.on("state_changed", callback)

    def on_hive_empty(self, callback: Callable[[], None]) -> None:
        """Register callback for when all workers are gone."""
        self.on("hive_empty", callback)

    def on_hive_complete(self, callback: Callable[[], None]) -> None:
        """Register callback for when all tasks are done and workers idle."""
        self.on("hive_complete", callback)

    def start(self) -> None:
        self.enabled = True
        self._running = True
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop())
            self._task.add_done_callback(self._on_loop_done)

    @staticmethod
    def _on_loop_done(task: asyncio.Task) -> None:
        """Log when the poll loop task finishes unexpectedly."""
        if task.cancelled():
            _log.info("poll loop task was cancelled")
        elif exc := task.exception():
            _log.error("poll loop task died with exception: %s", exc, exc_info=exc)
        else:
            _log.info("poll loop task exited normally")

    def stop(self) -> None:
        """Fully stop the pilot — kills the poll loop."""
        self.enabled = False
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()

    def toggle(self) -> bool:
        """Toggle drone actions on/off. State detection keeps running."""
        self.enabled = not self.enabled
        # Ensure the poll loop is alive even when drones are disabled
        # so worker state detection continues.
        if self._task is None or self._task.done():
            self._running = True
            self._task = asyncio.create_task(self._loop())
        return self.enabled

    async def poll_once(self) -> bool:
        """Run one poll cycle across all workers.

        Returns ``True`` if any action was taken (continue, revive, escalate,
        task assign, coordination directive), ``False`` otherwise.
        """
        # Revive poll loop if it died unexpectedly
        if self._running and (self._task is None or self._task.done()):
            _log.warning("poll loop was dead — restarting")
            self._task = asyncio.create_task(self._loop())
        if self._poll_lock.locked():
            return False  # Another poll is in progress — skip
        async with self._poll_lock:
            had_action, _any_state_changed = await self._poll_once_locked()
            return had_action

    def _sync_display_state(self, worker: Worker, state_changed: bool) -> bool:
        """Emit state_changed for display-only transitions (e.g. RESTING→SLEEPING).

        Returns updated ``state_changed`` flag.
        """
        display_val = worker.display_state.value
        prev_display = self._prev_states.get(f"_display_{worker.name}")
        if prev_display != display_val:
            self._prev_states[f"_display_{worker.name}"] = display_val
            if not state_changed:
                self.emit("state_changed", worker)
                state_changed = True
        return state_changed

    def _track_idle(self, worker: Worker) -> None:
        """Update per-worker idle-consecutive counter."""
        if worker.state == WorkerState.RESTING:
            self._idle_consecutive[worker.name] = self._idle_consecutive.get(worker.name, 0) + 1
        else:
            self._idle_consecutive.pop(worker.name, None)

    def _handle_state_change(self, worker: Worker, prev: WorkerState) -> tuple[bool, bool]:
        """Process a worker state change. Returns (transitioned_to_resting, state_changed)."""
        self.emit("state_changed", worker)
        # Wake from suspension on any real state transition
        self.wake_worker(worker.name)
        if worker.state == WorkerState.BUZZING:
            self._any_became_active = True
        transitioned = False
        if prev == WorkerState.BUZZING and worker.state in (
            WorkerState.RESTING,
            WorkerState.WAITING,
        ):
            transitioned = True
            self._needs_assign_check = True
        return transitioned, True

    def _should_skip_decide(self, worker: Worker, changed: bool) -> bool:
        """Return True if the decision engine should be skipped for this worker."""
        # Skip when drones are disabled
        if not self.enabled:
            return True
        # Skip already-escalated workers with no state change
        if worker.name in self._escalated and not changed:
            return True
        return False

    def _should_throttle_sleeping(self, worker: Worker) -> bool:
        """Check if a sleeping worker's full poll should be skipped (throttled)."""
        if worker.display_state != WorkerState.SLEEPING:
            return False
        if worker.name in self._focused_workers:
            return False
        last = self._last_full_poll.get(worker.name, 0.0)
        return time.time() - last < self.drone_config.sleeping_poll_interval

    def _update_content_fingerprint(self, name: str, content: str) -> None:
        """Update content fingerprint and unchanged streak for a worker."""
        fp = hash(content[-200:]) if content else 0
        if fp == self._content_fingerprints.get(name):
            self._unchanged_streak[name] = self._unchanged_streak.get(name, 0) + 1
        else:
            self._unchanged_streak[name] = 0
        self._content_fingerprints[name] = fp

    def _poll_sleeping_throttled(self, worker: Worker, cmd: str) -> tuple[bool, bool] | None:
        """Lightweight poll for throttled sleeping workers.

        Returns ``(had_action, state_changed)`` if the worker was handled
        (caller should return early), or ``None`` to fall through to full poll.
        """
        if not self._should_throttle_sleeping(worker):
            return None
        content = worker.process.get_content(5) if worker.process else ""
        new_state = self._get_provider(worker).classify_output(cmd, content)
        if new_state in (WorkerState.WAITING, WorkerState.BUZZING):
            return None  # State changed — fall through to full poll
        self._update_content_fingerprint(worker.name, content)
        state_changed = self._sync_display_state(worker, False)
        self._maybe_suspend_worker(worker)
        return False, state_changed

    def _poll_single_worker(
        self,
        worker: Worker,
        dead_workers: list[Worker],
    ) -> tuple[bool, bool, bool]:
        """Poll one worker. Returns (had_action, transitioned_to_resting, state_changed)."""
        had_action = False
        transitioned = False
        state_changed = False

        proc = worker.process
        if not proc or not proc.is_alive:
            if worker.state == WorkerState.STUNG:
                from swarm.worker.worker import STUNG_REAP_TIMEOUT

                if worker.state_duration >= STUNG_REAP_TIMEOUT:
                    _log.info("reaping stung worker %s (%.0fs)", worker.name, worker.state_duration)
                    dead_workers.append(worker)
                    return True, False, False
                # Run decision engine on STUNG worker (may trigger REVIVE)
                content = proc.get_content(_STATE_DETECT_LINES) if proc else ""
                had_action = self._run_decision_sync(worker, content)
                return had_action, False, False
            # Process confirmed dead — force STUNG (bypass hysteresis)
            _log.info("process gone for worker %s — marking STUNG", worker.name)
            worker.force_state(WorkerState.STUNG)
            self.emit("state_changed", worker)
            self._sync_display_state(worker, True)
            return True, False, True

        cmd = proc.get_child_foreground_command()

        # Throttle sleeping workers: lightweight state check instead of full poll
        throttle_result = self._poll_sleeping_throttled(worker, cmd)
        if throttle_result is not None:
            return False, False, throttle_result[1]
        if worker.display_state == WorkerState.SLEEPING:
            self._last_full_poll[worker.name] = time.time()

        content = proc.get_content(_STATE_DETECT_LINES)

        # Content fingerprinting: when a RESTING worker's output hasn't
        # changed for 3+ consecutive polls, skip classify + decide.
        self._update_content_fingerprint(worker.name, content)

        if worker.state == WorkerState.RESTING and self._unchanged_streak.get(worker.name, 0) >= 3:
            state_changed = self._sync_display_state(worker, False)
            self._poll_failures.pop(worker.name, None)
            return False, False, state_changed

        new_state = self._get_provider(worker).classify_output(cmd, content)
        # Shell fallback: CLI exited but the wrapper shell is still alive.
        # Treat as RESTING so the user can type in the shell (e.g. --resume).
        if new_state == WorkerState.STUNG and proc.is_alive:
            new_state = WorkerState.RESTING
        prev = self._prev_states.get(worker.name, worker.state)
        changed = worker.update_state(new_state)

        self._poll_failures.pop(worker.name, None)

        if changed:
            transitioned, state_changed = self._handle_state_change(worker, prev)

        self._track_idle(worker)

        # Sync display_state — handles RESTING→SLEEPING transitions
        # even when worker.state hasn't changed.
        state_changed = self._sync_display_state(worker, state_changed)

        self._prev_states[worker.name] = worker.state

        if self._should_skip_decide(worker, changed):
            return had_action, transitioned, state_changed

        had_action = self._run_decision_sync(worker, content)
        return had_action, transitioned, state_changed

    def _run_decision_sync(self, worker: Worker, content: str) -> bool:
        """Evaluate the drone decision for a worker (sync — actions deferred)."""
        decision = decide(
            worker,
            content,
            self.drone_config,
            escalated=self._escalated,
            provider=self._get_provider(worker),
        )

        if self._emit_decisions:
            self.emit("drone_decision", worker, content, decision)

        if decision.decision == Decision.CONTINUE:
            self._deferred_actions.append(("continue", worker, decision))
            return True
        if decision.decision == Decision.REVIVE:
            self._deferred_actions.append(("revive", worker, decision))
            return True
        if decision.decision == Decision.ESCALATE:
            self.log.add(
                DroneAction.ESCALATED,
                worker.name,
                decision.reason,
                metadata={"source": decision.source, "rule_pattern": decision.rule_pattern},
            )
            self.emit("escalate", worker, decision.reason)
            return True
        return False

    async def _execute_deferred_actions(self) -> None:
        """Execute deferred async actions from the sync poll loop."""
        for action_type, worker, decision in self._deferred_actions:
            if action_type == "continue":
                await self._safe_worker_action(
                    worker,
                    worker.process.send_enter(),
                    DroneAction.CONTINUED,
                    decision,
                    include_rule_pattern=True,
                )
            elif action_type == "revive":
                if await self._safe_worker_action(
                    worker,
                    revive_worker(worker, self.pool),
                    DroneAction.REVIVED,
                    decision,
                ):
                    worker.record_revive()
        self._deferred_actions.clear()

    async def _safe_worker_action(
        self,
        worker: Worker,
        coro: object,
        action: DroneAction,
        decision: DroneDecision | None = None,
        *,
        include_rule_pattern: bool = False,
        reason: str | None = None,
    ) -> bool:
        """Execute *coro* for *worker*, log on success, warn on failure.

        Returns ``True`` on success.  Sets ``_had_substantive_action`` so
        the adaptive backoff resets correctly.
        """
        try:
            await coro  # type: ignore[misc]
        except (ProcessError, OSError):
            _log.warning("failed %s for %s", action.value, worker.name, exc_info=True)
            return False
        metadata: dict[str, str] = {}
        if decision is not None:
            metadata["source"] = decision.source
            if include_rule_pattern and decision.rule_pattern:
                metadata["rule_pattern"] = decision.rule_pattern
        log_reason = reason or (decision.reason if decision else "")
        self.log.add(action, worker.name, log_reason, metadata=metadata)
        self._had_substantive_action = True
        return True

    def _cleanup_dead_workers(self, dead_workers: list[Worker]) -> None:
        """Remove dead workers from tracking and unassign their tasks."""
        for dw in dead_workers:
            self.workers.remove(dw)
            self._prev_states.pop(dw.name, None)
            self._poll_failures.pop(dw.name, None)
            self._escalated.discard(dw.name)
            self._idle_consecutive.pop(dw.name, None)
            self._content_fingerprints.pop(dw.name, None)
            self._unchanged_streak.pop(dw.name, None)
            self._suspended.discard(dw.name)
            self._suspended_at.pop(dw.name, None)
            _log.info("removed dead worker: %s", dw.name)
            if self.task_board:
                self.task_board.unassign_worker(dw.name)
        self.emit("workers_changed")

    def _should_eager_assign(self) -> bool:
        """Check if idle-escalation or event-driven flag should trigger assign."""
        if self._needs_assign_check:
            return True
        threshold = self.drone_config.idle_assign_threshold
        if not self.task_board or not self.task_board.available_tasks:
            return False
        return any(v >= threshold for v in self._idle_consecutive.values())

    async def _run_periodic_tasks(self) -> bool:
        """Run periodic background tasks: completions, auto-assign, coordination."""
        had_action = False
        if self.enabled and self.task_board:
            if self._check_task_completions():
                had_action = True
        # Auto-assign: always attempt, but _should_eager_assign logs intent
        self._needs_assign_check = False
        if self.enabled and self.task_board and self.queen:
            if await self._auto_assign_tasks():
                had_action = True
        if (
            self.enabled
            and self.queen
            and self._tick > 0
            and self._tick % _COORDINATION_INTERVAL == 0
        ):
            if await self._coordination_cycle():
                had_action = True
        return had_action

    def _is_suspended_skip(self, worker: Worker) -> bool:
        """Return True if this worker should be skipped (suspended, safety-net not elapsed)."""
        if worker.name not in self._suspended:
            return False
        suspended_since = self._suspended_at.get(worker.name, 0.0)
        now = time.time()
        if now - suspended_since < self._suspend_safety_interval:
            return True
        # Safety-net: reset timer and fall through to normal poll
        self._suspended_at[worker.name] = now
        return False

    async def _poll_once_locked(self) -> tuple[bool, bool]:
        """Returns (had_action, any_state_changed)."""
        any_transitioned_to_resting = False
        any_state_changed = False
        dead_workers: list[Worker] = []
        had_action = False
        max_poll_failures = self.drone_config.max_poll_failures
        self._deferred_actions: list[tuple] = []

        for worker in list(self.workers):
            if self._is_suspended_skip(worker):
                continue

            try:
                action, transitioned, changed = self._poll_single_worker(worker, dead_workers)
                had_action |= action
                any_transitioned_to_resting |= transitioned
                any_state_changed |= changed
            except (ProcessError, OSError):
                fails = self._poll_failures.get(worker.name, 0) + 1
                self._poll_failures[worker.name] = fails
                _log.warning(
                    "poll failed for %s (%d/%d)",
                    worker.name,
                    fails,
                    max_poll_failures,
                    exc_info=True,
                )
                if fails >= max_poll_failures:
                    _log.warning(
                        "circuit breaker tripped for %s — treating as dead",
                        worker.name,
                    )
                    dead_workers.append(worker)
                    had_action = True

        # Execute deferred async actions (send_enter, revive)
        await self._execute_deferred_actions()

        if dead_workers:
            self._cleanup_dead_workers(dead_workers)

        if await self._run_periodic_tasks():
            had_action = True

        self._tick += 1
        return had_action, any_state_changed

    # Default idle threshold — overridden by drone_config.auto_complete_min_idle in __init__
    _AUTO_COMPLETE_MIN_IDLE = 45  # seconds (class default, instance attr preferred)

    # If the Queen initially rejected a completion, wait this long before
    # re-proposing.  Prevents spam while still catching tasks that are truly
    # done after the initial check said "not done".
    _COMPLETION_REPROPOSE_COOLDOWN = 300  # 5 minutes

    def _check_task_completions(self) -> bool:
        """Propose completion for tasks whose assigned worker has been idle long enough.

        Instead of auto-completing, emits a ``task_done`` event so the daemon
        can ask the Queen for an assessment and create a user-approvable proposal.

        Uses a timestamp-based cooldown so tasks aren't permanently stuck if
        the Queen initially said "not done".
        """
        if not self.task_board:
            return False

        now = time.time()
        proposed_any = False
        for worker in self.workers:
            if worker.state != WorkerState.RESTING:
                continue
            if worker.state_duration < self._auto_complete_min_idle:
                continue
            active_tasks = [
                t
                for t in self.task_board.tasks_for_worker(worker.name)
                if t.status in (TaskStatus.ASSIGNED, TaskStatus.IN_PROGRESS)
            ]
            for task in active_tasks:
                last_proposed = self._proposed_completions.get(task.id)
                if last_proposed is not None:
                    if now - last_proposed < self._COMPLETION_REPROPOSE_COOLDOWN:
                        continue
                    _log.info(
                        "re-proposing completion for task %s (%s) — %.0fs since last attempt",
                        task.id,
                        task.title,
                        now - last_proposed,
                    )
                self._proposed_completions[task.id] = now
                self.emit("task_done", worker, task, "")
                self.log.add(
                    DroneAction.PROPOSED_COMPLETION,
                    worker.name,
                    f"task appears done: {task.title}",
                )
                _log.info(
                    "proposing completion for task %s (%s) — worker %s idle %.0fs",
                    task.id,
                    task.title,
                    worker.name,
                    worker.state_duration,
                )
                proposed_any = True
        return proposed_any

    async def _auto_assign_tasks(self) -> bool:
        """Ask Queen for assignments and emit proposals for user approval.

        Returns ``True`` if any proposals were created.
        """
        if not self.task_board or not self.queen:
            return False

        available = self.task_board.available_tasks
        if not available:
            return False

        # Find resting workers with no active task and no pending proposals
        idle_workers = [
            w
            for w in self.workers
            if w.state == WorkerState.RESTING
            and not self.task_board.active_tasks_for_worker(w.name)
            and not (
                self._pending_proposals_for_worker and self._pending_proposals_for_worker(w.name)
            )
        ]
        if not idle_workers:
            return False

        _log.info(
            "auto-assign: %d idle workers, %d available tasks", len(idle_workers), len(available)
        )

        task_dicts = [
            {
                "id": t.id,
                "title": t.title,
                "description": t.description,
                "priority": t.priority.value,
                "task_type": t.task_type.value,
                "tags": t.tags,
                "attachments": t.attachments,
            }
            for t in available
        ]

        try:
            from swarm.queen.context import build_hive_context

            hive_ctx = build_hive_context(
                list(self.workers),
                task_board=self.task_board,
                drone_log=self.log,
                worker_descriptions=self.worker_descriptions,
            )
            assignments = await self.queen.assign_tasks(
                [w.name for w in idle_workers],
                task_dicts,
                hive_context=hive_ctx,
            )
        except asyncio.CancelledError:
            _log.info("auto-assign cancelled (shutdown)")
            return False
        except (asyncio.TimeoutError, RuntimeError, ProcessError, OSError):
            _log.warning("Queen assign_tasks failed", exc_info=True)
            return False

        from swarm.tasks.proposal import AssignmentProposal

        acted = False
        auto_approve = self.drone_config.auto_approve_assignments
        min_conf = getattr(self.queen, "min_confidence", 0.7)
        for assignment in assignments:
            if not isinstance(assignment, dict):
                _log.warning("Queen returned non-dict assignment entry: %s", type(assignment))
                continue
            worker_name = assignment.get("worker", "")
            task_id = assignment.get("task_id", "")
            message = assignment.get("message", "")
            reasoning = assignment.get("reasoning", "")
            confidence = float(assignment.get("confidence", 0.8))

            worker = next((w for w in self.workers if w.name == worker_name), None)
            task = self.task_board.get(task_id) if task_id else None

            if not worker or not task or not task.is_available:
                continue

            # Auto-approve high-confidence assignments directly
            if auto_approve and confidence >= min_conf:
                _log.info(
                    "auto-approving assignment: %s → %s (conf=%.2f)",
                    worker_name,
                    task.title,
                    confidence,
                )
                self.log.add(
                    DroneAction.AUTO_ASSIGNED,
                    worker_name,
                    f"auto-assigned: {task.title} (conf={confidence:.0%})",
                )
                self.emit("task_assigned", worker, task, message)
                # Reset idle counter for this worker
                self._idle_consecutive.pop(worker_name, None)
                acted = True
                continue

            # Below threshold: create a proposal for user approval
            proposal = AssignmentProposal.assignment(
                worker_name=worker_name,
                task_id=task_id,
                task_title=task.title,
                message=message,
                reasoning=reasoning,
                confidence=confidence,
            )
            _log.info("Queen proposed: %s → %s (%s)", worker_name, task.title, task_id)
            self.log.add(
                DroneAction.PROPOSED_ASSIGNMENT, worker_name, f"Queen proposed: {task.title}"
            )
            self.emit("proposal", proposal)
            acted = True

        return acted

    # --- Directive action handlers ---

    async def _handle_send_message(self, directive: dict, worker: Worker) -> bool:
        """Handle Queen 'send_message' directive via proposal system."""
        message = directive.get("message", "")
        if not message:
            return False
        # Re-check pending proposals (guard at top may be stale after Queen call)
        if self._pending_proposals_check and self._pending_proposals_check():
            _log.info("Ignoring send_message for %s: pending proposals exist", worker.name)
            return False
        from swarm.tasks.proposal import AssignmentProposal

        reason = directive.get("reason", "")
        proposal = AssignmentProposal.escalation(
            worker_name=worker.name,
            action=QueenAction.SEND_MESSAGE,
            assessment=reason,
            message=message,
            reasoning=reason,
        )
        self.emit("proposal", proposal)
        self.log.add(DroneAction.PROPOSED_MESSAGE, worker.name, f"Queen proposes message: {reason}")
        return True

    async def _handle_continue(self, directive: dict, worker: Worker) -> bool:
        """Handle Queen 'continue' directive — send Enter to worker."""
        reason = directive.get("reason", "")
        return await self._safe_worker_action(
            worker,
            worker.process.send_enter(),
            DroneAction.QUEEN_CONTINUED,
            reason=f"Queen: {reason}",
        )

    async def _handle_restart(self, directive: dict, worker: Worker) -> bool:
        """Handle Queen 'restart' directive — revive the worker process."""
        reason = directive.get("reason", "")
        return await self._safe_worker_action(
            worker,
            revive_worker(worker, self.pool),
            DroneAction.REVIVED,
            reason=f"Queen: {reason}",
        )

    async def _handle_complete_task(self, directive: dict, worker: Worker) -> bool:
        """Handle Queen 'complete_task' directive — propose task completion."""
        task_id = directive.get("task_id", "")
        reason = directive.get("reason", "")
        resolution = directive.get("resolution", reason)
        # Guard: only propose if worker is actually RESTING
        if worker.state != WorkerState.RESTING:
            _log.info(
                "Ignoring complete_task for %s: worker %s is %s, not RESTING",
                task_id,
                worker.name,
                worker.state.value,
            )
            return False
        task = self.task_board.get(task_id) if task_id and self.task_board else None
        if not task or task.status not in (TaskStatus.ASSIGNED, TaskStatus.IN_PROGRESS):
            return False
        if task_id in self._proposed_completions:
            return False
        self._proposed_completions[task_id] = time.time()
        self.emit("task_done", worker, task, resolution)
        self.log.add(DroneAction.QUEEN_PROPOSED_DONE, worker.name, f"Queen proposes done: {reason}")
        _log.info("Queen proposes task %s done for %s", task_id, worker.name)
        return True

    async def _handle_assign_task(self, directive: dict, worker: Worker) -> bool:
        """Handle Queen 'assign_task' directive — propose task assignment."""
        task_id = directive.get("task_id", "")
        message = directive.get("message", "")
        reason = directive.get("reason", "")
        if not task_id or not self.task_board or not message:
            return False
        task = self.task_board.get(task_id)
        if not task or not task.is_available:
            _log.info("Ignoring assign_task for %s: task %s not available", worker.name, task_id)
            return False
        # Don't assign to workers who still have an active task
        active_tasks = self.task_board.active_tasks_for_worker(worker.name)
        if active_tasks:
            _log.info(
                "Ignoring assign_task for %s: worker already has %d active task(s)",
                worker.name,
                len(active_tasks),
            )
            return False
        # Re-check pending proposals (guard at top may be stale after Queen call)
        if self._pending_proposals_check and self._pending_proposals_check():
            _log.info("Ignoring assign_task for %s: pending proposals exist", worker.name)
            return False
        from swarm.tasks.proposal import AssignmentProposal

        proposal = AssignmentProposal.assignment(
            worker_name=worker.name,
            task_id=task_id,
            task_title=task.title,
            message=message,
            reasoning=reason,
        )
        self.emit("proposal", proposal)
        self.log.add(DroneAction.PROPOSED_ASSIGNMENT, worker.name, f"Queen proposed: {task.title}")
        return True

    async def _handle_wait(self, _directive: dict[str, object], _worker: Worker) -> bool:
        """No-op: Queen says to wait and observe."""
        return False

    _ACTION_HANDLERS: dict[str, Callable[..., object]] = {
        QueenAction.SEND_MESSAGE: _handle_send_message,
        QueenAction.CONTINUE: _handle_continue,
        QueenAction.RESTART: _handle_restart,
        QueenAction.COMPLETE_TASK: _handle_complete_task,
        QueenAction.ASSIGN_TASK: _handle_assign_task,
        QueenAction.WAIT: _handle_wait,
    }

    async def _execute_directives(self, directives: list[object]) -> bool:
        """Dispatch a list of Queen directives to the appropriate handlers."""
        had_directive = False
        for directive in directives:
            if not isinstance(directive, dict):
                _log.warning("Queen returned non-dict directive entry: %s", type(directive))
                continue
            worker_name = directive.get("worker", "")
            action = directive.get("action", "")

            worker = next((w for w in self.workers if w.name == worker_name), None)
            if not worker:
                continue

            _log.info(
                "Queen directive: %s → %s (%s)", worker_name, action, directive.get("reason", "")
            )

            handler = self._ACTION_HANDLERS.get(action)
            if handler:
                if await handler(self, directive, worker):
                    had_directive = True
            else:
                _log.warning("Unknown Queen directive action: %r for %s", action, worker_name)
        return had_directive

    def _coordination_snapshot_unchanged(self) -> bool:
        """Return True if hive state hasn't changed since last coordination cycle."""
        available_count = len(self.task_board.available_tasks) if self.task_board else 0
        active_count = len(self.task_board.active_tasks) if self.task_board else 0
        snapshot: dict[str, str | int] = {w.name: w.display_state.value for w in self.workers}
        snapshot["__available"] = available_count
        snapshot["__active"] = active_count
        if snapshot == self._prev_coordination_snapshot:
            return True
        self._prev_coordination_snapshot = snapshot
        return False

    def _capture_worker_outputs(self) -> dict[str, str]:
        """Capture worker output for coordination.

        Skips sleeping, stung, and already-escalated WAITING workers.
        """
        worker_outputs: dict[str, str] = {}
        for w in list(self.workers):
            ds = w.display_state
            if ds in (WorkerState.SLEEPING, WorkerState.STUNG):
                continue
            # Skip WAITING workers already escalated — their prompt is known
            if ds == WorkerState.WAITING and w.name in self._escalated:
                continue
            lines = 15 if ds == WorkerState.RESTING else 60
            if w.process:
                worker_outputs[w.name] = w.process.get_content(lines)
        return worker_outputs

    async def _coordination_cycle(self) -> bool:
        """Periodic full-hive coordination via Queen.

        Returns ``True`` if any directives were executed.
        """
        if not self.queen or not self.queen.enabled:
            return False

        # Skip if there are already pending proposals awaiting user decision
        if self._pending_proposals_check and self._pending_proposals_check():
            return False

        # Skip coordination when all workers are actively BUZZING — there's
        # nothing to coordinate (especially with a single worker).
        worker_states = {w.state for w in self.workers}
        if worker_states == {WorkerState.BUZZING}:
            _log.debug("coordination skipped: all %d workers BUZZING", len(self.workers))
            return False

        if self._coordination_snapshot_unchanged():
            _log.debug("coordination skipped: hive state unchanged")
            return False

        _start = time.time()
        try:
            from swarm.queen.context import build_hive_context

            worker_outputs = self._capture_worker_outputs()

            hive_ctx = build_hive_context(
                list(self.workers),
                worker_outputs=worker_outputs,
                drone_log=self.log,
                task_board=self.task_board,
                worker_descriptions=self.worker_descriptions,
            )
            result = await self.queen.coordinate_hive(hive_ctx)
        except asyncio.CancelledError:
            _log.info("coordination cycle cancelled (shutdown)")
            return False
        except (asyncio.TimeoutError, RuntimeError, ProcessError, OSError):
            _log.warning("Queen coordination cycle failed", exc_info=True)
            return False

        directives = result.get("directives", []) if isinstance(result, dict) else []
        had_directive = await self._execute_directives(directives)

        # Reset snapshot so the next cycle re-evaluates after a real action.
        if had_directive:
            self._prev_coordination_snapshot = None

        # Only log QUEEN_PROPOSAL when directives produced a real action;
        # no-op cycles (all "wait") are debug-only to avoid buzz log spam.
        if had_directive:
            self.log.add(
                SystemAction.QUEEN_PROPOSAL,
                "hive",
                f"coordination: {len(directives)} directives",
                category=LogCategory.QUEEN,
                metadata={
                    "duration_s": round(time.time() - _start, 1),
                    "directive_count": len(directives),
                    "confidence": float(result.get("confidence", 0))
                    if isinstance(result, dict)
                    else 0.0,
                },
            )
        else:
            _log.debug(
                "coordination cycle: %d directives (all no-op, %.1fs)",
                len(directives),
                time.time() - _start,
            )

        conflicts = result.get("conflicts", []) if isinstance(result, dict) else []
        if conflicts:
            _log.warning("Queen detected conflicts: %s", conflicts)

        return had_directive

    def _compute_backoff(self) -> float:
        """Compute poll interval based on worker states and idle streak.

        Uses explicit config overrides (poll_interval_buzzing, etc.) if set,
        otherwise derives from the pilot's own _base_interval with sensible
        ratios: WAITING = 1×, BUZZING = 3×, RESTING = 3×.
        """
        cfg = self.drone_config
        base = self._base_interval
        states = {w.state for w in self.workers}

        if WorkerState.WAITING in states:
            state_base = cfg.poll_interval_waiting or base
        elif WorkerState.BUZZING in states:
            state_base = cfg.poll_interval_buzzing or base * 3
        else:
            state_base = cfg.poll_interval_resting or base * 3

        backoff = min(
            state_base * (2 ** min(self._idle_streak, 3)),
            self._max_interval,
        )
        # Cap backoff when user is actively viewing a worker that needs
        # quick response (WAITING/RESTING).  BUZZING workers don't benefit
        # from fast polling.
        if self._focused_workers & {w.name for w in self.workers}:
            focused_states = {w.state for w in self.workers if w.name in self._focused_workers}
            if focused_states & {WorkerState.WAITING, WorkerState.RESTING}:
                backoff = min(backoff, self._focus_interval)
        return backoff

    async def _loop(self) -> None:
        _log.info("poll loop started (enabled=%s, workers=%d)", self.enabled, len(self.workers))
        try:
            while self._running:
                backoff = self._base_interval
                try:
                    self._had_substantive_action = False
                    self._any_became_active = False
                    async with self._poll_lock:
                        had_action, any_state_changed = await self._poll_once_locked()

                        # Track idle streak for adaptive backoff.
                        # Reset on state changes or substantive actions (CONTINUE,
                        # REVIVE) but NOT on escalation-only ticks — escalations
                        # mean "waiting for user" and backoff should grow.
                        if self._had_substantive_action or self._any_became_active:
                            self._idle_streak = 0
                        else:
                            self._idle_streak += 1

                        # Auto-terminate when all workers are gone
                        if not self.workers:
                            _log.warning("all workers gone — stopping pilot")
                            self.enabled = False
                            self._running = False
                            self.emit("hive_empty")
                            break

                        # Detect hive completion: all tasks done, all workers idle
                        # WAITING workers still have in-flight prompts, so don't count as done
                        # Guard: only auto-stop when a task was actually completed
                        # during this pilot session (not stale completions from a
                        # previous run loaded from the persistent store).
                        if (
                            self.enabled
                            and self.drone_config.auto_stop_on_complete
                            and self.task_board
                            and self._saw_completion
                            and not self.task_board.available_tasks
                            and not self.task_board.active_tasks
                            and all(w.state == WorkerState.RESTING for w in self.workers)
                        ):
                            self._all_done_streak += 1
                            if self._all_done_streak >= 3:
                                _log.info("all tasks done, all workers idle — hive complete")
                                self.enabled = False
                                self._running = False
                                self.emit("hive_complete")
                                break
                        else:
                            self._all_done_streak = 0

                        backoff = self._compute_backoff()
                except Exception:  # broad catch: poll loop must not die
                    _log.error("poll loop error — recovering next cycle", exc_info=True)

                await asyncio.sleep(backoff)
        except asyncio.CancelledError:
            _log.debug("poll loop cancelled (shutdown)")
            raise
        except BaseException:
            _log.error("poll loop terminated unexpectedly", exc_info=True)
            raise
        finally:
            _log.info("poll loop exited (running=%s)", self._running)
