"""AssignmentProposal — Queen-proposed task assignments awaiting user approval."""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum


class ProposalStatus(Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"


@dataclass
class AssignmentProposal:
    """A Queen-proposed assignment of a task to a worker."""

    worker_name: str
    task_id: str = ""
    task_title: str = ""
    message: str = ""
    reasoning: str = ""
    confidence: float = 1.0
    proposal_type: str = "assignment"  # "assignment" or "escalation"
    assessment: str = ""  # Queen's analysis (escalation only)
    queen_action: str = ""  # "continue"|"send_message"|"restart"|"wait"
    status: ProposalStatus = ProposalStatus.PENDING
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    created_at: float = field(default_factory=time.time)

    @property
    def age(self) -> float:
        return time.time() - self.created_at


class ProposalStore:
    """In-memory store for assignment proposals."""

    def __init__(self) -> None:
        self._proposals: dict[str, AssignmentProposal] = {}

    def add(self, proposal: AssignmentProposal) -> None:
        self._proposals[proposal.id] = proposal

    def get(self, proposal_id: str) -> AssignmentProposal | None:
        return self._proposals.get(proposal_id)

    def remove(self, proposal_id: str) -> bool:
        return self._proposals.pop(proposal_id, None) is not None

    @property
    def pending(self) -> list[AssignmentProposal]:
        return [p for p in self._proposals.values() if p.status == ProposalStatus.PENDING]

    def pending_for_task(self, task_id: str) -> list[AssignmentProposal]:
        return [p for p in self.pending if p.task_id == task_id]

    def pending_for_worker(self, worker_name: str) -> list[AssignmentProposal]:
        return [p for p in self.pending if p.worker_name == worker_name]

    def expire_stale(
        self,
        valid_task_ids: set[str],
        valid_worker_names: set[str],
    ) -> int:
        """Expire pending proposals where the task or worker is no longer valid.

        Returns the number of proposals expired.
        """
        count = 0
        for p in self.pending:
            if p.worker_name not in valid_worker_names:
                p.status = ProposalStatus.EXPIRED
                count += 1
            elif p.task_id and p.task_id not in valid_task_ids:
                p.status = ProposalStatus.EXPIRED
                count += 1
        return count

    def clear_resolved(self) -> int:
        """Remove non-pending proposals from memory. Returns count removed."""
        to_remove = [
            pid for pid, p in self._proposals.items() if p.status != ProposalStatus.PENDING
        ]
        for pid in to_remove:
            del self._proposals[pid]
        return len(to_remove)

    @property
    def all_proposals(self) -> list[AssignmentProposal]:
        return list(self._proposals.values())
