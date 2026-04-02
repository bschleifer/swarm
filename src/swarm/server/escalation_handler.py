"""EscalationHandler — escalation, oversight, and notification logic."""

from __future__ import annotations

import time as _time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from swarm.logging import get_logger

if TYPE_CHECKING:
    from swarm.notify.bus import NotificationBus
    from swarm.queen.oversight import OversightResult, OversightSignal
    from swarm.queen.queen import Queen
    from swarm.server.analyzer import QueenAnalyzer
    from swarm.tasks.proposal import ProposalStore
    from swarm.worker.worker import Worker

_log = get_logger("server.escalation_handler")


class EscalationHandler:
    """Owns escalation, oversight alerts, operator approvals, and notifications.

    Extracted from SwarmDaemon to satisfy single-responsibility principle.
    All business logic is identical to the original daemon methods.
    """

    def __init__(
        self,
        *,
        broadcast_ws: Callable[[dict[str, Any]], None],
        notification_bus: NotificationBus,
        proposal_store: ProposalStore,
        get_analyzer: Callable[[], QueenAnalyzer],
        get_queen: Callable[[], Queen],
        emit: Callable[[str, Any, Any], None],
    ) -> None:
        self._broadcast_ws = broadcast_ws
        self._notification_bus = notification_bus
        self._proposal_store = proposal_store
        self._get_analyzer = get_analyzer
        self._get_queen = get_queen
        self._emit = emit

        self._notification_history: list[dict[str, Any]] = []

    def on_escalation(self, worker: Worker, reason: str) -> None:
        """Handle worker escalation — check pending, broadcast, start analysis."""
        # Skip if there's already a pending escalation proposal for this worker
        if self._proposal_store.has_pending_escalation(worker.name):
            _log.debug("skipping escalation for %s — pending proposal exists", worker.name)
            return

        # Skip if Queen is already analyzing this worker
        analyzer = self._get_analyzer()
        if analyzer.has_inflight_escalation(worker.name):
            _log.debug("skipping escalation for %s — analysis already in flight", worker.name)
            return

        self._notification_bus.emit_escalation(worker.name, reason)
        self._broadcast_ws(
            {
                "type": "escalation",
                "worker": worker.name,
                "reason": reason,
            }
        )
        self._emit("escalation", worker, reason)

        # Trigger Queen analysis if enabled
        queen = self._get_queen()
        if queen.enabled and queen.can_call:
            analyzer.start_escalation(worker, reason)

    def on_oversight_alert(
        self, worker: Worker, signal: OversightSignal, result: OversightResult
    ) -> None:
        """Handle critical oversight alert — notify human via dashboard."""
        from swarm.queen.oversight import OversightResult as _OversightResult

        if not isinstance(result, _OversightResult):
            return
        self.push_notification(
            event="queen_oversight",
            worker=worker.name,
            message=result.message or result.reasoning,
            priority="high",
        )
        self._broadcast_ws(
            {
                "type": "oversight_alert",
                "worker": worker.name,
                "signal": result.signal.signal_type.value,
                "severity": result.severity.value,
                "message": result.message,
                "reasoning": result.reasoning,
            }
        )

    def on_operator_terminal_approval(
        self,
        worker: Worker,
        summary: str,
        prompt_type: str,
        pattern: str,
        prompt_snippet: str = "",
    ) -> None:
        """Broadcast operator terminal approval so the dashboard can offer Approve Always."""
        self._broadcast_ws(
            {
                "type": "operator_terminal_approval",
                "worker": worker.name,
                "summary": summary,
                "prompt_type": prompt_type,
                "pattern": pattern,
                "prompt_snippet": prompt_snippet,
            }
        )

    def push_notification(
        self,
        *,
        event: str,
        worker: str,
        message: str,
        priority: str = "medium",
    ) -> None:
        """Push a notification to dashboard clients and store in history."""
        notif = {
            "type": "notification",
            "event": event,
            "worker": worker,
            "message": message,
            "priority": priority,
            "timestamp": _time.time(),
        }
        self._notification_history.append(notif)
        # Cap history at 50 entries
        if len(self._notification_history) > 50:
            self._notification_history = self._notification_history[-50:]
        self._broadcast_ws(notif)

    async def coordinate_hive(self, *, force: bool = False) -> dict[str, Any]:
        """Delegate to QueenAnalyzer."""
        return await self._get_analyzer().coordinate(force=force)
