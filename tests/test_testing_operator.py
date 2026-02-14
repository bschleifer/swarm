"""Tests for testing/operator.py — TestOperator."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from swarm.testing.config import TestConfig
from swarm.testing.log import TestRunLog
from swarm.testing.operator import TestOperator


def _make_mock_daemon():
    """Create a mock daemon with the required attributes."""
    daemon = MagicMock()
    daemon.proposals = MagicMock()
    daemon.proposals._on_new_proposal = None
    daemon.proposals.approve = AsyncMock(return_value=True)
    daemon.proposals.reject = MagicMock(return_value=True)
    daemon.proposal_store = MagicMock()
    daemon.queen = MagicMock()
    daemon.queen.enabled = False  # disable Queen for unit tests
    daemon.queen.can_call = False
    return daemon


def _make_mock_proposal(proposal_id="p123", status="pending"):
    from swarm.tasks.proposal import ProposalStatus

    proposal = MagicMock()
    proposal.id = proposal_id
    proposal.worker_name = "test-worker"
    proposal.proposal_type = "assignment"
    proposal.task_title = "Fix bug"
    proposal.message = "test"
    proposal.reasoning = "test reasoning"
    proposal.assessment = ""
    proposal.queen_action = ""
    proposal.confidence = 0.8
    proposal.status = ProposalStatus.PENDING if status == "pending" else ProposalStatus.APPROVED
    return proposal


class TestTestOperatorInit:
    async def test_start_wires_hook(self, tmp_path):
        daemon = _make_mock_daemon()
        log = TestRunLog("test", tmp_path)
        config = TestConfig(auto_resolve_delay=0.01)
        operator = TestOperator(daemon, log, config)
        operator.start()
        assert daemon.proposals._on_new_proposal is not None
        operator.stop()

    async def test_stop_cancels_task(self, tmp_path):
        daemon = _make_mock_daemon()
        log = TestRunLog("test", tmp_path)
        config = TestConfig(auto_resolve_delay=0.01)
        operator = TestOperator(daemon, log, config)
        operator.start()
        assert operator._task is not None
        operator.stop()


class TestTestOperatorResolve:
    @pytest.mark.asyncio
    async def test_auto_approves_when_queen_disabled(self, tmp_path):
        daemon = _make_mock_daemon()
        proposal = _make_mock_proposal()
        daemon.proposal_store.get.return_value = proposal

        log = TestRunLog("test", tmp_path)
        config = TestConfig(auto_resolve_delay=0.01)
        operator = TestOperator(daemon, log, config)

        await operator._resolve("p123")

        daemon.proposals.approve.assert_awaited_once_with("p123")
        assert len(log.entries) == 1
        assert log.entries[0].event_type == "operator_decision"
        assert "approved" in log.entries[0].detail

    @pytest.mark.asyncio
    async def test_skips_already_resolved(self, tmp_path):
        daemon = _make_mock_daemon()
        proposal = _make_mock_proposal(status="approved")
        daemon.proposal_store.get.return_value = proposal

        log = TestRunLog("test", tmp_path)
        config = TestConfig(auto_resolve_delay=0.01)
        operator = TestOperator(daemon, log, config)

        await operator._resolve("p123")

        daemon.proposals.approve.assert_not_awaited()
        assert len(log.entries) == 0

    @pytest.mark.asyncio
    async def test_skips_missing_proposal(self, tmp_path):
        daemon = _make_mock_daemon()
        daemon.proposal_store.get.return_value = None

        log = TestRunLog("test", tmp_path)
        config = TestConfig(auto_resolve_delay=0.01)
        operator = TestOperator(daemon, log, config)

        await operator._resolve("nonexistent")

        daemon.proposals.approve.assert_not_awaited()
        assert len(log.entries) == 0

    @pytest.mark.asyncio
    async def test_handles_approve_failure(self, tmp_path):
        daemon = _make_mock_daemon()
        proposal = _make_mock_proposal()
        daemon.proposal_store.get.return_value = proposal
        daemon.proposals.approve = AsyncMock(side_effect=RuntimeError("test error"))

        log = TestRunLog("test", tmp_path)
        config = TestConfig(auto_resolve_delay=0.01)
        operator = TestOperator(daemon, log, config)

        # Should not raise
        await operator._resolve("p123")
        assert len(log.entries) == 0  # logged nothing because approve failed
