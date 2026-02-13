"""Tests for server/analyzer.py — QueenAnalyzer escalation/completion analysis."""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from swarm.drones.log import DroneLog
from swarm.queen.queen import Queen
from swarm.config import QueenConfig
from swarm.server.analyzer import QueenAnalyzer
from swarm.tasks.proposal import AssignmentProposal, ProposalStore
from swarm.tasks.task import SwarmTask
from swarm.worker.worker import Worker, WorkerState


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Patch targets for locally-imported functions inside analyzer methods
_CAPTURE = "swarm.tmux.cell.capture_pane"
_SEND_KEYS = "swarm.tmux.cell.send_keys"
_SEND_ENTER = "swarm.tmux.cell.send_enter"
_REVIVE = "swarm.worker.manager.revive_worker"
_BUILD_HIVE = "swarm.queen.context.build_hive_context"
_BUILD_TASK_INFO = "swarm.tasks.proposal.build_worker_task_info"


def _make_worker(
    name: str = "api",
    state: WorkerState = WorkerState.RESTING,
    pane_id: str = "%0",
    state_since: float | None = None,
) -> Worker:
    w = Worker(name=name, path="/tmp", pane_id=pane_id, state=state)
    if state_since is not None:
        w.state_since = state_since
    return w


def _make_task(title: str = "Fix bug", task_id: str = "t1") -> SwarmTask:
    return SwarmTask(title=title, id=task_id, description="Run pytest and fix failures")


def _make_daemon(workers: list[Worker] | None = None) -> MagicMock:
    """Build a minimal mock daemon with the attributes QueenAnalyzer uses."""
    d = MagicMock()
    d.workers = workers or []
    d.proposal_store = ProposalStore()
    d.drone_log = DroneLog()
    d.task_board = MagicMock()
    d.config = MagicMock()
    d.config.session_name = "test"
    d.config.drones.approval_rules = None
    d.broadcast_ws = MagicMock()
    d.queue_proposal = MagicMock()
    d.get_worker = MagicMock(
        side_effect=lambda name: next((w for w in d.workers if w.name == name), None)
    )
    d._worker_descriptions = MagicMock(return_value={})
    return d


