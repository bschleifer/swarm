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
    daemon.task_board = None
    daemon.drone_log = None
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


class TestTestOperatorRaceSafe:
    """Race-safe proposal resolution — TaskOperationError handled gracefully."""

    @pytest.mark.asyncio
    async def test_handles_task_operation_error(self, tmp_path):
        """TaskOperationError (race) should be caught without logging warning."""
        from swarm.server.daemon import TaskOperationError

        daemon = _make_mock_daemon()
        proposal = _make_mock_proposal()
        daemon.proposal_store.get.return_value = proposal
        daemon.proposals.approve = AsyncMock(
            side_effect=TaskOperationError("Proposal already resolved")
        )

        log = TestRunLog("test", tmp_path)
        config = TestConfig(auto_resolve_delay=0.01)
        operator = TestOperator(daemon, log, config)

        # Should not raise, and should not log as a warning
        await operator._resolve("p123")
        # No decision logged because the proposal was a race
        assert len(log.entries) == 0

    @pytest.mark.asyncio
    async def test_other_errors_still_logged(self, tmp_path):
        """Non-TaskOperationError should still be caught and logged."""
        daemon = _make_mock_daemon()
        proposal = _make_mock_proposal()
        daemon.proposal_store.get.return_value = proposal
        daemon.proposals.approve = AsyncMock(side_effect=RuntimeError("unexpected error"))

        log = TestRunLog("test", tmp_path)
        config = TestConfig(auto_resolve_delay=0.01)
        operator = TestOperator(daemon, log, config)

        # Should not raise
        await operator._resolve("p123")
        # No decision logged because approve failed
        assert len(log.entries) == 0


class TestTestOperatorCleanup:
    """Tests for cleanup of test tasks and log entries on stop."""

    def test_stop_cleans_up_test_tasks(self, tmp_path):
        import time

        from swarm.drones.log import DroneAction, DroneLog
        from swarm.tasks.board import TaskBoard

        daemon = _make_mock_daemon()
        board = TaskBoard()
        drone_log = DroneLog()
        daemon.task_board = board
        daemon.drone_log = drone_log

        log = TestRunLog("test", tmp_path)
        config = TestConfig(auto_resolve_delay=0.01)
        operator = TestOperator(daemon, log, config)

        # Record start time
        operator._start_time = time.time() - 1  # 1 second ago

        # Add a task created after start time
        task = board.create("Test task")
        operator._test_task_ids.add(task.id)

        # Add a log entry during test
        drone_log.add(DroneAction.CONTINUED, "test-worker", "test action")

        assert len(board.all_tasks) == 1
        assert len(drone_log.entries) == 1

        # Stop should clean up
        operator._cleanup()

        assert len(board.all_tasks) == 0
        assert len(drone_log.entries) == 0

    def test_cleanup_preserves_pre_existing_tasks(self, tmp_path):
        import time

        from swarm.drones.log import DroneAction, DroneLog
        from swarm.tasks.board import TaskBoard

        daemon = _make_mock_daemon()
        board = TaskBoard()
        drone_log = DroneLog()
        daemon.task_board = board
        daemon.drone_log = drone_log

        # Pre-existing task and log entry
        board.create("Pre-existing task")
        drone_log.add(DroneAction.CONTINUED, "pre-worker", "pre action")

        log = TestRunLog("test", tmp_path)
        config = TestConfig(auto_resolve_delay=0.01)
        operator = TestOperator(daemon, log, config)
        operator._start_time = time.time()

        # Only the test task ID is tracked
        test_task = board.create("Test task")
        operator._test_task_ids.add(test_task.id)

        assert len(board.all_tasks) == 2

        operator._cleanup()

        # Pre-existing task and log entry preserved
        assert len(board.all_tasks) == 1
        assert board.all_tasks[0].title == "Pre-existing task"
        # Log entry from before start_time is preserved
        assert len(drone_log.entries) == 1
