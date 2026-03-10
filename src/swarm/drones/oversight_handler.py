"""OversightHandler — signal-triggered monitoring and intervention dispatch."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from swarm.drones.log import LogCategory, SystemAction
from swarm.logging import get_logger
from swarm.worker.worker import Worker

if TYPE_CHECKING:
    from collections.abc import Callable

    from swarm.drones.log import DroneLog
    from swarm.queen.oversight import OversightMonitor, OversightResult
    from swarm.queen.queen import Queen
    from swarm.tasks.board import TaskBoard

_log = get_logger("drones.oversight")


class OversightHandler:
    """Handles oversight signal detection and Queen evaluation.

    Extracted from :class:`~swarm.drones.pilot.DronePilot` to reduce
    pilot.py complexity.
    """

    def __init__(
        self,
        workers: list[Worker],
        log: DroneLog,
        queen: Queen | None,
        task_board: TaskBoard | None,
        oversight_monitor: OversightMonitor | None,
        emit: Callable[..., None],
        capture_outputs: Callable[[], dict[str, str]],
    ) -> None:
        self.workers = workers
        self.log = log
        self.queen = queen
        self.task_board = task_board
        self._oversight = oversight_monitor
        self._emit = emit
        self._capture_outputs = capture_outputs

    def set_oversight(self, monitor: OversightMonitor | None) -> None:
        """Update the oversight monitor reference."""
        self._oversight = monitor

    async def oversight_cycle(self) -> bool:
        """Run oversight signal detection and Queen evaluation.

        Returns ``True`` if any intervention was triggered.
        """
        monitor = self._oversight
        if monitor is None or not monitor.enabled or not self.queen:
            return False

        worker_outputs = self._capture_outputs()
        signals = monitor.collect_signals(self.workers, self.task_board, worker_outputs)
        if not signals:
            return False

        had_action = False
        for signal in signals:
            self.log.add(
                SystemAction.OVERSIGHT_SIGNAL,
                signal.worker_name,
                f"{signal.signal_type.value}: {signal.description}",
                category=LogCategory.QUEEN,
            )

            output = worker_outputs.get(signal.worker_name, "")
            task_info = ""
            if signal.task_id and self.task_board:
                task = self.task_board.get(signal.task_id)
                if task:
                    task_info = f"{task.title}: {task.description}"

            result = await monitor.evaluate_signal(signal, self.queen, output, task_info)
            if result is None:
                self.log.add(
                    SystemAction.OVERSIGHT_RATE_LIMITED,
                    signal.worker_name,
                    f"oversight rate limited: {signal.signal_type.value}",
                    category=LogCategory.QUEEN,
                )
                continue

            acted = await self._handle_oversight_result(result)
            if acted:
                had_action = True

        return had_action

    async def _handle_oversight_result(self, result: OversightResult) -> bool:
        """Execute the intervention recommended by oversight evaluation."""
        from swarm.queen.oversight import Severity

        worker = next(
            (w for w in self.workers if w.name == result.signal.worker_name),
            None,
        )
        if not worker:
            return False

        detail = f"oversight {result.severity.value}: {result.action} — {result.reasoning}"
        self.log.add(
            SystemAction.OVERSIGHT_INTERVENTION,
            worker.name,
            detail,
            category=LogCategory.QUEEN,
            is_notification=result.severity != Severity.MINOR,
        )

        if result.action == "note":
            _log.info("oversight note for %s: %s", worker.name, result.message[:80])
            return True

        elif result.action == "redirect" and worker.process:
            if not worker.process.is_user_active and result.message:
                await worker.process.send_interrupt()
                await asyncio.sleep(1.0)
                await worker.process.send_keys(result.message + "\n")
                _log.info(
                    "oversight redirected %s: %s",
                    worker.name,
                    result.message[:80],
                )
                return True

        elif result.action == "flag_human":
            self._emit(
                "oversight_alert",
                worker,
                result.signal,
                result,
            )
            _log.info(
                "oversight flagged %s for human review: %s",
                worker.name,
                result.reasoning[:80],
            )
            return True

        return False
