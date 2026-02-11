"""Tests for tasks/proposal.py — ProposalStore and AssignmentProposal."""

from __future__ import annotations

from swarm.tasks.proposal import AssignmentProposal, ProposalStatus, ProposalStore


def test_add_and_get():
    store = ProposalStore()
    p = AssignmentProposal(worker_name="api", task_id="t1", task_title="Fix bug")
    store.add(p)
    assert store.get(p.id) is p


def test_pending_property():
    store = ProposalStore()
    p1 = AssignmentProposal(worker_name="api", task_id="t1", task_title="Fix bug")
    p2 = AssignmentProposal(worker_name="web", task_id="t2", task_title="Add feature")
    store.add(p1)
    store.add(p2)
    assert len(store.pending) == 2

    p1.status = ProposalStatus.APPROVED
    assert len(store.pending) == 1
    assert store.pending[0].id == p2.id


def test_pending_for_task():
    store = ProposalStore()
    p1 = AssignmentProposal(worker_name="api", task_id="t1", task_title="Fix bug")
    p2 = AssignmentProposal(worker_name="web", task_id="t2", task_title="Add feature")
    store.add(p1)
    store.add(p2)
    assert len(store.pending_for_task("t1")) == 1
    assert store.pending_for_task("t1")[0].worker_name == "api"
    assert len(store.pending_for_task("t3")) == 0


def test_pending_for_worker():
    store = ProposalStore()
    p1 = AssignmentProposal(worker_name="api", task_id="t1", task_title="Fix bug")
    p2 = AssignmentProposal(worker_name="api", task_id="t2", task_title="Add feature")
    store.add(p1)
    store.add(p2)
    assert len(store.pending_for_worker("api")) == 2
    assert len(store.pending_for_worker("web")) == 0


def test_remove():
    store = ProposalStore()
    p = AssignmentProposal(worker_name="api", task_id="t1", task_title="Fix bug")
    store.add(p)
    assert store.remove(p.id) is True
    assert store.get(p.id) is None
    assert store.remove("nonexistent") is False


def test_expire_stale():
    store = ProposalStore()
    p1 = AssignmentProposal(worker_name="api", task_id="t1", task_title="Fix bug")
    p2 = AssignmentProposal(worker_name="web", task_id="t2", task_title="Add feature")
    p3 = AssignmentProposal(worker_name="api", task_id="t2", task_title="Another")
    store.add(p1)
    store.add(p2)
    store.add(p3)

    # t1 still valid, t2 still valid, but "web" worker is gone
    expired = store.expire_stale(valid_task_ids={"t1", "t2"}, valid_worker_names={"api"})
    assert expired == 1  # p2 (web worker gone)
    assert p2.status == ProposalStatus.EXPIRED

    # t1 valid but t2 removed
    expired = store.expire_stale(valid_task_ids={"t1"}, valid_worker_names={"api"})
    assert expired == 1  # p3 (t2 gone)
    assert p3.status == ProposalStatus.EXPIRED


def test_clear_resolved():
    store = ProposalStore()
    p1 = AssignmentProposal(worker_name="api", task_id="t1", task_title="Fix bug")
    p2 = AssignmentProposal(worker_name="web", task_id="t2", task_title="Add feature")
    store.add(p1)
    store.add(p2)

    p1.status = ProposalStatus.APPROVED
    p2.status = ProposalStatus.REJECTED

    count = store.clear_resolved()
    assert count == 2
    assert len(store.all_proposals) == 0


def test_clear_resolved_keeps_pending():
    store = ProposalStore()
    p1 = AssignmentProposal(worker_name="api", task_id="t1", task_title="Fix bug")
    p2 = AssignmentProposal(worker_name="web", task_id="t2", task_title="Add feature")
    store.add(p1)
    store.add(p2)

    p1.status = ProposalStatus.APPROVED
    count = store.clear_resolved()
    assert count == 1
    assert len(store.all_proposals) == 1
    assert store.get(p2.id) is p2


def test_proposal_age():
    import time

    p = AssignmentProposal(
        worker_name="api", task_id="t1", task_title="Fix bug", created_at=time.time() - 120
    )
    assert p.age >= 119  # allow tiny drift


def test_proposal_defaults():
    """New fields have sensible defaults."""
    p = AssignmentProposal(worker_name="api")
    assert p.task_id == ""
    assert p.confidence == 1.0
    assert p.proposal_type == "assignment"
    assert p.assessment == ""
    assert p.queen_action == ""


def test_escalation_proposal():
    """Escalation proposals have no task_id."""
    p = AssignmentProposal(
        worker_name="api",
        proposal_type="escalation",
        assessment="Worker is stuck on permission prompt",
        queen_action="send_message",
        message="yes",
        confidence=0.85,
    )
    assert p.task_id == ""
    assert p.proposal_type == "escalation"
    assert p.confidence == 0.85
    assert p.queen_action == "send_message"


def test_expire_stale_escalation_no_task():
    """Escalation proposals without task_id should not expire due to missing task."""
    store = ProposalStore()
    p = AssignmentProposal(
        worker_name="api",
        proposal_type="escalation",
        queen_action="continue",
    )
    store.add(p)

    # api is valid, no task_id to check
    expired = store.expire_stale(valid_task_ids=set(), valid_worker_names={"api"})
    assert expired == 0
    assert p.status == ProposalStatus.PENDING


def test_expire_stale_escalation_worker_gone():
    """Escalation proposals expire when the worker is gone."""
    store = ProposalStore()
    p = AssignmentProposal(
        worker_name="gone-worker",
        proposal_type="escalation",
        queen_action="wait",
    )
    store.add(p)

    expired = store.expire_stale(valid_task_ids=set(), valid_worker_names={"api"})
    assert expired == 1
    assert p.status == ProposalStatus.EXPIRED
