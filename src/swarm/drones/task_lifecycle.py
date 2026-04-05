"""TaskLifecycle — task completion checking and auto-assignment."""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, ClassVar

from swarm.drones.log import DroneAction
from swarm.logging import get_logger
from swarm.pty.process import ProcessError
from swarm.tasks.task import TaskStatus
from swarm.worker.worker import Worker, WorkerState

if TYPE_CHECKING:
    from collections.abc import Callable

    from swarm.config import DroneConfig
    from swarm.drones.log import DroneLog
    from swarm.queen.queen import Queen
    from swarm.tasks.board import TaskBoard

_log = get_logger("drones.task_lifecycle")


class TaskLifecycle:
    """Handles task completion checks and auto-assignment via Queen.

    Extracted from :class:`~swarm.drones.pilot.DronePilot` to reduce
    pilot.py complexity.
    """

    # Default idle threshold -- overridden by drone_config.auto_complete_min_idle in __init__
    _AUTO_COMPLETE_MIN_IDLE: ClassVar[int] = 45

    # If the Queen initially rejected a completion, wait this long before
    # re-proposing.  Prevents spam while still catching tasks that are truly
    # done after the initial check said "not done".
    _COMPLETION_REPROPOSE_COOLDOWN: ClassVar[int] = 300  # 5 minutes

    # Interval (in ticks) between stale proposed-completion cleanup sweeps
    _PROPOSED_COMPLETION_CLEANUP_INTERVAL: ClassVar[int] = 60
    # Max age (seconds) for proposed-completion entries before eviction
    _PROPOSED_COMPLETION_MAX_AGE: ClassVar[float] = 3600.0
    _PROPOSED_COMPLETION_MAX_SIZE: ClassVar[int] = 500

    def __init__(
        self,
        workers: list[Worker],
        log: DroneLog,
        task_board: TaskBoard | None,
        queen: Queen | None,
        drone_config: DroneConfig,
        proposed_completions: dict[str, float],
        idle_consecutive: dict[str, int],
        emit: Callable[..., None],
        build_context: Callable[..., str],
        pending_proposals_check: Callable[[], bool] | None,
        pending_proposals_for_worker: Callable[[str], bool] | None,
    ) -> None:
        self.workers = workers
        self.log = log
        self.task_board = task_board
        self.queen = queen
        self.drone_config = drone_config
        self._proposed_completions = proposed_completions
        self._idle_consecutive = idle_consecutive
        self._emit = emit
        self._build_context = build_context
        self._pending_proposals_check = pending_proposals_check
        self._pending_proposals_for_worker = pending_proposals_for_worker
        self._auto_complete_min_idle = drone_config.auto_complete_min_idle
        self._needs_assign_check: bool = False
        self._saw_completion: bool = False

    def mark_completion_seen(self) -> None:
        """Signal that a task completion occurred during this pilot session."""
        self._saw_completion = True

    @property
    def saw_completion(self) -> bool:
        """Whether a task completion occurred during this pilot session."""
        return self._saw_completion

    @property
    def needs_assign_check(self) -> bool:
        """Whether an assign check is needed."""
        return self._needs_assign_check

    @needs_assign_check.setter
    def needs_assign_check(self, value: bool) -> None:
        self._needs_assign_check = value

    def set_auto_complete_idle(self, seconds: float) -> None:
        """Override the minimum idle time before proposing task completion."""
        self._auto_complete_min_idle = seconds

    def clear_proposed_completion(self, task_id: str) -> None:
        """Remove a task from the proposed-completions tracker.

        Called by the daemon when a completion proposal is rejected or the
        task is unassigned, allowing the pilot to re-propose later.
        """
        self._proposed_completions.pop(task_id, None)

    def _should_eager_assign(self) -> bool:
        """Check if idle-escalation or event-driven flag should trigger assign."""
        if self._needs_assign_check:
            return True
        threshold = self.drone_config.idle_assign_threshold
        if not self.task_board or not self.task_board.available_tasks:
            return False
        return any(v >= threshold for v in self._idle_consecutive.values())

    def _cleanup_stale_proposed_completions(self) -> None:
        """Evict proposed-completion entries older than 1 hour to prevent unbounded growth."""
        if not self._proposed_completions:
            return
        cutoff = time.time() - self._PROPOSED_COMPLETION_MAX_AGE
        stale = [k for k, ts in self._proposed_completions.items() if ts < cutoff]
        for k in stale:
            del self._proposed_completions[k]
        # Size-based safeguard: keep only the most recent entries
        if len(self._proposed_completions) > self._PROPOSED_COMPLETION_MAX_SIZE:
            sorted_keys = sorted(
                self._proposed_completions, key=lambda k: self._proposed_completions.get(k, 0.0)
            )
            for k in sorted_keys[: -self._PROPOSED_COMPLETION_MAX_SIZE]:
                del self._proposed_completions[k]

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
                        "re-proposing completion for task %s (%s) -- %.0fs since last attempt",
                        task.id,
                        task.title,
                        now - last_proposed,
                    )
                self._proposed_completions[task.id] = now
                self._emit("task_done", worker, task, "")
                self.log.add(
                    DroneAction.PROPOSED_COMPLETION,
                    worker.name,
                    f"task appears done: {task.title}",
                )
                _log.info(
                    "proposing completion for task %s (%s) -- worker %s idle %.0fs",
                    task.id,
                    task.title,
                    worker.name,
                    worker.state_duration,
                )
                proposed_any = True
        return proposed_any

    async def _auto_assign_tasks(self) -> bool:  # noqa: C901
        """Ask Queen for assignments and emit proposals for user approval.

        Returns ``True`` if any proposals were created.
        """
        if not self.task_board or not self.queen:
            return False

        available = self.task_board.available_tasks
        if not available:
            return False

        # Snapshot assigned/in-progress tasks once; active_tasks_for_worker() would
        # rescan the full task list per worker (O(workers × tasks)).
        workers_with_active: set[str] = {
            t.assigned_worker for t in self.task_board.active_tasks if t.assigned_worker
        }
        # Find resting workers with no active task and no pending proposals
        idle_workers = [
            w
            for w in self.workers
            if w.state == WorkerState.RESTING
            and w.name not in workers_with_active
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
            hive_ctx = self._build_context()
            assignments = await self.queen.assign_tasks(
                [w.name for w in idle_workers],
                task_dicts,
                hive_context=hive_ctx,
            )
        except asyncio.CancelledError:
            _log.info("auto-assign cancelled (shutdown)")
            return False
        except (TimeoutError, RuntimeError, ProcessError, OSError):
            _log.warning("Queen assign_tasks failed", exc_info=True)
            return False

        from swarm.tasks.proposal import AssignmentProposal

        acted = False
        auto_approve = self.drone_config.auto_approve_assignments
        min_conf = getattr(self.queen, "min_confidence", 0.7)
        workers_by_name = {w.name: w for w in self.workers}
        for assignment in assignments:
            if not isinstance(assignment, dict):
                _log.warning("Queen returned non-dict assignment entry: %s", type(assignment))
                continue
            worker_name = assignment.get("worker", "")
            task_id = assignment.get("task_id", "")
            message = assignment.get("message", "")
            reasoning = assignment.get("reasoning", "")
            try:
                confidence = float(assignment.get("confidence", 0.8))
            except (ValueError, TypeError):
                confidence = 0.5

            worker = workers_by_name.get(worker_name)
            task = self.task_board.get(task_id) if task_id else None

            if not worker or not task or not task.is_available:
                continue

            # Auto-approve high-confidence assignments directly
            if auto_approve and confidence >= min_conf:
                _log.info(
                    "auto-approving assignment: %s -> %s (conf=%.2f)",
                    worker_name,
                    task.title,
                    confidence,
                )
                self.log.add(
                    DroneAction.AUTO_ASSIGNED,
                    worker_name,
                    f"auto-assigned: {task.title} (conf={confidence:.0%})",
                )
                self._emit("task_assigned", worker, task, message)
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
            _log.info("Queen proposed: %s -> %s (%s)", worker_name, task.title, task_id)
            self.log.add(
                DroneAction.PROPOSED_ASSIGNMENT, worker_name, f"Queen proposed: {task.title}"
            )
            self._emit("proposal", proposal)
            acted = True

        return acted
