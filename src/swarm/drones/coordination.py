"""Worker output capture for drone directive execution.

Previously (before task-A/B cleanup of the legacy hive-coordination caller)
this module also housed :meth:`coordination_cycle`, a periodic full-hive Queen
coordination sweep that fired every ~5 minutes.  The audit in
``docs/specs/headless-queen-architecture.md`` found its hit rate was low and
its coverage was already duplicated by specialized drones (IdleWatcher,
InterWorkerMessageWatcher, FileOwnership, PressureManager) — it was deleted
in Task B of that spec.

What's left: a thin helper that snapshots worker PTY content for the
:class:`~swarm.drones.directives.DirectiveExecutor` pipeline.  The class name
is kept for backwards-import compatibility.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from swarm.logging import get_logger
from swarm.worker.worker import Worker, WorkerState

if TYPE_CHECKING:
    pass

_log = get_logger("drones.coordination")


class CoordinationHandler:
    """Captures worker PTY output for the directive-execution pipeline.

    Previously handled periodic full-hive Queen coordination — that cycle
    was removed (see module docstring).  The class is preserved (renamed
    purpose; same import path) because the directive executor's
    ``capture_outputs`` callback still depends on :meth:`capture_worker_outputs`.
    """

    def __init__(
        self,
        workers: list[Worker],
        escalated: dict[str, float],
    ) -> None:
        self.workers = workers
        self._escalated = escalated

    def capture_worker_outputs(self) -> dict[str, str]:
        """Capture worker PTY output for directive execution.

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
