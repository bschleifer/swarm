"""ProposalManager — handles Queen proposal lifecycle."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from swarm.drones.log import DroneAction, LogCategory, SystemAction
from swarm.logging import get_logger
from swarm.tasks.proposal import AssignmentProposal, ProposalStatus, ProposalStore
from swarm.worker.worker import WorkerState

if TYPE_CHECKING:
    from swarm.server.daemon import SwarmDaemon

_log = get_logger("server.proposals")


class ProposalManager:
    """Manages Queen proposal lifecycle: creation, approval, rejection, expiry."""

    def __init__(self, store: ProposalStore, daemon: SwarmDaemon) -> None:
        self.store = store
        self._daemon = daemon

    @property
    def pending(self) -> list[AssignmentProposal]:
        return self.store.pending

    def on_proposal(self, proposal: AssignmentProposal) -> None:
        """Accept a new proposal: dedup, store, log, broadcast, notify."""
        d = self._daemon
        # Final dedup gate — reject if a matching pending proposal already exists
        pending = self.store.pending_for_worker(proposal.worker_name)
        for p in pending:
            if p.proposal_type == proposal.proposal_type:
                # For task-specific proposals, match on task_id
                if proposal.task_id and p.task_id == proposal.task_id:
                    _log.debug(
                        "dropping duplicate %s proposal for %s (task %s)",
                        proposal.proposal_type,
                        proposal.worker_name,
                        proposal.task_id,
                    )
                    return
                # For escalations without task_id, one per worker is enough
                if not proposal.task_id and proposal.proposal_type == "escalation":
                    _log.debug(
                        "dropping duplicate escalation proposal for %s",
                        proposal.worker_name,
                    )
                    return
        self.store.add(proposal)
        # Log to system log based on proposal type
        if proposal.proposal_type == "escalation":
            d.drone_log.add(
                SystemAction.QUEEN_ESCALATION,
                proposal.worker_name,
                proposal.assessment or proposal.reasoning or "escalation",
                category=LogCategory.QUEEN,
                is_notification=True,
            )
        elif proposal.proposal_type == "completion":
            d.drone_log.add(
                SystemAction.QUEEN_COMPLETION,
                proposal.worker_name,
                proposal.task_title or "completion",
                category=LogCategory.QUEEN,
                is_notification=True,
            )
        else:
            d.drone_log.add(
                SystemAction.QUEEN_PROPOSAL,
                proposal.worker_name,
                proposal.task_title or proposal.assessment or "proposal",
                category=LogCategory.QUEEN,
                is_notification=True,
            )
        d.broadcast_ws(
            {
                "type": "proposal_created",
                "proposal": self.proposal_dict(proposal),
                "pending_count": len(self.store.pending),
            }
        )
        if proposal.proposal_type == "escalation":
            d.notification_bus.emit_escalation(
                proposal.worker_name,
                f"Queen escalation: {proposal.assessment or proposal.task_title}",
            )
        else:
            d.notification_bus.emit_task_assigned(
                proposal.worker_name,
                f"Proposal: {proposal.task_title}",
            )
        # Escalation proposals pop up a modal so the user sees them immediately
        if proposal.proposal_type == "escalation":
            d.broadcast_ws(
                {
                    "type": "queen_escalation",
                    "proposal_id": proposal.id,
                    "worker": proposal.worker_name,
                    "assessment": proposal.assessment,
                    "reasoning": proposal.reasoning,
                    "action": proposal.queen_action,
                    "message": proposal.message,
                    "confidence": proposal.confidence,
                }
            )
        # Completion proposals also pop a modal with task resolution details
        elif proposal.proposal_type == "completion":
            task = d.task_board.get(proposal.task_id)
            has_email = bool(task and task.source_email_id)
            d.broadcast_ws(
                {
                    "type": "queen_completion",
                    "proposal_id": proposal.id,
                    "worker": proposal.worker_name,
                    "task_id": proposal.task_id,
                    "task_title": proposal.task_title,
                    "assessment": proposal.assessment,
                    "reasoning": proposal.reasoning,
                    "confidence": proposal.confidence,
                    "has_source_email": has_email,
                }
            )

    def expire_stale(self) -> None:
        """Expire proposals where the task or worker is no longer valid."""
        d = self._daemon
        valid_task_ids = {t.id for t in d.task_board.available_tasks}
        valid_worker_names = {w.name for w in d.workers}
        expired = self.store.expire_stale(valid_task_ids, valid_worker_names)
        if expired:
            self.store.clear_resolved()
            self.broadcast()

    def proposal_dict(self, proposal: AssignmentProposal) -> dict[str, Any]:
        """Serialize a proposal for WebSocket / JSON responses."""
        result: dict = {
            "id": proposal.id,
            "worker_name": proposal.worker_name,
            "task_id": proposal.task_id,
            "task_title": proposal.task_title,
            "message": proposal.message,
            "reasoning": proposal.reasoning,
            "confidence": proposal.confidence,
            "proposal_type": proposal.proposal_type,
            "assessment": proposal.assessment,
            "queen_action": proposal.queen_action,
            "status": proposal.status.value,
            "created_at": proposal.created_at,
            "age": round(proposal.age, 1),
        }
        if proposal.proposal_type == "completion" and proposal.task_id:
            task = self._daemon.task_board.get(proposal.task_id)
            result["has_source_email"] = bool(task and task.source_email_id)
        return result

    def broadcast(self) -> None:
        """Push current proposals to all WS clients."""
        pending = self.store.pending
        self._daemon.broadcast_ws(
            {
                "type": "proposals_changed",
                "proposals": [self.proposal_dict(p) for p in pending],
                "pending_count": len(pending),
            }
        )

    async def approve(self, proposal_id: str, draft_response: bool = False) -> bool:
        """Approve a Queen proposal: assign task or execute escalation action.

        When *draft_response* is True and the proposal is a completion with a
        source email, the reply pipeline is triggered.
        """
        from swarm.server.daemon import TaskOperationError, WorkerNotFoundError

        d = self._daemon
        proposal = self.store.get(proposal_id)
        if not proposal or proposal.status != ProposalStatus.PENDING:
            raise TaskOperationError(f"Proposal '{proposal_id}' not found or not pending")

        worker = d.get_worker(proposal.worker_name)
        if not worker:
            proposal.status = ProposalStatus.EXPIRED
            self.store.clear_resolved()
            self.broadcast()
            raise WorkerNotFoundError(f"Worker '{proposal.worker_name}' no longer exists")

        if proposal.proposal_type == "escalation":
            await d.analyzer.execute_escalation(proposal)
            proposal.status = ProposalStatus.APPROVED
            d.drone_log.add(
                DroneAction.APPROVED,
                proposal.worker_name,
                f"proposal approved: {proposal.queen_action}",
            )
            self.store.clear_resolved()
            self.broadcast()
            return True

        if proposal.proposal_type == "completion":
            resolution = proposal.assessment or proposal.reasoning or ""
            d.complete_task(
                proposal.task_id, actor="queen", resolution=resolution, send_reply=draft_response
            )
            proposal.status = ProposalStatus.APPROVED
            d.drone_log.add(
                DroneAction.APPROVED,
                proposal.worker_name,
                f"task completed: {proposal.task_title}",
            )
            self.store.clear_resolved()
            self.broadcast()
            return True

        if worker.state not in (WorkerState.RESTING, WorkerState.WAITING):
            proposal.status = ProposalStatus.EXPIRED
            self.store.clear_resolved()
            self.broadcast()
            raise TaskOperationError(
                f"Worker '{proposal.worker_name}' is {worker.state.value}, not idle"
            )

        await d.assign_task(
            proposal.task_id,
            proposal.worker_name,
            actor="queen",
            message=proposal.message or None,
        )
        proposal.status = ProposalStatus.APPROVED
        d.drone_log.add(
            DroneAction.APPROVED,
            proposal.worker_name,
            f"proposal approved: {proposal.task_title}",
        )
        self.store.clear_resolved()
        self.broadcast()
        return True

    def reject(self, proposal_id: str) -> bool:
        """Reject a Queen proposal."""
        from swarm.server.daemon import TaskOperationError

        d = self._daemon
        proposal = self.store.get(proposal_id)
        if not proposal or proposal.status != ProposalStatus.PENDING:
            raise TaskOperationError(f"Proposal '{proposal_id}' not found or not pending")
        proposal.status = ProposalStatus.REJECTED
        # Allow pilot to re-propose this task if it stays idle
        if proposal.proposal_type == "completion" and proposal.task_id:
            d.pilot.clear_proposed_completion(proposal.task_id)
        d.drone_log.add(
            DroneAction.REJECTED,
            proposal.worker_name,
            f"proposal rejected: {proposal.task_title}",
        )
        self.store.clear_resolved()
        self.broadcast()
        return True

    def reject_all(self) -> int:
        """Reject all pending proposals. Returns count rejected."""
        d = self._daemon
        pending = self.store.pending
        for p in pending:
            p.status = ProposalStatus.REJECTED
            # Allow pilot to re-propose rejected completion tasks
            if p.proposal_type == "completion" and p.task_id:
                d.pilot.clear_proposed_completion(p.task_id)
        count = len(pending)
        if count:
            d.drone_log.add(DroneAction.REJECTED, "all", f"rejected {count} proposal(s)")
            self.store.clear_resolved()
            self.broadcast()
        return count
