"""Drone background drones — async polling loop + decision engine."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from swarm.drones.log import DroneAction, DroneLog
from swarm.drones.rules import Decision, decide
from swarm.config import DroneConfig
from swarm.events import EventEmitter
from swarm.logging import get_logger
from swarm.tmux.cell import capture_pane, get_pane_command, pane_exists, send_enter, send_keys
from swarm.tmux.hive import discover_workers, set_pane_option, update_window_names
from swarm.tmux.style import (
    set_terminal_title,
    spinner_frame,
)
from swarm.worker.manager import revive_worker
from swarm.tasks.task import TaskStatus
from swarm.worker.state import classify_pane_content
from swarm.worker.worker import Worker, WorkerState, worker_state_counts

if TYPE_CHECKING:
    from collections.abc import Callable

    from swarm.queen.queen import Queen
    from swarm.tasks.board import TaskBoard

_log = get_logger("drones.pilot")

# Re-discover workers every N poll cycles (default: every 6 cycles = ~30s at 5s interval)
_REDISCOVERY_INTERVAL = 6

# Run Queen coordination every N poll cycles (default: every 12 cycles = ~60s at 5s interval)
_COORDINATION_INTERVAL = 12


class DronePilot(EventEmitter):
    def __init__(
        self,
        workers: list[Worker],
        log: DroneLog,
        interval: float = 5.0,
        session_name: str | None = None,
        drone_config: DroneConfig | None = None,
        task_board: TaskBoard | None = None,
        queen: Queen | None = None,
        worker_descriptions: dict[str, str] | None = None,
    ) -> None:
        self.__init_emitter__()
        self.workers = workers
        self.log = log
        self.interval = interval
        self.session_name = session_name
        self.drone_config = drone_config or DroneConfig()
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
        # Hive-complete detection
        self._all_done_streak: int = 0
        # Track task IDs already proposed for completion (prevent re-proposing)
        self._proposed_completions: set[str] = set()
        # Proposal support: callback to check if pending proposals exist
        self._pending_proposals_check: Callable[[], bool] | None = None

    def on_proposal(self, callback) -> None:
        """Register callback for when the Queen proposes an assignment."""
        self.on("proposal", callback)

    def on_escalate(self, callback) -> None:
        self.on("escalate", callback)

    def on_workers_changed(self, callback) -> None:
        """Register callback for when workers list changes (add/remove)."""
        self.on("workers_changed", callback)

    def on_task_assigned(self, callback) -> None:
        """Register callback for when a task is auto-assigned to a worker."""
        self.on("task_assigned", callback)

    def on_task_done(self, callback) -> None:
        """Register callback for when a task appears complete (worker idle with active task)."""
        self.on("task_done", callback)

    def on_state_changed(self, callback) -> None:
        """Register callback for any worker state change."""
        self.on("state_changed", callback)

    def on_hive_empty(self, callback) -> None:
        """Register callback for when all workers are gone."""
        self.on("hive_empty", callback)

    def on_hive_complete(self, callback) -> None:
        """Register callback for when all tasks are done and workers idle."""
        self.on("hive_complete", callback)

    def start(self) -> None:
        self.enabled = True
        self._running = True
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop())

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

    async def poll_once(self) -> bool:  # noqa: C901
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
            return await self._poll_once_locked()

    async def _poll_once_locked(self) -> bool:  # noqa: C901
        any_transitioned_to_resting = False
        dead_workers: list[Worker] = []
        had_action = False
        max_poll_failures = self.drone_config.max_poll_failures

        for worker in list(self.workers):
            try:
                # Check if pane still exists
                if not await pane_exists(worker.pane_id):
                    # Already STUNG (killed by user) — just skip, don't remove
                    if worker.state == WorkerState.STUNG:
                        continue
                    _log.info("pane %s gone for worker %s", worker.pane_id, worker.name)
                    dead_workers.append(worker)
                    had_action = True
                    continue

                cmd = await get_pane_command(worker.pane_id)
                content = await capture_pane(worker.pane_id)
                new_state = classify_pane_content(cmd, content)
                prev = self._prev_states.get(worker.pane_id, worker.state)
                changed = worker.update_state(new_state)

                # Successful poll — reset failure counter
                self._poll_failures.pop(worker.pane_id, None)

                if changed:
                    # Write state to tmux pane option so borders update
                    await set_pane_option(worker.pane_id, "@swarm_state", worker.state.value)
                    self.emit("state_changed", worker)

                    # Track BUZZING→RESTING/WAITING transitions for bell
                    if prev == WorkerState.BUZZING and worker.state in (
                        WorkerState.RESTING,
                        WorkerState.WAITING,
                    ):
                        any_transitioned_to_resting = True

                self._prev_states[worker.pane_id] = worker.state

                if not self.enabled:
                    continue

                decision = decide(worker, content, self.drone_config, escalated=self._escalated)

                if decision.decision == Decision.CONTINUE:
                    await send_enter(worker.pane_id)
                    self.log.add(DroneAction.CONTINUED, worker.name, decision.reason)
                    had_action = True

                elif decision.decision == Decision.REVIVE:
                    await revive_worker(worker, session_name=self.session_name)
                    worker.record_revive()
                    self.log.add(DroneAction.REVIVED, worker.name, decision.reason)
                    had_action = True

                elif decision.decision == Decision.ESCALATE:
                    self.log.add(DroneAction.ESCALATED, worker.name, decision.reason)
                    self.emit("escalate", worker, decision.reason)
                    had_action = True

            except Exception:
                fails = self._poll_failures.get(worker.pane_id, 0) + 1
                self._poll_failures[worker.pane_id] = fails
                _log.warning(
                    "poll failed for %s (pane %s) (%d/%d)",
                    worker.name,
                    worker.pane_id,
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

        # Remove dead workers
        if dead_workers:
            for dw in dead_workers:
                self.workers.remove(dw)
                self._prev_states.pop(dw.pane_id, None)
                self._poll_failures.pop(dw.pane_id, None)
                self._escalated.discard(dw.pane_id)
                _log.info("removed dead worker: %s", dw.name)
                if self.task_board:
                    self.task_board.unassign_worker(dw.name)
            self.emit("workers_changed")

        # Propose completion for tasks whose workers have been idle long enough
        if self.enabled and self.task_board:
            if self._check_task_completions():
                had_action = True

        # Auto-assign tasks to idle workers (when enabled and board has work)
        if self.enabled and self.task_board and self.queen:
            if await self._auto_assign_tasks():
                had_action = True

        # Periodic Queen coordination cycle
        if (
            self.enabled
            and self.queen
            and self._tick > 0
            and self._tick % _COORDINATION_INTERVAL == 0
        ):
            if await self._coordination_cycle():
                had_action = True

        # Periodic re-discovery
        if self.session_name and self._tick > 0 and self._tick % _REDISCOVERY_INTERVAL == 0:
            await self._rediscover()

        # Post-loop: terminal title, bell, window names (non-critical)
        if self.session_name:
            try:
                await self._update_terminal_ui(any_transitioned_to_resting)
            except Exception:
                _log.debug("terminal UI update failed", exc_info=True)

        self._tick += 1
        return had_action

    async def _rediscover(self) -> None:
        """Re-discover panes and add any new workers not already tracked."""
        if not self.session_name:
            return
        try:
            discovered = await discover_workers(self.session_name)
        except Exception:
            _log.debug("re-discovery failed", exc_info=True)
            return

        known_panes = {w.pane_id for w in self.workers}
        new_workers = [w for w in discovered if w.pane_id not in known_panes]
        if new_workers:
            for w in new_workers:
                _log.info("discovered new worker: %s (pane %s)", w.name, w.pane_id)
                self.workers.append(w)
            self.emit("workers_changed")

    async def _update_terminal_ui(self, bell: bool) -> None:
        """Update terminal title, window names, and ring bell on transitions."""
        counts = worker_state_counts(self.workers)
        buzzing, waiting, resting, total = (
            counts["buzzing"],
            counts["waiting"],
            counts["resting"],
            counts["total"],
        )

        # Terminal title (via tmux's native set-titles)
        if buzzing == total:
            frame = spinner_frame(self._tick)
            title = f"{frame} swarm: all working"
        elif waiting > 0:
            title = f"swarm: {waiting}/{total} WAITING"
        elif resting > 0:
            title = f"swarm: {resting}/{total} IDLE"
        else:
            title = f"swarm: {total} workers"

        await set_terminal_title(self.session_name, title)

        # Window names
        await update_window_names(self.session_name, self.workers)

    # Workers must be RESTING for at least this long before proposing task completion.
    # Prevents premature proposals during brief pauses between Claude actions.
    _AUTO_COMPLETE_MIN_IDLE = 45  # seconds

    def _check_task_completions(self) -> bool:
        """Propose completion for tasks whose assigned worker has been idle long enough.

        Instead of auto-completing, emits a ``task_done`` event so the daemon
        can ask the Queen for an assessment and create a user-approvable proposal.
        """
        if not self.task_board:
            return False

        proposed_any = False
        for worker in self.workers:
            if worker.state != WorkerState.RESTING:
                continue
            if worker.state_duration < self._AUTO_COMPLETE_MIN_IDLE:
                continue
            active_tasks = [
                t
                for t in self.task_board.tasks_for_worker(worker.name)
                if t.status in (TaskStatus.ASSIGNED, TaskStatus.IN_PROGRESS)
            ]
            for task in active_tasks:
                if task.id in self._proposed_completions:
                    continue
                self._proposed_completions.add(task.id)
                self.emit("task_done", worker, task, "")
                self.log.add(
                    DroneAction.CONTINUED,
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
        if not self.task_board or not self.queen or not self.queen.can_call:
            return False

        # Skip if there are already pending proposals awaiting user decision
        if self._pending_proposals_check and self._pending_proposals_check():
            return False

        available = self.task_board.available_tasks
        if not available:
            return False

        # Find resting workers with no active task
        idle_workers = [
            w
            for w in self.workers
            if w.state == WorkerState.RESTING and not self.task_board.tasks_for_worker(w.name)
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
        except Exception:
            _log.warning("Queen assign_tasks failed", exc_info=True)
            return False

        from swarm.tasks.proposal import AssignmentProposal

        proposed_any = False
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

            proposal = AssignmentProposal(
                worker_name=worker_name,
                task_id=task_id,
                task_title=task.title,
                message=message,
                reasoning=reasoning,
                confidence=confidence,
            )
            _log.info("Queen proposed: %s → %s (%s)", worker_name, task.title, task_id)
            self.log.add(DroneAction.CONTINUED, worker_name, f"Queen proposed: {task.title}")
            self.emit("proposal", proposal)
            proposed_any = True

        return proposed_any

    async def _coordination_cycle(self) -> bool:  # noqa: C901
        """Periodic full-hive coordination via Queen.

        Returns ``True`` if any directives were executed.
        """
        if not self.queen or not self.queen.enabled:
            return False

        try:
            from swarm.queen.context import build_hive_context

            worker_outputs: dict[str, str] = {}
            for w in list(self.workers):
                try:
                    worker_outputs[w.name] = await capture_pane(w.pane_id, lines=60)
                except Exception:
                    _log.debug("failed to capture pane for %s in coordination cycle", w.name)

            hive_ctx = build_hive_context(
                list(self.workers),
                worker_outputs=worker_outputs,
                drone_log=self.log,
                task_board=self.task_board,
                worker_descriptions=self.worker_descriptions,
            )
            result = await self.queen.coordinate_hive(hive_ctx)
        except Exception:
            _log.warning("Queen coordination cycle failed", exc_info=True)
            return False

        had_directive = False
        directives = result.get("directives", []) if isinstance(result, dict) else []
        for directive in directives:
            if not isinstance(directive, dict):
                _log.warning("Queen returned non-dict directive entry: %s", type(directive))
                continue
            worker_name = directive.get("worker", "")
            action = directive.get("action", "")
            message = directive.get("message", "")
            reason = directive.get("reason", "")

            worker = next((w for w in self.workers if w.name == worker_name), None)
            if not worker:
                continue

            _log.info("Queen directive: %s → %s (%s)", worker_name, action, reason)

            if action == "send_message" and message:
                try:
                    await send_keys(worker.pane_id, message)
                    self.log.add(DroneAction.CONTINUED, worker_name, f"Queen: {reason}")
                    had_directive = True
                except Exception:
                    _log.warning("failed to send Queen directive to %s", worker_name, exc_info=True)
            elif action == "continue":
                try:
                    await send_enter(worker.pane_id)
                    self.log.add(DroneAction.CONTINUED, worker_name, f"Queen: {reason}")
                    had_directive = True
                except Exception:
                    _log.warning("failed to send Queen continue to %s", worker_name, exc_info=True)
            elif action == "restart":
                try:
                    await revive_worker(worker, session_name=self.session_name)
                    self.log.add(DroneAction.REVIVED, worker_name, f"Queen: {reason}")
                    had_directive = True
                except Exception:
                    _log.warning(
                        "failed to revive %s per Queen directive",
                        worker_name,
                        exc_info=True,
                    )
            elif action == "complete_task":
                task_id = directive.get("task_id", "")
                resolution = directive.get("resolution", reason)
                # Guard: only propose if worker is actually RESTING
                if worker.state != WorkerState.RESTING:
                    _log.info(
                        "Ignoring complete_task for %s: worker %s is %s, not RESTING",
                        task_id,
                        worker_name,
                        worker.state.value,
                    )
                    continue
                task = self.task_board.get(task_id) if task_id and self.task_board else None
                if task and task.status in (TaskStatus.ASSIGNED, TaskStatus.IN_PROGRESS):
                    if task_id not in self._proposed_completions:
                        self._proposed_completions.add(task_id)
                        self.emit("task_done", worker, task, resolution)
                        self.log.add(
                            DroneAction.CONTINUED, worker_name, f"Queen proposes done: {reason}"
                        )
                        had_directive = True
                        _log.info("Queen proposes task %s done for %s", task_id, worker_name)
            elif action == "assign_task":
                task_id = directive.get("task_id", "")
                if task_id and self.task_board and message:
                    from swarm.tasks.proposal import AssignmentProposal

                    task = self.task_board.get(task_id)
                    if task and task.is_available:
                        proposal = AssignmentProposal(
                            worker_name=worker_name,
                            task_id=task_id,
                            task_title=task.title,
                            message=message,
                            reasoning=reason,
                            confidence=0.8,
                        )
                        self.emit("proposal", proposal)
                        self.log.add(
                            DroneAction.CONTINUED, worker_name, f"Queen proposed: {task.title}"
                        )
                        had_directive = True

        conflicts = result.get("conflicts", []) if isinstance(result, dict) else []
        if conflicts:
            _log.warning("Queen detected conflicts: %s", conflicts)

        return had_directive

    async def _loop(self) -> None:
        while self._running:
            backoff = self._base_interval
            try:
                async with self._poll_lock:
                    had_action = await self._poll_once_locked()

                    # Track idle streak for adaptive backoff
                    if had_action:
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
                    if (
                        self.enabled
                        and self.drone_config.auto_stop_on_complete
                        and self.task_board
                        and not self.task_board.available_tasks
                        and not self.task_board.active_tasks
                        and all(w.state == WorkerState.RESTING for w in self.workers)
                    ):
                        self._all_done_streak += 1
                        if self._all_done_streak >= 3:
                            _log.info("all tasks done, all workers idle — hive complete")
                            self.enabled = False
                            self.emit("hive_complete")
                            break
                    else:
                        self._all_done_streak = 0

                    # Exponential backoff: base → 2x → 4x → capped at max
                    backoff = min(
                        self._base_interval * (2 ** min(self._idle_streak, 3)),
                        self._max_interval,
                    )
            except Exception:
                _log.error("poll loop error — recovering next cycle", exc_info=True)

            await asyncio.sleep(backoff)
