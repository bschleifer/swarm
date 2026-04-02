"""StatePublisher — broadcasts worker/task/pipeline state changes over WebSocket."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from swarm.logging import get_logger
from swarm.tunnel import TunnelState
from swarm.worker.worker import Worker, WorkerState

if TYPE_CHECKING:
    from swarm.drones.log import DroneLog, SystemEntry
    from swarm.notify.bus import NotificationBus
    from swarm.pipelines.engine import PipelineEngine
    from swarm.services.registry import ServiceRegistry
    from swarm.tasks.proposal import AssignmentProposal

_log = get_logger("server.state_publisher")


def _log_task_exception(task: asyncio.Task[object]) -> None:
    """Log unhandled exceptions from fire-and-forget tasks."""
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        _log.error("fire-and-forget task failed: %s", exc, exc_info=exc)


class StatePublisher:
    """Owns worker/task/pipeline state broadcasting and debounce logic.

    Extracted from SwarmDaemon to satisfy single-responsibility principle.
    All business logic is identical to the original daemon methods.
    """

    def __init__(
        self,
        *,
        broadcast_ws: Callable[[dict[str, Any]], None],
        get_workers: Callable[[], list[Worker]],
        get_worker_task_map: Callable[[], dict[str, str]],
        expire_proposals: Callable[[], None],
        broadcast_proposals: Callable[[], None],
        clear_worker_inflight: Callable[[str], None],
        pending_for_worker: Callable[[str], list[AssignmentProposal]],
        clear_resolved_proposals: Callable[[], None],
        push_notification: Callable[..., None],
        notification_bus: NotificationBus,
        drone_log: DroneLog,
        emit: Callable[[str], None],
        get_pressure_level: Callable[[], str],
        pipeline_engine: PipelineEngine,
        service_registry: ServiceRegistry,
        track_task: Callable[[asyncio.Task[object]], None],
        mark_dirty: Callable[[], None] | None = None,
        debounce_delay: float = 0.3,
    ) -> None:
        self._broadcast_ws = broadcast_ws
        self._get_workers = get_workers
        self._get_worker_task_map = get_worker_task_map
        self._expire_proposals = expire_proposals
        self._broadcast_proposals = broadcast_proposals
        self._clear_worker_inflight = clear_worker_inflight
        self._pending_for_worker = pending_for_worker
        self._clear_resolved_proposals = clear_resolved_proposals
        self._push_notification = push_notification
        self._notification_bus = notification_bus
        self._drone_log = drone_log
        self._emit = emit
        self._get_pressure_level = get_pressure_level
        self._pipeline_engine = pipeline_engine
        self._service_registry = service_registry
        self._track_task = track_task
        self._mark_dirty_cb = mark_dirty

        # Debounced state broadcasts
        self._state_dirty: bool = False
        self._state_debounce_handle: asyncio.TimerHandle | None = None
        self._state_debounce_delay: float = debounce_delay

    # --- Task board ---

    def on_task_board_changed(self) -> None:
        self._broadcast_ws({"type": "tasks_changed"})
        self._expire_proposals()

    # --- Pipeline ---

    def on_pipeline_change(self) -> None:
        self._broadcast_ws({"type": "pipelines_changed"})
        try:
            asyncio.get_running_loop()
            task = asyncio.create_task(self._run_automated_steps())
            task.add_done_callback(_log_task_exception)
            self._track_task(task)
        except RuntimeError:
            pass  # No event loop (test/CLI context)

    async def _run_automated_steps(self) -> None:
        """Check all pipelines for READY automated steps and run them."""
        from swarm.pipelines.models import StepStatus, StepType
        from swarm.services.registry import ServiceContext

        for pipeline in self._pipeline_engine.list_all():
            for step in pipeline.steps:
                if step.status == StepStatus.READY and step.step_type == StepType.AUTOMATED:
                    if not step.service:
                        continue
                    if not self._service_registry.has(step.service):
                        _log.warning("no handler for service %s", step.service)
                        continue
                    step.start()
                    ctx = ServiceContext(
                        pipeline_id=pipeline.id,
                        step_id=step.id,
                        pipeline_name=pipeline.name,
                        step_name=step.name,
                    )
                    result = await self._service_registry.execute(step.service, step.config, ctx)
                    if result.success:
                        self._pipeline_engine.complete_step(
                            pipeline.id, step.id, result=result.data
                        )
                    else:
                        self._pipeline_engine.fail_step(pipeline.id, step.id, error=result.error)

    # --- Worker state ---

    def on_state_changed(self, worker: Worker) -> None:
        """Called when any worker changes state — push to WS clients."""
        from swarm.tasks.proposal import ProposalStatus, ProposalType

        # When a worker resumes working, expire stale escalation AND completion
        # proposals — the worker is no longer idle so the proposals are outdated.
        if worker.state == WorkerState.BUZZING:
            # Clear in-flight analysis tracking
            self._clear_worker_inflight(worker.name)
            pending = self._pending_for_worker(worker.name)
            stale = [
                p
                for p in pending
                if p.proposal_type in (ProposalType.ESCALATION, ProposalType.COMPLETION)
            ]
            if stale:
                for p in stale:
                    p.status = ProposalStatus.EXPIRED
                self._clear_resolved_proposals()
                self._broadcast_proposals()

        # Log STUNG transitions to system log
        if worker.state == WorkerState.STUNG:
            from swarm.drones.log import LogCategory, SystemAction

            self._drone_log.add(
                SystemAction.WORKER_STUNG,
                worker.name,
                "worker exited",
                category=LogCategory.WORKER,
                is_notification=True,
            )

        if self._mark_dirty_cb is not None:
            self._mark_dirty_cb()
        else:
            self._mark_state_dirty()

    def _mark_state_dirty(self) -> None:
        """Schedule a debounced state broadcast.

        Multiple state changes within ``_state_debounce_delay`` seconds are
        coalesced into a single WebSocket broadcast.
        """
        self._state_dirty = True
        if self._state_debounce_handle is not None:
            self._state_debounce_handle.cancel()
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # No event loop (test/CLI context) — flush immediately
            self._flush_state_broadcast()
            return
        self._state_debounce_handle = loop.call_later(
            self._state_debounce_delay, self._flush_state_broadcast
        )

    def _flush_state_broadcast(self) -> None:
        """Send the coalesced state broadcast if dirty."""
        if not self._state_dirty:
            return
        self._state_dirty = False
        self._state_debounce_handle = None
        self.broadcast_state()

    # --- Drone log ---

    def on_drone_entry(self, entry: SystemEntry) -> None:
        self._broadcast_ws(
            {
                "type": "system_log",
                "action": entry.action.value,
                "worker": entry.worker_name,
                "detail": entry.detail,
                "category": entry.category.value,
                "is_notification": entry.is_notification,
            }
        )
        # Push notification for notification-worthy entries
        if entry.is_notification:
            self._push_notification(
                event=entry.action.value.lower(),
                worker=entry.worker_name,
                message=entry.detail,
                priority="high"
                if entry.action.value
                in (
                    "WORKER_STUNG",
                    "TASK_FAILED",
                )
                else "medium",
            )

    # --- Full state broadcast ---

    def broadcast_state(self) -> None:
        """Push current worker states to all WS clients."""
        self._broadcast_ws(
            {
                "type": "state",
                "workers": [
                    {
                        "name": w.name,
                        "state": w.display_state.value,
                        "state_duration": round(w.state_duration, 1),
                    }
                    for w in self._get_workers()
                ],
                "pressure_level": self._get_pressure_level(),
            }
        )

    # --- Workers changed ---

    def on_workers_changed(self) -> None:
        task_map = self._get_worker_task_map()
        self._broadcast_ws(
            {
                "type": "workers_changed",
                "workers": [{"name": w.name, "state": w.state.value} for w in self._get_workers()],
                "worker_tasks": task_map,
            }
        )
        self._expire_proposals()
        self._emit("workers_changed")

    # --- Usage ---

    def broadcast_usage(self) -> None:
        """Broadcast aggregated usage to all WS clients."""
        workers_usage: dict[str, object] = {}
        total_cost = 0.0
        total_tokens = 0
        for w in self._get_workers():
            workers_usage[w.name] = w.usage.to_dict()
            total_cost += w.usage.cost_usd
            total_tokens += w.usage.total_tokens
        self._broadcast_ws(
            {
                "type": "usage_updated",
                "workers": workers_usage,
                "total": {
                    "cost_usd": round(total_cost, 4),
                    "total_tokens": total_tokens,
                },
            }
        )

    # --- Tunnel ---

    def on_tunnel_state_change(self, state: TunnelState, detail: str) -> None:
        """Broadcast tunnel state changes to all WS clients."""
        if state == TunnelState.RUNNING:
            self._broadcast_ws({"type": "tunnel_started", "url": detail})
        elif state == TunnelState.STOPPED:
            self._broadcast_ws({"type": "tunnel_stopped"})
        elif state == TunnelState.ERROR:
            self._broadcast_ws({"type": "tunnel_error", "error": detail})
