"""Tests for server/proposals.py — ProposalManager approval/rejection/logging."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from swarm.drones.log import DroneLog, LogCategory, SystemAction
from swarm.server.proposals import ProposalManager
from swarm.tasks.proposal import AssignmentProposal, ProposalStore
from swarm.worker.worker import WorkerState
from tests.conftest import make_worker as _conftest_make_worker


def _make_daemon():
    """Build a minimal mock daemon for ProposalManager tests."""
    daemon = MagicMock()
    daemon.drone_log = DroneLog()
    daemon.task_board = MagicMock()
    daemon.pilot = MagicMock()
    daemon.notification_bus = MagicMock()
    daemon.ws_clients = set()
    daemon.analyzer = MagicMock()
    daemon.analyzer.execute_escalation = AsyncMock()
    return daemon


def _make_worker(name: str = "api"):
    return _conftest_make_worker(name=name, state=WorkerState.RESTING)


class TestApproveEscalationWait:
    """Issue 1: approving a 'wait' escalation sends Enter to the worker process."""

    @pytest.mark.asyncio
    async def test_wait_approval_sends_enter(self):
        """When action='wait' is approved, send_enter should be called."""
        daemon = _make_daemon()
        store = ProposalStore()
        mgr = ProposalManager(store, daemon)

        proposal = AssignmentProposal.escalation(
            worker_name="api",
            action="wait",
            assessment="Worker showing plan prompt",
        )
        store.add(proposal)
        worker = _make_worker("api")
        daemon.get_worker.return_value = worker

        await mgr.approve(proposal.id)
        assert "\n" in worker.process.keys_sent

    @pytest.mark.asyncio
    async def test_non_wait_approval_does_not_send_enter(self):
        """When action='continue', send_enter should NOT be called from _approve_escalation."""
        daemon = _make_daemon()
        store = ProposalStore()
        mgr = ProposalManager(store, daemon)

        proposal = AssignmentProposal.escalation(
            worker_name="api",
            action="continue",
            assessment="Worker stuck on prompt",
        )
        store.add(proposal)
        worker = _make_worker("api")
        daemon.get_worker.return_value = worker

        await mgr.approve(proposal.id)
        # send_enter is NOT called from _approve_escalation for non-wait actions.
        # The analyzer.execute_escalation mock handles the action instead.
        assert "\n" not in worker.process.keys_sent


class TestLogCategories:
    """Issue 4: escalation/completion proposals log with QUEEN category."""

    @pytest.mark.asyncio
    async def test_approve_escalation_uses_queen_category(self):
        daemon = _make_daemon()
        store = ProposalStore()
        mgr = ProposalManager(store, daemon)

        proposal = AssignmentProposal.escalation(
            worker_name="api",
            action="wait",
            assessment="Plan prompt",
        )
        store.add(proposal)
        daemon.get_worker.return_value = _make_worker("api")

        await mgr.approve(proposal.id)

        # Find the APPROVED log entry
        approved = [e for e in daemon.drone_log.entries if e.action == SystemAction.APPROVED]
        assert len(approved) == 1
        assert approved[0].category == LogCategory.QUEEN

    @pytest.mark.asyncio
    async def test_approve_completion_uses_queen_category(self):
        daemon = _make_daemon()
        store = ProposalStore()
        mgr = ProposalManager(store, daemon)

        proposal = AssignmentProposal.completion(
            worker_name="api",
            task_id="t1",
            task_title="Fix bug",
            assessment="All done",
        )
        store.add(proposal)
        daemon.get_worker.return_value = _make_worker("api")
        daemon.complete_task = MagicMock()

        await mgr.approve(proposal.id)

        approved = [e for e in daemon.drone_log.entries if e.action == SystemAction.APPROVED]
        assert len(approved) == 1
        assert approved[0].category == LogCategory.QUEEN

    @pytest.mark.asyncio
    async def test_approve_assignment_uses_drone_category(self):
        daemon = _make_daemon()
        store = ProposalStore()
        mgr = ProposalManager(store, daemon)

        proposal = AssignmentProposal.assignment(
            worker_name="api",
            task_id="t1",
            task_title="Build API",
            message="Do it",
        )
        store.add(proposal)
        worker = _make_worker("api")
        daemon.get_worker.return_value = worker
        daemon.assign_task = AsyncMock()

        await mgr.approve(proposal.id)

        approved = [e for e in daemon.drone_log.entries if e.action == SystemAction.APPROVED]
        assert len(approved) == 1
        assert approved[0].category == LogCategory.DRONE

    def test_reject_escalation_uses_queen_category(self):
        daemon = _make_daemon()
        store = ProposalStore()
        mgr = ProposalManager(store, daemon)

        proposal = AssignmentProposal.escalation(
            worker_name="api",
            action="send_message",
            assessment="Worker stuck",
        )
        store.add(proposal)

        mgr.reject(proposal.id)

        rejected = [e for e in daemon.drone_log.entries if e.action == SystemAction.REJECTED]
        assert len(rejected) == 1
        assert rejected[0].category == LogCategory.QUEEN

    def test_reject_assignment_uses_drone_category(self):
        daemon = _make_daemon()
        store = ProposalStore()
        mgr = ProposalManager(store, daemon)

        proposal = AssignmentProposal.assignment(
            worker_name="api",
            task_id="t1",
            task_title="Build API",
            message="Do it",
        )
        store.add(proposal)

        mgr.reject(proposal.id)

        rejected = [e for e in daemon.drone_log.entries if e.action == SystemAction.REJECTED]
        assert len(rejected) == 1
        assert rejected[0].category == LogCategory.DRONE

    def test_reject_all_uses_queen_category(self):
        daemon = _make_daemon()
        store = ProposalStore()
        mgr = ProposalManager(store, daemon)

        p1 = AssignmentProposal.escalation(worker_name="api", action="wait", assessment="Plan")
        p2 = AssignmentProposal.assignment(
            worker_name="web", task_id="t1", task_title="Bug", message="Fix"
        )
        store.add(p1)
        store.add(p2)

        count = mgr.reject_all()
        assert count == 2

        rejected = [e for e in daemon.drone_log.entries if e.action == SystemAction.REJECTED]
        assert len(rejected) == 1
        assert rejected[0].category == LogCategory.QUEEN
