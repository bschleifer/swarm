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

    @classmethod
    def escalation(
        cls,
        *,
        worker_name: str,
        action: str,
        assessment: str,
        message: str = "",
        reasoning: str = "",
        confidence: float = 0.6,
    ) -> AssignmentProposal:
        return cls(
            worker_name=worker_name,
            proposal_type="escalation",
            queen_action=action,
            assessment=assessment,
            message=message,
            reasoning=reasoning or assessment,
            confidence=confidence,
        )

    @classmethod
    def completion(
        cls,
        *,
        worker_name: str,
        task_id: str,
        task_title: str,
        assessment: str,
        reasoning: str = "",
        confidence: float = 0.8,
    ) -> AssignmentProposal:
        return cls(
            worker_name=worker_name,
            task_id=task_id,
            task_title=task_title,
            proposal_type="completion",
            queen_action="complete_task",
            assessment=assessment,
            reasoning=reasoning,
            confidence=confidence,
        )

    @classmethod
    def assignment(
        cls,
        *,
        worker_name: str,
        task_id: str,
        task_title: str,
        message: str,
        reasoning: str = "",
        confidence: float = 0.8,
    ) -> AssignmentProposal:
        return cls(
            worker_name=worker_name,
            task_id=task_id,
            task_title=task_title,
            message=message,
            reasoning=reasoning,
            confidence=confidence,
        )


class ProposalStore:
    """In-memory store for assignment proposals."""

    _HISTORY_CAP = 100

    def __init__(self) -> None:
        self._proposals: dict[str, AssignmentProposal] = {}
        self._history: list[AssignmentProposal] = []

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

    def has_pending_escalation(self, worker_name: str) -> bool:
        return any(p.proposal_type == "escalation" for p in self.pending_for_worker(worker_name))

    def has_pending_completion(self, worker_name: str, task_id: str) -> bool:
        return any(
            p.proposal_type == "completion" and p.task_id == task_id
            for p in self.pending_for_worker(worker_name)
        )

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
        """Move non-pending proposals to history. Returns count moved."""
        to_remove = [
            pid for pid, p in self._proposals.items() if p.status != ProposalStatus.PENDING
        ]
        for pid in to_remove:
            self._history.append(self._proposals.pop(pid))
        # Cap history size
        if len(self._history) > self._HISTORY_CAP:
            self._history = self._history[-self._HISTORY_CAP :]
        return len(to_remove)

    @property
    def all_proposals(self) -> list[AssignmentProposal]:
        return list(self._proposals.values())

    @property
    def history(self) -> list[AssignmentProposal]:
        """Return resolved proposals, newest first."""
        return list(reversed(self._history))

    def add_to_history(self, proposal: AssignmentProposal) -> None:
        """Add a resolved proposal directly to history (e.g. auto-actions)."""
        self._history.append(proposal)
        if len(self._history) > self._HISTORY_CAP:
            self._history = self._history[-self._HISTORY_CAP :]


def build_worker_task_info(task_board, worker_name: str) -> str:
    """Build a task-info string for a worker's active tasks."""
    if not task_board:
        return ""
    active = [
        t
        for t in task_board.tasks_for_worker(worker_name)
        if t.status.value in ("assigned", "in_progress")
    ]
    if not active:
        return ""
    lines: list[str] = []
    for t in active:
        lines.append(f"- [{t.id[:12]}] {t.title} (status={t.status.value})")
        if t.description:
            lines.append(f"  Description: {t.description[:200]}")
    return "\n".join(lines)