def _make_queen(monkeypatch, min_confidence: float = 0.7) -> Queen:
    monkeypatch.setattr("swarm.queen.queen.load_session", lambda _: None)
    monkeypatch.setattr("swarm.queen.queen.save_session", lambda *a: None)
    return Queen(
        config=QueenConfig(cooldown=0.0, min_confidence=min_confidence),
        session_name="test",
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def queen(monkeypatch):
    return _make_queen(monkeypatch)


@pytest.fixture
def daemon():
    return _make_daemon()


@pytest.fixture
def analyzer(queen, daemon):
    return QueenAnalyzer(queen, daemon)


# ---------------------------------------------------------------------------
# Public accessor / tracking tests
# ---------------------------------------------------------------------------


class TestInflightTracking:
    """Test has_inflight_*, track_*, clear_* methods."""

    def test_escalation_tracking(self, analyzer):
        assert analyzer.has_inflight_escalation("api") is False
        analyzer.track_escalation("api")
        assert analyzer.has_inflight_escalation("api") is True
        analyzer.clear_escalation("api")
        assert analyzer.has_inflight_escalation("api") is False

    def test_completion_tracking(self, analyzer):
        key = "api:t1"
        assert analyzer.has_inflight_completion(key) is False
        analyzer.track_completion(key)
        assert analyzer.has_inflight_completion(key) is True
        analyzer.clear_completion(key)
        assert analyzer.has_inflight_completion(key) is False

    def test_clear_escalation_idempotent(self, analyzer):
        """Clearing a non-existent escalation should not raise."""
        analyzer.clear_escalation("nonexistent")

    def test_clear_completion_idempotent(self, analyzer):
        """Clearing a non-existent completion key should not raise."""
        analyzer.clear_completion("nonexistent:key")

    def test_clear_worker_inflight(self, analyzer):
        """clear_worker_inflight removes escalation AND matching completion keys."""
        analyzer.track_escalation("api")
        analyzer.track_completion("api:t1")
        analyzer.track_completion("api:t2")
        analyzer.track_completion("web:t3")

        analyzer.clear_worker_inflight("api")

        assert analyzer.has_inflight_escalation("api") is False
        assert analyzer.has_inflight_completion("api:t1") is False
        assert analyzer.has_inflight_completion("api:t2") is False
        # "web" completion should be untouched
        assert analyzer.has_inflight_completion("web:t3") is True

    def test_clear_worker_inflight_no_op_when_empty(self, analyzer):
        """Clearing inflight for unknown worker should not raise."""
        analyzer.clear_worker_inflight("nonexistent")


# ---------------------------------------------------------------------------
# analyze_escalation tests
# ---------------------------------------------------------------------------


class TestAnalyzeEscalation:
    """Tests for analyze_escalation — Queen evaluates an escalated worker."""

    @pytest.mark.asyncio
    async def test_auto_act_continue(self, analyzer, daemon):
        """High-confidence 'continue' action should auto-execute, not queue proposal."""
        worker = _make_worker()
        daemon.workers = [worker]
        analyzer.queen.min_confidence = 0.7

        queen_result = {
            "action": "continue",
            "confidence": 0.9,
            "assessment": "Worker waiting for input",
            "reasoning": "Idle at prompt",
            "message": "",
        }
        analyzer.queen.analyze_worker = AsyncMock(return_value=queen_result)

        with (
            patch(_CAPTURE, new_callable=AsyncMock, return_value="pane output"),
            patch(_SEND_ENTER, new_callable=AsyncMock),
            patch.object(analyzer, "gather_context", new_callable=AsyncMock, return_value="ctx"),
        ):
            await analyzer.analyze_escalation(worker, "unrecognized state")

        # Should auto-execute, not queue
        daemon.queue_proposal.assert_not_called()
        daemon.broadcast_ws.assert_called_once()
        ws_data = daemon.broadcast_ws.call_args[0][0]
        assert ws_data["type"] == "queen_auto_acted"
        assert ws_data["action"] == "continue"

    @pytest.mark.asyncio
    async def test_auto_act_restart(self, analyzer, daemon):
        """High-confidence 'restart' action should auto-execute."""
        worker = _make_worker()
        daemon.workers = [worker]
        analyzer.queen.min_confidence = 0.7

        queen_result = {
            "action": "restart",
            "confidence": 0.95,
            "assessment": "Worker crashed",
            "reasoning": "Segfault detected",
            "message": "",
        }
        analyzer.queen.analyze_worker = AsyncMock(return_value=queen_result)

        with (
            patch(_CAPTURE, new_callable=AsyncMock, return_value="error"),
            patch(_REVIVE, new_callable=AsyncMock),
            patch.object(analyzer, "gather_context", new_callable=AsyncMock, return_value="ctx"),
        ):
            await analyzer.analyze_escalation(worker, "worker crashed")

        daemon.queue_proposal.assert_not_called()

    @pytest.mark.asyncio
    async def test_low_confidence_queues_proposal(self, analyzer, daemon):
        """Low-confidence result should queue a proposal rather than auto-act."""
        worker = _make_worker()
        analyzer.queen.min_confidence = 0.7

        queen_result = {
            "action": "continue",
            "confidence": 0.5,
            "assessment": "Possibly stuck",
            "reasoning": "Not certain",
            "message": "",
        }
        analyzer.queen.analyze_worker = AsyncMock(return_value=queen_result)

        with (
            patch(_CAPTURE, new_callable=AsyncMock, return_value="output"),
            patch.object(analyzer, "gather_context", new_callable=AsyncMock, return_value="ctx"),
        ):
            await analyzer.analyze_escalation(worker, "unrecognized state")

        daemon.queue_proposal.assert_called_once()
        proposal = daemon.queue_proposal.call_args[0][0]
        assert proposal.proposal_type == "escalation"
        assert proposal.confidence == 0.5

    @pytest.mark.asyncio
    async def test_user_question_always_queues(self, analyzer, daemon):
        """Escalations containing 'user question' always require user approval."""
        worker = _make_worker()
        analyzer.queen.min_confidence = 0.7

        queen_result = {
            "action": "continue",
            "confidence": 0.99,
            "assessment": "User asked a question",
            "reasoning": "Question in pane",
            "message": "",
        }
        analyzer.queen.analyze_worker = AsyncMock(return_value=queen_result)

        with (
            patch(_CAPTURE, new_callable=AsyncMock, return_value="output"),
            patch.object(analyzer, "gather_context", new_callable=AsyncMock, return_value="ctx"),
        ):
            await analyzer.analyze_escalation(worker, "user question detected")

        daemon.queue_proposal.assert_called_once()

    @pytest.mark.asyncio
    async def test_plan_reason_always_queues(self, analyzer, daemon):
        """Exact 'plan requires user approval' reason always requires user approval."""
        worker = _make_worker()
        analyzer.queen.min_confidence = 0.7

        queen_result = {
            "action": "continue",
            "confidence": 0.99,
            "assessment": "Plan submitted",
            "reasoning": "Agent presenting a plan",
            "message": "",
        }
        analyzer.queen.analyze_worker = AsyncMock(return_value=queen_result)

        with (
            patch(_CAPTURE, new_callable=AsyncMock, return_value="output"),
            patch.object(analyzer, "gather_context", new_callable=AsyncMock, return_value="ctx"),
        ):
            await analyzer.analyze_escalation(worker, "plan requires user approval")

        daemon.queue_proposal.assert_called_once()

    @pytest.mark.asyncio
    async def test_choice_requires_approval_can_auto_act(self, analyzer, daemon):
        """Regression: 'choice requires approval' should allow auto-act at high confidence.

        Previously, if the drone's escalation reason was generated from a false-positive
        plan detection, the reason contained 'plan' and blocked auto-approval. The drone
        reason 'choice requires approval: ...' should NOT block auto-approval.
        """
        worker = _make_worker()
        daemon.workers = [worker]
        analyzer.queen.min_confidence = 0.7

        queen_result = {
            "action": "continue",
            "confidence": 0.9,
            "assessment": "Safe read-only grep command",
            "reasoning": "Worker needs permission for grep",
            "message": "",
        }
        analyzer.queen.analyze_worker = AsyncMock(return_value=queen_result)

        with (
            patch(_CAPTURE, new_callable=AsyncMock, return_value="pane output"),
            patch(_SEND_ENTER, new_callable=AsyncMock),
            patch.object(analyzer, "gather_context", new_callable=AsyncMock, return_value="ctx"),
        ):
            await analyzer.analyze_escalation(worker, "choice requires approval: choice menu")

        # Should auto-execute, not queue
        daemon.queue_proposal.assert_not_called()
        daemon.broadcast_ws.assert_called_once()
        ws_data = daemon.broadcast_ws.call_args[0][0]
        assert ws_data["type"] == "queen_auto_acted"

    @pytest.mark.asyncio
    async def test_send_message_never_auto_acts(self, analyzer, daemon):
        """send_message should never auto-execute even with high confidence."""
        worker = _make_worker()
        analyzer.queen.min_confidence = 0.7

        queen_result = {
            "action": "send_message",
            "confidence": 0.99,
            "assessment": "Need to send input",
            "reasoning": "Worker waiting",
            "message": "yes",
        }
        analyzer.queen.analyze_worker = AsyncMock(return_value=queen_result)

        with (
            patch(_CAPTURE, new_callable=AsyncMock, return_value="output"),
            patch.object(analyzer, "gather_context", new_callable=AsyncMock, return_value="ctx"),
        ):
            await analyzer.analyze_escalation(worker, "unrecognized state")

        daemon.queue_proposal.assert_called_once()

    @pytest.mark.asyncio
    async def test_non_dict_result_returns_early(self, analyzer, daemon):
        """Non-dict Queen result should be silently ignored."""
        worker = _make_worker()
        analyzer.queen.analyze_worker = AsyncMock(return_value="not a dict")

        with (
            patch(_CAPTURE, new_callable=AsyncMock, return_value="output"),
            patch.object(analyzer, "gather_context", new_callable=AsyncMock, return_value="ctx"),
        ):
            await analyzer.analyze_escalation(worker, "test")

        daemon.queue_proposal.assert_not_called()

    @pytest.mark.asyncio
    async def test_empty_analysis_dropped(self, analyzer, daemon):
        """Queen result with no assessment/reasoning/message is dropped."""
        worker = _make_worker()
        queen_result = {
            "action": "continue",
            "confidence": 0.9,
            "assessment": "",
            "reasoning": "",
            "message": "",
        }
        analyzer.queen.analyze_worker = AsyncMock(return_value=queen_result)

        with (
            patch(_CAPTURE, new_callable=AsyncMock, return_value="output"),
            patch.object(analyzer, "gather_context", new_callable=AsyncMock, return_value="ctx"),
        ):
            await analyzer.analyze_escalation(worker, "test")

        daemon.queue_proposal.assert_not_called()

    @pytest.mark.asyncio
    async def test_duplicate_proposal_dropped(self, analyzer, daemon):
        """If a pending escalation proposal already exists, don't create another."""
        worker = _make_worker()
        # Pre-add an existing pending escalation
        existing = AssignmentProposal.escalation(
            worker_name="api",
            action="continue",
            assessment="existing",
        )
        daemon.proposal_store.add(existing)

        queen_result = {
            "action": "continue",
            "confidence": 0.5,
            "assessment": "New analysis",
            "reasoning": "New reasoning",
            "message": "",
        }
        analyzer.queen.analyze_worker = AsyncMock(return_value=queen_result)

        with (
            patch(_CAPTURE, new_callable=AsyncMock, return_value="output"),
            patch.object(analyzer, "gather_context", new_callable=AsyncMock, return_value="ctx"),
        ):
            await analyzer.analyze_escalation(worker, "test")

        daemon.queue_proposal.assert_not_called()

    @pytest.mark.asyncio
    async def test_oserror_clears_inflight(self, analyzer, daemon):
        """OSError during capture_pane should clear inflight and return gracefully."""
        worker = _make_worker()
        analyzer.track_escalation("api")

        with patch(_CAPTURE, new_callable=AsyncMock, side_effect=OSError("boom")):
            await analyzer.analyze_escalation(worker, "test")

        assert analyzer.has_inflight_escalation("api") is False
        daemon.queue_proposal.assert_not_called()

    @pytest.mark.asyncio
    async def test_timeout_clears_inflight(self, analyzer, daemon):
        """asyncio.TimeoutError should clear inflight and return gracefully."""
        worker = _make_worker()
        analyzer.track_escalation("api")

        with patch(_CAPTURE, new_callable=AsyncMock, side_effect=asyncio.TimeoutError()):
            await analyzer.analyze_escalation(worker, "test")

        assert analyzer.has_inflight_escalation("api") is False

    @pytest.mark.asyncio
    async def test_pane_gone_error_clears_inflight(self, analyzer, daemon):
        """PaneGoneError should clear inflight and return gracefully."""
        from swarm.tmux.cell import PaneGoneError

        worker = _make_worker()
        analyzer.track_escalation("api")

        with patch(_CAPTURE, new_callable=AsyncMock, side_effect=PaneGoneError("gone")):
            await analyzer.analyze_escalation(worker, "test")

        assert analyzer.has_inflight_escalation("api") is False

    @pytest.mark.asyncio
    async def test_tmux_error_clears_inflight(self, analyzer, daemon):
        """TmuxError should clear inflight and return gracefully."""
        from swarm.tmux.cell import TmuxError

        worker = _make_worker()
        analyzer.track_escalation("api")

        with patch(_CAPTURE, new_callable=AsyncMock, side_effect=TmuxError("fail")):
            await analyzer.analyze_escalation(worker, "test")

        assert analyzer.has_inflight_escalation("api") is False

    @pytest.mark.asyncio
    async def test_default_confidence(self, analyzer, daemon):
        """When Queen omits confidence, default to 0.8."""
        worker = _make_worker()
        analyzer.queen.min_confidence = 0.9  # above default 0.8

        queen_result = {
            "action": "continue",
            # no "confidence" key
            "assessment": "Worker idle",
            "reasoning": "Needs attention",
            "message": "",
        }
        analyzer.queen.analyze_worker = AsyncMock(return_value=queen_result)

        with (
            patch(_CAPTURE, new_callable=AsyncMock, return_value="output"),
            patch.object(analyzer, "gather_context", new_callable=AsyncMock, return_value="ctx"),
        ):
            await analyzer.analyze_escalation(worker, "test")

        # 0.8 < 0.9 min_confidence, so should queue instead of auto-act
        daemon.queue_proposal.assert_called_once()

    @pytest.mark.asyncio
    async def test_wait_action_queues_proposal(self, analyzer, daemon):
        """'wait' action is not in safe_auto_actions, so always queued."""
        worker = _make_worker()
        analyzer.queen.min_confidence = 0.7

        queen_result = {
            "action": "wait",
            "confidence": 0.99,
            "assessment": "No action needed",
            "reasoning": "Worker is fine",
            "message": "",
        }
        analyzer.queen.analyze_worker = AsyncMock(return_value=queen_result)

        with (
            patch(_CAPTURE, new_callable=AsyncMock, return_value="output"),
            patch.object(analyzer, "gather_context", new_callable=AsyncMock, return_value="ctx"),
        ):
            await analyzer.analyze_escalation(worker, "unrecognized state")

        daemon.queue_proposal.assert_called_once()


# ---------------------------------------------------------------------------
# execute_escalation tests
# ---------------------------------------------------------------------------


class TestExecuteEscalation:
    """Tests for execute_escalation — executes an approved proposal."""

    @pytest.mark.asyncio
    async def test_send_message(self, analyzer, daemon):
        worker = _make_worker()
        daemon.get_worker = MagicMock(return_value=worker)
        proposal = AssignmentProposal.escalation(
            worker_name="api",
            action="send_message",
            assessment="stuck",
            message="yes",
        )

        with patch(_SEND_KEYS, new_callable=AsyncMock) as mock_send:
            result = await analyzer.execute_escalation(proposal)

        assert result is True
        mock_send.assert_called_once_with("%0", "yes")

    @pytest.mark.asyncio
    async def test_continue(self, analyzer, daemon):
        worker = _make_worker()
        daemon.get_worker = MagicMock(return_value=worker)
        proposal = AssignmentProposal.escalation(
            worker_name="api",
            action="continue",
            assessment="idle",
        )

        with patch(_SEND_ENTER, new_callable=AsyncMock) as mock_enter:
            result = await analyzer.execute_escalation(proposal)

        assert result is True
        mock_enter.assert_called_once_with("%0")

    @pytest.mark.asyncio
    async def test_restart(self, analyzer, daemon):
        worker = _make_worker()
        daemon.get_worker = MagicMock(return_value=worker)
        proposal = AssignmentProposal.escalation(
            worker_name="api",
            action="restart",
            assessment="crashed",
        )

        with patch(_REVIVE, new_callable=AsyncMock) as mock_revive:
            result = await analyzer.execute_escalation(proposal)

        assert result is True
        mock_revive.assert_called_once_with(worker, session_name="test")
        assert worker.revive_count == 1

    @pytest.mark.asyncio
    async def test_wait_noop(self, analyzer, daemon):
        worker = _make_worker()
        daemon.get_worker = MagicMock(return_value=worker)
        proposal = AssignmentProposal.escalation(
            worker_name="api",
            action="wait",
            assessment="nothing to do",
        )

        result = await analyzer.execute_escalation(proposal)
        assert result is True

    @pytest.mark.asyncio
    async def test_worker_not_found(self, analyzer, daemon):
        daemon.get_worker = MagicMock(return_value=None)
        proposal = AssignmentProposal.escalation(
            worker_name="gone",
            action="continue",
            assessment="test",
        )

        result = await analyzer.execute_escalation(proposal)
        assert result is False


# ---------------------------------------------------------------------------
# analyze_completion tests
# ---------------------------------------------------------------------------


class TestAnalyzeCompletion:
    """Tests for analyze_completion — Queen assesses task completion."""

    @pytest.mark.asyncio
    async def test_done_high_confidence_queues_proposal(self, analyzer, daemon):
        """Task marked done with high confidence should create a completion proposal."""
        worker = _make_worker(state_since=time.time() - 120)
        task = _make_task()

        analyzer.queen.ask = AsyncMock(
            return_value={
                "done": True,
                "resolution": "All tests pass, committed fix abc123",
                "confidence": 0.9,
            }
        )

        with patch(_CAPTURE, new_callable=AsyncMock, return_value="$ tests pass"):
            await analyzer.analyze_completion(worker, task)

        daemon.queue_proposal.assert_called_once()
        proposal = daemon.queue_proposal.call_args[0][0]
        assert proposal.proposal_type == "completion"
        assert proposal.task_id == "t1"
        assert proposal.confidence == 0.9
        assert "All tests pass" in proposal.assessment

    @pytest.mark.asyncio
    async def test_not_done_returns_early(self, analyzer, daemon):
        """When Queen says task is NOT done, no proposal should be created."""
        worker = _make_worker(state_since=time.time() - 60)
        task = _make_task()

        analyzer.queen.ask = AsyncMock(
            return_value={
                "done": False,
                "resolution": "Worker still running tests",
                "confidence": 0.8,
            }
        )

        with patch(_CAPTURE, new_callable=AsyncMock, return_value="running..."):
            await analyzer.analyze_completion(worker, task)

        daemon.queue_proposal.assert_not_called()

    @pytest.mark.asyncio
    async def test_low_confidence_skips_proposal(self, analyzer, daemon):
        """Done=true but confidence below 0.6 should skip proposal."""
        worker = _make_worker(state_since=time.time() - 60)
        task = _make_task()

        analyzer.queen.ask = AsyncMock(
            return_value={
                "done": True,
                "resolution": "Maybe done, hard to tell",
                "confidence": 0.4,
            }
        )

        with patch(_CAPTURE, new_callable=AsyncMock, return_value="output"):
            await analyzer.analyze_completion(worker, task)

        daemon.queue_proposal.assert_not_called()

    @pytest.mark.asyncio
    async def test_buzzing_worker_aborts_early(self, analyzer, daemon):
        """If worker resumed BUZZING before analysis, abort immediately."""
        worker = _make_worker(state=WorkerState.BUZZING)
        task = _make_task()
        analyzer.track_completion("api:t1")

        await analyzer.analyze_completion(worker, task)

        daemon.queue_proposal.assert_not_called()
        # Inflight should be cleared
        assert analyzer.has_inflight_completion("api:t1") is False

    @pytest.mark.asyncio
    async def test_buzzing_after_queen_call_aborts(self, analyzer, daemon):
        """If worker resumes BUZZING while Queen is thinking, drop proposal."""
        worker = _make_worker(state_since=time.time() - 60)
        task = _make_task()

        async def queen_ask_side_effect(*a, **kw):
            # Simulate worker resuming during Queen call
            worker.state = WorkerState.BUZZING
            return {
                "done": True,
                "resolution": "Committed fix",
                "confidence": 0.95,
            }

        analyzer.queen.ask = AsyncMock(side_effect=queen_ask_side_effect)

        with patch(_CAPTURE, new_callable=AsyncMock, return_value="output"):
            await analyzer.analyze_completion(worker, task)

        daemon.queue_proposal.assert_not_called()

    @pytest.mark.asyncio
    async def test_idle_fallback_resolution_rejected(self, analyzer, daemon):
        """Resolution matching 'worker X idle for Ns' pattern is a fallback -- skip it."""
        worker = _make_worker(state_since=time.time() - 90)
        task = _make_task()

        analyzer.queen.ask = AsyncMock(
            return_value={
                "done": True,
                "resolution": "Worker api idle for 90s",
                "confidence": 0.8,
            }
        )

        with patch(_CAPTURE, new_callable=AsyncMock, return_value="output"):
            await analyzer.analyze_completion(worker, task)

        daemon.queue_proposal.assert_not_called()

    @pytest.mark.asyncio
    async def test_idle_fallback_has_been_idle_rejected(self, analyzer, daemon):
        """Resolution matching 'worker X has been idle for Ns' is a fallback -- skip."""
        worker = _make_worker(state_since=time.time() - 60)
        task = _make_task()

        analyzer.queen.ask = AsyncMock(
            return_value={
                "done": True,
                "resolution": "Worker api has been idle for 120s",
                "confidence": 0.85,
            }
        )

        with patch(_CAPTURE, new_callable=AsyncMock, return_value="output"):
            await analyzer.analyze_completion(worker, task)

        daemon.queue_proposal.assert_not_called()

    @pytest.mark.asyncio
    async def test_contradiction_done_but_not_complete_text(self, analyzer, daemon):
        """Queen says done=true but resolution says 'not complete' => override to not done."""
        worker = _make_worker(state_since=time.time() - 60)
        task = _make_task()

        analyzer.queen.ask = AsyncMock(
            return_value={
                "done": True,
                "resolution": "Task is not complete, tests failing",
                "confidence": 0.8,
            }
        )

        with patch(_CAPTURE, new_callable=AsyncMock, return_value="output"):
            await analyzer.analyze_completion(worker, task)

        daemon.queue_proposal.assert_not_called()

    @pytest.mark.asyncio
    async def test_contradiction_could_not_be_verified(self, analyzer, daemon):
        """Resolution containing 'could not be verified' contradicts done=true."""
        worker = _make_worker(state_since=time.time() - 60)
        task = _make_task()

        analyzer.queen.ask = AsyncMock(
            return_value={
                "done": True,
                "resolution": "Completion could not be verified from output",
                "confidence": 0.8,
            }
        )

        with patch(_CAPTURE, new_callable=AsyncMock, return_value="output"):
            await analyzer.analyze_completion(worker, task)

        daemon.queue_proposal.assert_not_called()

    @pytest.mark.asyncio
    async def test_contradiction_unable_to_confirm(self, analyzer, daemon):
        """Resolution with 'unable to confirm' contradicts done=true."""
        worker = _make_worker(state_since=time.time() - 60)
        task = _make_task()

        analyzer.queen.ask = AsyncMock(
            return_value={
                "done": True,
                "resolution": "Unable to confirm task completion",
                "confidence": 0.8,
            }
        )

        with patch(_CAPTURE, new_callable=AsyncMock, return_value="output"):
            await analyzer.analyze_completion(worker, task)

        daemon.queue_proposal.assert_not_called()

    @pytest.mark.asyncio
    async def test_contradiction_recommend_rerunning(self, analyzer, daemon):
        """Resolution with 'recommend re-running' contradicts done=true."""
        worker = _make_worker(state_since=time.time() - 60)
        task = _make_task()

        analyzer.queen.ask = AsyncMock(
            return_value={
                "done": True,
                "resolution": "Tests may have passed but recommend re-running",
                "confidence": 0.8,
            }
        )

        with patch(_CAPTURE, new_callable=AsyncMock, return_value="output"):
            await analyzer.analyze_completion(worker, task)

        daemon.queue_proposal.assert_not_called()

    @pytest.mark.asyncio
    async def test_contradiction_needs_more_work(self, analyzer, daemon):
        """Resolution with 'needs more work' contradicts done=true."""
        worker = _make_worker(state_since=time.time() - 60)
        task = _make_task()

        analyzer.queen.ask = AsyncMock(
            return_value={
                "done": True,
                "resolution": "Task needs more work on edge cases",
                "confidence": 0.8,
            }
        )

        with patch(_CAPTURE, new_callable=AsyncMock, return_value="output"):
            await analyzer.analyze_completion(worker, task)

        daemon.queue_proposal.assert_not_called()

    @pytest.mark.asyncio
    async def test_contradiction_did_not_complete(self, analyzer, daemon):
        """Resolution with 'did not complete' contradicts done=true."""
        worker = _make_worker(state_since=time.time() - 60)
        task = _make_task()

        analyzer.queen.ask = AsyncMock(
            return_value={
                "done": True,
                "resolution": "Worker did not complete the assigned task",
                "confidence": 0.8,
            }
        )

        with patch(_CAPTURE, new_callable=AsyncMock, return_value="output"):
            await analyzer.analyze_completion(worker, task)

        daemon.queue_proposal.assert_not_called()

    @pytest.mark.asyncio
    async def test_contradiction_went_idle_without(self, analyzer, daemon):
        """Resolution with 'went idle without' contradicts done=true."""
        worker = _make_worker(state_since=time.time() - 60)
        task = _make_task()

        analyzer.queen.ask = AsyncMock(
            return_value={
                "done": True,
                "resolution": "Worker went idle without finishing",
                "confidence": 0.8,
            }
        )

        with patch(_CAPTURE, new_callable=AsyncMock, return_value="output"):
            await analyzer.analyze_completion(worker, task)

        daemon.queue_proposal.assert_not_called()

    @pytest.mark.asyncio
    async def test_non_dict_result_treated_as_not_done(self, analyzer, daemon):
        """Non-dict Queen result should be treated as done=False."""
        worker = _make_worker(state_since=time.time() - 60)
        task = _make_task()

        analyzer.queen.ask = AsyncMock(return_value="not a dict")

        with patch(_CAPTURE, new_callable=AsyncMock, return_value="output"):
            await analyzer.analyze_completion(worker, task)

        daemon.queue_proposal.assert_not_called()

    @pytest.mark.asyncio
    async def test_oserror_clears_inflight(self, analyzer, daemon):
        """OSError during capture_pane should clear inflight completion."""
        worker = _make_worker(state_since=time.time() - 60)
        task = _make_task()
        key = "api:t1"
        analyzer.track_completion(key)

        with patch(_CAPTURE, new_callable=AsyncMock, side_effect=OSError("fail")):
            await analyzer.analyze_completion(worker, task)

        assert analyzer.has_inflight_completion(key) is False
        daemon.queue_proposal.assert_not_called()

    @pytest.mark.asyncio
    async def test_timeout_clears_inflight(self, analyzer, daemon):
        """TimeoutError during Queen call should clear inflight completion."""
        worker = _make_worker(state_since=time.time() - 60)
        task = _make_task()
        key = "api:t1"
        analyzer.track_completion(key)

        analyzer.queen.ask = AsyncMock(side_effect=asyncio.TimeoutError())

        with patch(_CAPTURE, new_callable=AsyncMock, return_value="output"):
            await analyzer.analyze_completion(worker, task)

        assert analyzer.has_inflight_completion(key) is False

    @pytest.mark.asyncio
    async def test_duplicate_completion_proposal_dropped(self, analyzer, daemon):
        """If a pending completion proposal already exists, don't create another."""
        worker = _make_worker(state_since=time.time() - 60)
        task = _make_task()

        # Pre-add an existing completion proposal
        existing = AssignmentProposal.completion(
            worker_name="api",
            task_id="t1",
            task_title="Fix bug",
            assessment="done",
        )
        daemon.proposal_store.add(existing)

        analyzer.queen.ask = AsyncMock(
            return_value={
                "done": True,
                "resolution": "All tests pass, committed abc123",
                "confidence": 0.9,
            }
        )

        with patch(_CAPTURE, new_callable=AsyncMock, return_value="output"):
            await analyzer.analyze_completion(worker, task)

        daemon.queue_proposal.assert_not_called()

    @pytest.mark.asyncio
    async def test_result_missing_fields_uses_defaults(self, analyzer, daemon):
        """Queen result with missing fields should use defaults (done=False, confidence=0.3)."""
        worker = _make_worker(state_since=time.time() - 60)
        task = _make_task()

        # Return a dict but with no done/resolution/confidence keys
        analyzer.queen.ask = AsyncMock(return_value={"some_other_key": "value"})

        with patch(_CAPTURE, new_callable=AsyncMock, return_value="output"):
            await analyzer.analyze_completion(worker, task)

        # done defaults to False, so no proposal
        daemon.queue_proposal.assert_not_called()

    @pytest.mark.asyncio
    async def test_confidence_exactly_0_6_queues_proposal(self, analyzer, daemon):
        """Confidence of exactly 0.6 should NOT be skipped (threshold is < 0.6)."""
        worker = _make_worker(state_since=time.time() - 60)
        task = _make_task()

        analyzer.queen.ask = AsyncMock(
            return_value={
                "done": True,
                "resolution": "Committed fix, all tests pass",
                "confidence": 0.6,
            }
        )

        with patch(_CAPTURE, new_callable=AsyncMock, return_value="output"):
            await analyzer.analyze_completion(worker, task)

        daemon.queue_proposal.assert_called_once()

    @pytest.mark.asyncio
    async def test_pane_gone_error_clears_inflight(self, analyzer, daemon):
        """PaneGoneError should clear inflight completion and return."""
        from swarm.tmux.cell import PaneGoneError

        worker = _make_worker(state_since=time.time() - 60)
        task = _make_task()
        key = "api:t1"
        analyzer.track_completion(key)

        with patch(_CAPTURE, new_callable=AsyncMock, side_effect=PaneGoneError("gone")):
            await analyzer.analyze_completion(worker, task)

        assert analyzer.has_inflight_completion(key) is False
        daemon.queue_proposal.assert_not_called()


# ---------------------------------------------------------------------------
# gather_context tests
# ---------------------------------------------------------------------------


class TestGatherContext:
    """Tests for gather_context -- captures all worker panes."""

    @pytest.mark.asyncio
    async def test_gathers_all_workers(self, analyzer, daemon):
        """Should capture each worker's pane and build hive context."""
        w1 = _make_worker(name="api", pane_id="%0")
        w2 = _make_worker(name="web", pane_id="%1")
        daemon.workers = [w1, w2]

        async def mock_capture(pane_id, lines=None):
            return f"output for {pane_id}"

        with (
            patch(_CAPTURE, new_callable=AsyncMock, side_effect=mock_capture),
            patch(_BUILD_HIVE, return_value="HIVE CONTEXT") as mock_build,
        ):
            result = await analyzer.gather_context()

        assert result == "HIVE CONTEXT"
        mock_build.assert_called_once()
        call_kwargs = mock_build.call_args
        worker_outputs = call_kwargs[1]["worker_outputs"]
        assert "api" in worker_outputs
        assert "web" in worker_outputs

    @pytest.mark.asyncio
    async def test_tolerates_capture_failures(self, analyzer, daemon):
        """Failed pane captures should be silently skipped."""
        w1 = _make_worker(name="api", pane_id="%0")
        w2 = _make_worker(name="web", pane_id="%1")
        daemon.workers = [w1, w2]

        async def partial_capture(pane_id, lines=None):
            if pane_id == "%0":
                raise OSError("no tmux")
            return "web output"

        with (
            patch(_CAPTURE, new_callable=AsyncMock, side_effect=partial_capture),
            patch(_BUILD_HIVE, return_value="CONTEXT") as mock_build,
        ):
            result = await analyzer.gather_context()

        assert result == "CONTEXT"
        worker_outputs = mock_build.call_args[1]["worker_outputs"]
        # Only web should have output; api failed
        assert "api" not in worker_outputs
        assert worker_outputs["web"] == "web output"

    @pytest.mark.asyncio
    async def test_empty_workers(self, analyzer, daemon):
        """No workers should produce empty context call."""
        daemon.workers = []

        with (
            patch(_CAPTURE, new_callable=AsyncMock) as mock_capture,
            patch(_BUILD_HIVE, return_value="EMPTY") as mock_build,
        ):
            result = await analyzer.gather_context()

        assert result == "EMPTY"
        mock_capture.assert_not_called()
        worker_outputs = mock_build.call_args[1]["worker_outputs"]
        assert worker_outputs == {}

    @pytest.mark.asyncio
    async def test_timeout_error_skipped(self, analyzer, daemon):
        """TimeoutError during capture should be silently skipped."""
        w1 = _make_worker(name="api", pane_id="%0")
        daemon.workers = [w1]

        with (
            patch(_CAPTURE, new_callable=AsyncMock, side_effect=asyncio.TimeoutError()),
            patch(_BUILD_HIVE, return_value="CTX") as mock_build,
        ):
            result = await analyzer.gather_context()

        assert result == "CTX"
        worker_outputs = mock_build.call_args[1]["worker_outputs"]
        assert worker_outputs == {}

    @pytest.mark.asyncio
    async def test_pane_gone_error_skipped(self, analyzer, daemon):
        """PaneGoneError during capture should be silently skipped."""
        from swarm.tmux.cell import PaneGoneError

        w1 = _make_worker(name="api", pane_id="%0")
        daemon.workers = [w1]

        with (
            patch(_CAPTURE, new_callable=AsyncMock, side_effect=PaneGoneError("gone")),
            patch(_BUILD_HIVE, return_value="CTX") as mock_build,
        ):
            result = await analyzer.gather_context()

        assert result == "CTX"
        worker_outputs = mock_build.call_args[1]["worker_outputs"]
        assert worker_outputs == {}


# ---------------------------------------------------------------------------
# analyze_worker and coordinate tests
# ---------------------------------------------------------------------------


class TestAnalyzeWorkerAndCoordinate:
    """Tests for analyze_worker and coordinate methods."""

    @pytest.mark.asyncio
    async def test_analyze_worker_calls_queen(self, analyzer, daemon):
        """analyze_worker should capture pane and call queen.analyze_worker."""
        worker = _make_worker()
        daemon.workers = [worker]
        daemon._require_worker = MagicMock(return_value=worker)

        analyzer.queen.analyze_worker = AsyncMock(
            return_value={
                "assessment": "idle",
                "action": "wait",
            }
        )

        with (
            patch(_CAPTURE, new_callable=AsyncMock, return_value="pane content"),
            patch(_BUILD_TASK_INFO, return_value="task info"),
        ):
            result = await analyzer.analyze_worker("api")

        assert result["action"] == "wait"
        analyzer.queen.analyze_worker.assert_called_once()

    @pytest.mark.asyncio
    async def test_analyze_worker_with_force(self, analyzer, daemon):
        """analyze_worker(force=True) should pass force to queen.analyze_worker."""
        worker = _make_worker()
        daemon._require_worker = MagicMock(return_value=worker)

        analyzer.queen.analyze_worker = AsyncMock(return_value={"ok": True})

        with (
            patch(_CAPTURE, new_callable=AsyncMock, return_value="content"),
            patch(_BUILD_TASK_INFO, return_value=""),
        ):
            await analyzer.analyze_worker("api", force=True)

        call_kwargs = analyzer.queen.analyze_worker.call_args
        assert call_kwargs[1]["force"] is True

    @pytest.mark.asyncio
    async def test_coordinate_calls_queen(self, analyzer, daemon):
        """coordinate should gather context and call queen.coordinate_hive."""
        analyzer.queen.coordinate_hive = AsyncMock(
            return_value={
                "assessment": "all good",
                "directives": [],
            }
        )

        with patch.object(
            analyzer, "gather_context", new_callable=AsyncMock, return_value="hive ctx"
        ):
            result = await analyzer.coordinate()

        assert result["assessment"] == "all good"
        analyzer.queen.coordinate_hive.assert_called_once_with("hive ctx", force=False)

    @pytest.mark.asyncio
    async def test_coordinate_with_force(self, analyzer, daemon):
        """coordinate(force=True) should pass force to queen.coordinate_hive."""
        analyzer.queen.coordinate_hive = AsyncMock(return_value={"ok": True})

        with patch.object(analyzer, "gather_context", new_callable=AsyncMock, return_value="ctx"):
            await analyzer.coordinate(force=True)

        analyzer.queen.coordinate_hive.assert_called_once_with("ctx", force=True)
