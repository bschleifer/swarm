"""CoordinationHandler — periodic full-hive Queen coordination."""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Any

from swarm.drones.log import LogCategory, SystemAction
from swarm.logging import get_logger
from swarm.pty.process import ProcessError
from swarm.worker.worker import Worker, WorkerState

if TYPE_CHECKING:
    from collections.abc import Callable

    from swarm.drones.directives import DirectiveExecutor
    from swarm.drones.log import DroneLog
    from swarm.queen.queen import Queen
    from swarm.tasks.board import TaskBoard

_log = get_logger("drones.coordination")


class CoordinationHandler:
    """Handles periodic full-hive coordination via Queen.

    Extracted from :class:`~swarm.drones.pilot.DronePilot` to reduce
    pilot.py complexity.
    """

    def __init__(
        self,
        workers: list[Worker],
        log: DroneLog,
        queen: Queen | None,
        task_board: TaskBoard | None,
        build_context: Callable[..., str],
        directive_executor: DirectiveExecutor,
        pending_proposals_check: Callable[[], bool] | None,
        escalated: dict[str, float],
    ) -> None:
        self.workers = workers
        self.log = log
        self.queen = queen
        self.task_board = task_board
        self._build_context = build_context
        self._directive_executor = directive_executor
        self._pending_proposals_check = pending_proposals_check
        self._escalated = escalated
        # Coordination skip: snapshot of worker states + task counts.
        self._prev_coordination_snapshot: dict[str, str | int] | None = None

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

    def capture_worker_outputs(self) -> dict[str, str]:
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

    async def _process_coordination_result(self, result: Any, start_time: float) -> bool:
        """Process Queen coordination result — execute directives and log."""
        directives = result.get("directives", []) if isinstance(result, dict) else []
        top_confidence = float(result.get("confidence", 0.0)) if isinstance(result, dict) else 0.0
        had_directive = await self._directive_executor.execute_directives(
            directives, confidence=top_confidence
        )

        # Reset snapshot so the next cycle re-evaluates after a real action.
        if had_directive:
            self._prev_coordination_snapshot = None

        if had_directive:
            parts = [
                f"{d.get('worker', '?')}→{d.get('action', '?')}"
                for d in directives
                if isinstance(d, dict)
            ]
            summary = ", ".join(parts) if parts else f"{len(directives)} directives"
            self.log.add(
                SystemAction.QUEEN_PROPOSAL,
                "hive",
                f"coordination ({top_confidence:.0%}): {summary}",
                category=LogCategory.QUEEN,
                metadata={
                    "duration_s": round(time.time() - start_time, 1),
                    "directive_count": len(directives),
                    "confidence": top_confidence,
                },
            )
        else:
            _log.debug(
                "coordination cycle: %d directives (all no-op, %.1fs)",
                len(directives),
                time.time() - start_time,
            )
        return had_directive

    async def coordination_cycle(self) -> bool:
        """Periodic full-hive coordination via Queen.

        Returns ``True`` if any directives were executed.
        """
        if not self.queen or not self.queen.enabled:
            return False

        # Skip if there are already pending proposals awaiting user decision
        if self._pending_proposals_check and self._pending_proposals_check():
            return False

        # Skip coordination when all workers are actively BUZZING
        worker_states = {w.state for w in self.workers}
        if worker_states == {WorkerState.BUZZING}:
            _log.debug("coordination skipped: all %d workers BUZZING", len(self.workers))
            return False

        if self._coordination_snapshot_unchanged():
            _log.debug("coordination skipped: hive state unchanged")
            return False

        _start = time.time()
        try:
            worker_outputs = self.capture_worker_outputs()

            hive_ctx = self._build_context(worker_outputs=worker_outputs)
            result = await self.queen.coordinate_hive(hive_ctx)
        except asyncio.CancelledError:
            _log.info("coordination cycle cancelled (shutdown)")
            return False
        except (TimeoutError, RuntimeError, ProcessError, OSError):
            _log.warning("Queen coordination cycle failed", exc_info=True)
            return False

        had_directive = await self._process_coordination_result(result, _start)

        conflicts = result.get("conflicts", []) if isinstance(result, dict) else []
        if conflicts:
            _log.warning("Queen detected conflicts: %s", conflicts)

        return had_directive
