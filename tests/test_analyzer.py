"""Tests for server/analyzer.py — QueenAnalyzer escalation/completion analysis."""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from swarm.drones.log import DroneLog, LogCategory, SystemAction
from swarm.pty.process import ProcessError
from swarm.queen.queue import QueenCallQueue
from swarm.queen.queen import Queen
from swarm.config import QueenConfig
from swarm.server.analyzer import QueenAnalyzer
from swarm.tasks.proposal import AssignmentProposal, ProposalStore
from swarm.tasks.task import SwarmTask
from swarm.worker.worker import Worker, WorkerState
from tests.fakes.process import FakeWorkerProcess


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Patch targets for locally-imported functions inside analyzer methods
_REVIVE = "swarm.worker.manager.revive_worker"
_BUILD_HIVE = "swarm.queen.context.build_hive_context"
_BUILD_TASK_INFO = "swarm.tasks.proposal.build_worker_task_info"


def _make_worker(
    name: str = "api",
    state: WorkerState = WorkerState.RESTING,
    state_since: float | None = None,
) -> Worker:
    w = Worker(name=name, path="/tmp", process=FakeWorkerProcess(name=name), state=state)
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
    d.pool = None
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
def queue():
    return QueenCallQueue(max_concurrent=2)


@pytest.fixture
def analyzer(queen, daemon, queue):
    return QueenAnalyzer(queen, daemon, queue)


# ---------------------------------------------------------------------------
# Queue delegation tests
# ---------------------------------------------------------------------------


class TestQueueDelegation:
    """Test that analyzer delegates dedup to the QueenCallQueue."""

    def test_has_inflight_escalation_delegates(self, analyzer, queue):
        assert analyzer.has_inflight_escalation("api") is False
        queue._all_keys.add("escalation:api")
        assert analyzer.has_inflight_escalation("api") is True

    def test_has_inflight_completion_delegates(self, analyzer, queue):
        key = "api:t1"
        assert analyzer.has_inflight_completion(key) is False
        queue._all_keys.add(f"completion:{key}")
        assert analyzer.has_inflight_completion(key) is True

    def test_clear_worker_inflight_delegates(self, analyzer, queue):
        """clear_worker_inflight should call queue.clear_worker."""
        # Add a queued request for "api"
        from swarm.queen.queue import QueenCallRequest

        req = QueenCallRequest(
            call_type="escalation",
            coro_factory=lambda: None,
            worker_name="api",
            worker_state_at_enqueue="RESTING",
            dedup_key="escalation:api",
            force=False,
        )
        queue._queue.append(req)
        queue._all_keys.add("escalation:api")

        analyzer.clear_worker_inflight("api")
        assert not queue.has_pending("escalation:api")

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
        worker.process.set_content("worker output")

        queen_result = {
            "action": "continue",
            "confidence": 0.9,
            "assessment": "Worker waiting for input",
            "reasoning": "Idle at prompt",
            "message": "",
        }
        analyzer.queen.analyze_worker = AsyncMock(return_value=queen_result)

        with patch.object(analyzer, "gather_context", new_callable=AsyncMock, return_value="ctx"):
            await analyzer.analyze_escalation(worker, "unrecognized state")

        # Should auto-execute, not queue
        daemon.queue_proposal.assert_not_called()
        daemon.broadcast_ws.assert_called_once()
        ws_data = daemon.broadcast_ws.call_args[0][0]
        assert ws_data["type"] == "queen_auto_acted"
        assert ws_data["action"] == "continue"
        # Verify enter was sent via process
        assert "\n" in worker.process.keys_sent

    @pytest.mark.asyncio
    async def test_auto_act_restart(self, analyzer, daemon):
        """High-confidence 'restart' action should auto-execute."""
        worker = _make_worker()
        daemon.workers = [worker]
        analyzer.queen.min_confidence = 0.7
        worker.process.set_content("error")

        queen_result = {
            "action": "restart",
            "confidence": 0.95,
            "assessment": "Worker crashed",
            "reasoning": "Segfault detected",
            "message": "",
        }
        analyzer.queen.analyze_worker = AsyncMock(return_value=queen_result)

        with (
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
        worker.process.set_content("output")

        queen_result = {
            "action": "continue",
            "confidence": 0.5,
            "assessment": "Possibly stuck",
            "reasoning": "Not certain",
            "message": "",
        }
        analyzer.queen.analyze_worker = AsyncMock(return_value=queen_result)

        with patch.object(analyzer, "gather_context", new_callable=AsyncMock, return_value="ctx"):
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
        worker.process.set_content("output")

        queen_result = {
            "action": "continue",
            "confidence": 0.99,
            "assessment": "User asked a question",
            "reasoning": "Question in pane",
            "message": "",
        }
        analyzer.queen.analyze_worker = AsyncMock(return_value=queen_result)

        with patch.object(analyzer, "gather_context", new_callable=AsyncMock, return_value="ctx"):
            await analyzer.analyze_escalation(worker, "user question detected")

        daemon.queue_proposal.assert_called_once()

    @pytest.mark.asyncio
    async def test_plan_reason_always_queues(self, analyzer, daemon):
        """Exact 'plan requires user approval' reason always requires user approval."""
        worker = _make_worker()
        analyzer.queen.min_confidence = 0.7
        worker.process.set_content("output")

        queen_result = {
            "action": "continue",
            "confidence": 0.99,
            "assessment": "Plan submitted",
            "reasoning": "Agent presenting a plan",
            "message": "",
        }
        analyzer.queen.analyze_worker = AsyncMock(return_value=queen_result)

        with patch.object(analyzer, "gather_context", new_callable=AsyncMock, return_value="ctx"):
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
        worker.process.set_content("worker output")

        queen_result = {
            "action": "continue",
            "confidence": 0.9,
            "assessment": "Safe read-only grep command",
            "reasoning": "Worker needs permission for grep",
            "message": "",
        }
        analyzer.queen.analyze_worker = AsyncMock(return_value=queen_result)

        with patch.object(analyzer, "gather_context", new_callable=AsyncMock, return_value="ctx"):
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
        worker.process.set_content("output")

        queen_result = {
            "action": "send_message",
            "confidence": 0.99,
            "assessment": "Need to send input",
            "reasoning": "Worker waiting",
            "message": "yes",
        }
        analyzer.queen.analyze_worker = AsyncMock(return_value=queen_result)

        with patch.object(analyzer, "gather_context", new_callable=AsyncMock, return_value="ctx"):
            await analyzer.analyze_escalation(worker, "unrecognized state")

        daemon.queue_proposal.assert_called_once()

    @pytest.mark.asyncio
    async def test_non_dict_result_returns_early(self, analyzer, daemon):
        """Non-dict Queen result should be silently ignored."""
        worker = _make_worker()
        analyzer.queen.analyze_worker = AsyncMock(return_value="not a dict")
        worker.process.set_content("output")

        with patch.object(analyzer, "gather_context", new_callable=AsyncMock, return_value="ctx"):
            await analyzer.analyze_escalation(worker, "test")

        daemon.queue_proposal.assert_not_called()

    @pytest.mark.asyncio
    async def test_empty_analysis_dropped(self, analyzer, daemon):
        """Queen result with no assessment/reasoning/message is dropped."""
        worker = _make_worker()
        worker.process.set_content("output")
        queen_result = {
            "action": "continue",
            "confidence": 0.9,
            "assessment": "",
            "reasoning": "",
            "message": "",
        }
        analyzer.queen.analyze_worker = AsyncMock(return_value=queen_result)

        with patch.object(analyzer, "gather_context", new_callable=AsyncMock, return_value="ctx"):
            await analyzer.analyze_escalation(worker, "test")

        daemon.queue_proposal.assert_not_called()

    @pytest.mark.asyncio
    async def test_duplicate_proposal_dropped(self, analyzer, daemon):
        """If a pending escalation proposal already exists, don't create another."""
        worker = _make_worker()
        worker.process.set_content("output")
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

        with patch.object(analyzer, "gather_context", new_callable=AsyncMock, return_value="ctx"):
            await analyzer.analyze_escalation(worker, "test")

        daemon.queue_proposal.assert_not_called()

    @pytest.mark.asyncio
    async def test_oserror_returns_gracefully(self, analyzer, daemon):
        """OSError during get_content should return gracefully."""
        worker = _make_worker()
        worker.process.get_content = MagicMock(side_effect=OSError("boom"))

        await analyzer.analyze_escalation(worker, "test")

        daemon.queue_proposal.assert_not_called()

    @pytest.mark.asyncio
    async def test_process_error_returns_gracefully(self, analyzer, daemon):
        """ProcessError during get_content should return gracefully."""
        worker = _make_worker()
        worker.process.get_content = MagicMock(side_effect=ProcessError("gone"))

        await analyzer.analyze_escalation(worker, "test")

        daemon.queue_proposal.assert_not_called()

    @pytest.mark.asyncio
    async def test_process_error_on_escalation_returns_gracefully(self, analyzer, daemon):
        """ProcessError during escalation analysis should return gracefully."""
        worker = _make_worker()
        worker.process.get_content = MagicMock(side_effect=ProcessError("fail"))

        await analyzer.analyze_escalation(worker, "test")

        daemon.queue_proposal.assert_not_called()

    @pytest.mark.asyncio
    async def test_default_confidence(self, analyzer, daemon):
        """When Queen omits confidence, default to 0.8."""
        worker = _make_worker()
        worker.process.set_content("output")
        analyzer.queen.min_confidence = 0.9  # above default 0.8

        queen_result = {
            "action": "continue",
            # no "confidence" key
            "assessment": "Worker idle",
            "reasoning": "Needs attention",
            "message": "",
        }
        analyzer.queen.analyze_worker = AsyncMock(return_value=queen_result)

        with patch.object(analyzer, "gather_context", new_callable=AsyncMock, return_value="ctx"):
            await analyzer.analyze_escalation(worker, "test")

        # 0.8 < 0.9 min_confidence, so should queue instead of auto-act
        daemon.queue_proposal.assert_called_once()

    @pytest.mark.asyncio
    async def test_wait_action_queues_proposal(self, analyzer, daemon):
        """'wait' action is not in safe_auto_actions, so always queued."""
        worker = _make_worker()
        worker.process.set_content("output")
        analyzer.queen.min_confidence = 0.7

        queen_result = {
            "action": "wait",
            "confidence": 0.99,
            "assessment": "No action needed",
            "reasoning": "Worker is fine",
            "message": "",
        }
        analyzer.queen.analyze_worker = AsyncMock(return_value=queen_result)

        with patch.object(analyzer, "gather_context", new_callable=AsyncMock, return_value="ctx"):
            await analyzer.analyze_escalation(worker, "unrecognized state")

        daemon.queue_proposal.assert_called_once()

    @pytest.mark.asyncio
    async def test_escalation_logs_queen_escalation_entry(self, analyzer, daemon):
        """analyze_escalation should create a QUEEN_ESCALATION system log entry with metadata."""
        worker = _make_worker()
        worker.process.set_content("output")
        analyzer.queen.min_confidence = 0.7

        queen_result = {
            "action": "continue",
            "confidence": 0.5,
            "assessment": "Worker seems stuck",
            "reasoning": "Idle for a while",
            "message": "",
        }
        analyzer.queen.analyze_worker = AsyncMock(return_value=queen_result)

        with patch.object(analyzer, "gather_context", new_callable=AsyncMock, return_value="ctx"):
            await analyzer.analyze_escalation(worker, "test")

        entries = [e for e in daemon.drone_log.entries if e.action == SystemAction.QUEEN_ESCALATION]
        assert len(entries) == 1
        assert entries[0].category == LogCategory.QUEEN
        assert entries[0].metadata["queen_action"] == "continue"
        assert entries[0].metadata["confidence"] == 0.5
        assert "duration_s" in entries[0].metadata

    @pytest.mark.asyncio
    async def test_escalation_emits_queen_analysis_event(self, analyzer, daemon):
        """analyze_escalation should emit a queen_analysis event on daemon."""
        worker = _make_worker()
        worker.process.set_content("output")
        queen_result = {
            "action": "continue",
            "confidence": 0.5,
            "assessment": "Stuck",
            "reasoning": "Idle",
            "message": "",
        }
        analyzer.queen.analyze_worker = AsyncMock(return_value=queen_result)

        with patch.object(analyzer, "gather_context", new_callable=AsyncMock, return_value="ctx"):
            await analyzer.analyze_escalation(worker, "test")

        # Verify daemon.emit was called with queen_analysis event
        emit_calls = [c for c in daemon.emit.call_args_list if c[0][0] == "queen_analysis"]
        assert len(emit_calls) == 1
        _, wn, action, reasoning, conf = emit_calls[0][0]
        assert wn == "api"
        assert action == "continue"
        assert conf == 0.5

    @pytest.mark.asyncio
    async def test_short_idle_wait_confidence_clamped(self, analyzer, daemon):
        """Queen returning wait + high confidence for short-idle worker gets clamped to 0.47."""
        worker = _make_worker(state_since=time.time() - 47)  # idle 47s
        worker.process.set_content("output")
        analyzer.queen.min_confidence = 0.7

        queen_result = {
            "action": "wait",
            "confidence": 0.93,
            "assessment": "Worker resting briefly",
            "reasoning": "Short idle",
            "message": "",
        }
        analyzer.queen.analyze_worker = AsyncMock(return_value=queen_result)

        with patch.object(analyzer, "gather_context", new_callable=AsyncMock, return_value="ctx"):
            await analyzer.analyze_escalation(worker, "unrecognized state")

        # Confidence should be clamped → queued as proposal (not auto-acted)
        daemon.queue_proposal.assert_called_once()
        proposal = daemon.queue_proposal.call_args[0][0]
        assert proposal.confidence == 0.47

    @pytest.mark.asyncio
    async def test_long_idle_wait_confidence_not_clamped(self, analyzer, daemon):
        """Queen returning wait + high confidence for long-idle worker is NOT clamped."""
        worker = _make_worker(state_since=time.time() - 120)  # idle 120s
        worker.process.set_content("output")
        analyzer.queen.min_confidence = 0.7

        queen_result = {
            "action": "wait",
            "confidence": 0.85,
            "assessment": "Worker idle a while",
            "reasoning": "Long idle",
            "message": "",
        }
        analyzer.queen.analyze_worker = AsyncMock(return_value=queen_result)

        with patch.object(analyzer, "gather_context", new_callable=AsyncMock, return_value="ctx"):
            await analyzer.analyze_escalation(worker, "unrecognized state")

        # "wait" always queues as proposal, but confidence should NOT be clamped
        daemon.queue_proposal.assert_called_once()
        proposal = daemon.queue_proposal.call_args[0][0]
        assert proposal.confidence == 0.85


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

        result = await analyzer.execute_escalation(proposal)

        assert result is True
        assert "yes\n" in worker.process.keys_sent

    @pytest.mark.asyncio
    async def test_continue(self, analyzer, daemon):
        worker = _make_worker()
        daemon.get_worker = MagicMock(return_value=worker)
        proposal = AssignmentProposal.escalation(
            worker_name="api",
            action="continue",
            assessment="idle",
        )

        result = await analyzer.execute_escalation(proposal)

        assert result is True
        assert "\n" in worker.process.keys_sent

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
        mock_revive.assert_called_once_with(worker, None)
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
        worker.process.set_content("$ tests pass")
        task = _make_task()

        analyzer.queen.ask = AsyncMock(
            return_value={
                "done": True,
                "resolution": "All tests pass, committed fix abc123",
                "confidence": 0.9,
            }
        )

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
        worker.process.set_content("running...")
        task = _make_task()

        analyzer.queen.ask = AsyncMock(
            return_value={
                "done": False,
                "resolution": "Worker still running tests",
                "confidence": 0.8,
            }
        )

        await analyzer.analyze_completion(worker, task)

        daemon.queue_proposal.assert_not_called()

    @pytest.mark.asyncio
    async def test_low_confidence_skips_proposal(self, analyzer, daemon):
        """Done=true but confidence below 0.6 should skip proposal."""
        worker = _make_worker(state_since=time.time() - 60)
        worker.process.set_content("output")
        task = _make_task()

        analyzer.queen.ask = AsyncMock(
            return_value={
                "done": True,
                "resolution": "Maybe done, hard to tell",
                "confidence": 0.4,
            }
        )

        await analyzer.analyze_completion(worker, task)

        daemon.queue_proposal.assert_not_called()

    @pytest.mark.asyncio
    async def test_buzzing_worker_aborts_early(self, analyzer, daemon):
        """If worker resumed BUZZING before analysis, abort immediately."""
        worker = _make_worker(state=WorkerState.BUZZING)
        task = _make_task()

        await analyzer.analyze_completion(worker, task)

        daemon.queue_proposal.assert_not_called()

    @pytest.mark.asyncio
    async def test_buzzing_after_queen_call_aborts(self, analyzer, daemon):
        """If worker resumes BUZZING while Queen is thinking, drop proposal."""
        worker = _make_worker(state_since=time.time() - 60)
        worker.process.set_content("output")
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

        await analyzer.analyze_completion(worker, task)

        daemon.queue_proposal.assert_not_called()

    @pytest.mark.asyncio
    async def test_idle_fallback_resolution_rejected(self, analyzer, daemon):
        """Resolution matching 'worker X idle for Ns' pattern is a fallback -- skip it."""
        worker = _make_worker(state_since=time.time() - 90)
        worker.process.set_content("output")
        task = _make_task()

        analyzer.queen.ask = AsyncMock(
            return_value={
                "done": True,
                "resolution": "Worker api idle for 90s",
                "confidence": 0.8,
            }
        )

        await analyzer.analyze_completion(worker, task)

        daemon.queue_proposal.assert_not_called()

    @pytest.mark.asyncio
    async def test_idle_fallback_has_been_idle_rejected(self, analyzer, daemon):
        """Resolution matching 'worker X has been idle for Ns' is a fallback -- skip."""
        worker = _make_worker(state_since=time.time() - 60)
        worker.process.set_content("output")
        task = _make_task()

        analyzer.queen.ask = AsyncMock(
            return_value={
                "done": True,
                "resolution": "Worker api has been idle for 120s",
                "confidence": 0.85,
            }
        )

        await analyzer.analyze_completion(worker, task)

        daemon.queue_proposal.assert_not_called()

    @pytest.mark.asyncio
    async def test_contradiction_done_but_not_complete_text(self, analyzer, daemon):
        """Queen says done=true but resolution says 'not complete' => override to not done."""
        worker = _make_worker(state_since=time.time() - 60)
        worker.process.set_content("output")
        task = _make_task()

        analyzer.queen.ask = AsyncMock(
            return_value={
                "done": True,
                "resolution": "Task is not complete, tests failing",
                "confidence": 0.8,
            }
        )

        await analyzer.analyze_completion(worker, task)

        daemon.queue_proposal.assert_not_called()

    @pytest.mark.asyncio
    async def test_contradiction_could_not_be_verified(self, analyzer, daemon):
        """Resolution containing 'could not be verified' contradicts done=true."""
        worker = _make_worker(state_since=time.time() - 60)
        worker.process.set_content("output")
        task = _make_task()

        analyzer.queen.ask = AsyncMock(
            return_value={
                "done": True,
                "resolution": "Completion could not be verified from output",
                "confidence": 0.8,
            }
        )

        await analyzer.analyze_completion(worker, task)

        daemon.queue_proposal.assert_not_called()

    @pytest.mark.asyncio
    async def test_contradiction_unable_to_confirm(self, analyzer, daemon):
        """Resolution with 'unable to confirm' contradicts done=true."""
        worker = _make_worker(state_since=time.time() - 60)
        worker.process.set_content("output")
        task = _make_task()

        analyzer.queen.ask = AsyncMock(
            return_value={
                "done": True,
                "resolution": "Unable to confirm task completion",
                "confidence": 0.8,
            }
        )

        await analyzer.analyze_completion(worker, task)

        daemon.queue_proposal.assert_not_called()

    @pytest.mark.asyncio
    async def test_contradiction_recommend_rerunning(self, analyzer, daemon):
        """Resolution with 'recommend re-running' contradicts done=true."""
        worker = _make_worker(state_since=time.time() - 60)
        worker.process.set_content("output")
        task = _make_task()

        analyzer.queen.ask = AsyncMock(
            return_value={
                "done": True,
                "resolution": "Tests may have passed but recommend re-running",
                "confidence": 0.8,
            }
        )

        await analyzer.analyze_completion(worker, task)

        daemon.queue_proposal.assert_not_called()

    @pytest.mark.asyncio
    async def test_contradiction_needs_more_work(self, analyzer, daemon):
        """Resolution with 'needs more work' contradicts done=true."""
        worker = _make_worker(state_since=time.time() - 60)
        worker.process.set_content("output")
        task = _make_task()

        analyzer.queen.ask = AsyncMock(
            return_value={
                "done": True,
                "resolution": "Task needs more work on edge cases",
                "confidence": 0.8,
            }
        )

        await analyzer.analyze_completion(worker, task)

        daemon.queue_proposal.assert_not_called()

    @pytest.mark.asyncio
    async def test_contradiction_did_not_complete(self, analyzer, daemon):
        """Resolution with 'did not complete' contradicts done=true."""
        worker = _make_worker(state_since=time.time() - 60)
        worker.process.set_content("output")
        task = _make_task()

        analyzer.queen.ask = AsyncMock(
            return_value={
                "done": True,
                "resolution": "Worker did not complete the assigned task",
                "confidence": 0.8,
            }
        )

        await analyzer.analyze_completion(worker, task)

        daemon.queue_proposal.assert_not_called()

    @pytest.mark.asyncio
    async def test_contradiction_went_idle_without(self, analyzer, daemon):
        """Resolution with 'went idle without' contradicts done=true."""
        worker = _make_worker(state_since=time.time() - 60)
        worker.process.set_content("output")
        task = _make_task()

        analyzer.queen.ask = AsyncMock(
            return_value={
                "done": True,
                "resolution": "Worker went idle without finishing",
                "confidence": 0.8,
            }
        )

        await analyzer.analyze_completion(worker, task)

        daemon.queue_proposal.assert_not_called()

    @pytest.mark.asyncio
    async def test_non_dict_result_treated_as_not_done(self, analyzer, daemon):
        """Non-dict Queen result should be treated as done=False."""
        worker = _make_worker(state_since=time.time() - 60)
        worker.process.set_content("output")
        task = _make_task()

        analyzer.queen.ask = AsyncMock(return_value="not a dict")

        await analyzer.analyze_completion(worker, task)

        daemon.queue_proposal.assert_not_called()

    @pytest.mark.asyncio
    async def test_oserror_returns_gracefully(self, analyzer, daemon):
        """OSError during get_content should return gracefully."""
        worker = _make_worker(state_since=time.time() - 60)
        worker.process.get_content = MagicMock(side_effect=OSError("fail"))
        task = _make_task()

        await analyzer.analyze_completion(worker, task)

        daemon.queue_proposal.assert_not_called()

    @pytest.mark.asyncio
    async def test_timeout_returns_gracefully(self, analyzer, daemon):
        """TimeoutError during Queen call should return gracefully."""
        worker = _make_worker(state_since=time.time() - 60)
        worker.process.set_content("output")
        task = _make_task()

        analyzer.queen.ask = AsyncMock(side_effect=asyncio.TimeoutError())

        await analyzer.analyze_completion(worker, task)

        daemon.queue_proposal.assert_not_called()

    @pytest.mark.asyncio
    async def test_duplicate_completion_proposal_dropped(self, analyzer, daemon):
        """If a pending completion proposal already exists, don't create another."""
        worker = _make_worker(state_since=time.time() - 60)
        worker.process.set_content("output")
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

        await analyzer.analyze_completion(worker, task)

        daemon.queue_proposal.assert_not_called()

    @pytest.mark.asyncio
    async def test_result_missing_fields_uses_defaults(self, analyzer, daemon):
        """Queen result with missing fields should use defaults (done=False, confidence=0.3)."""
        worker = _make_worker(state_since=time.time() - 60)
        worker.process.set_content("output")
        task = _make_task()

        # Return a dict but with no done/resolution/confidence keys
        analyzer.queen.ask = AsyncMock(return_value={"some_other_key": "value"})

        await analyzer.analyze_completion(worker, task)

        # done defaults to False, so no proposal
        daemon.queue_proposal.assert_not_called()

    @pytest.mark.asyncio
    async def test_confidence_exactly_0_6_queues_proposal(self, analyzer, daemon):
        """Confidence of exactly 0.6 should NOT be skipped (threshold is < 0.6)."""
        worker = _make_worker(state_since=time.time() - 60)
        worker.process.set_content("output")
        task = _make_task()

        analyzer.queen.ask = AsyncMock(
            return_value={
                "done": True,
                "resolution": "Committed fix, all tests pass",
                "confidence": 0.6,
            }
        )

        await analyzer.analyze_completion(worker, task)

        daemon.queue_proposal.assert_called_once()

    @pytest.mark.asyncio
    async def test_process_error_returns_gracefully(self, analyzer, daemon):
        """ProcessError should return gracefully."""
        worker = _make_worker(state_since=time.time() - 60)
        worker.process.get_content = MagicMock(side_effect=ProcessError("gone"))
        task = _make_task()

        await analyzer.analyze_completion(worker, task)

        daemon.queue_proposal.assert_not_called()

    @pytest.mark.asyncio
    async def test_completion_logs_queen_completion_entry(self, analyzer, daemon):
        """analyze_completion should create a QUEEN_COMPLETION system log entry with metadata."""
        worker = _make_worker(state_since=time.time() - 120)
        worker.process.set_content("$ tests pass")
        task = _make_task()

        analyzer.queen.ask = AsyncMock(
            return_value={
                "done": True,
                "resolution": "All tests pass, committed fix abc123",
                "confidence": 0.9,
            }
        )

        await analyzer.analyze_completion(worker, task)

        entries = [e for e in daemon.drone_log.entries if e.action == SystemAction.QUEEN_COMPLETION]
        assert len(entries) == 1
        assert entries[0].category == LogCategory.QUEEN
        assert entries[0].metadata["done"] is True
        assert entries[0].metadata["confidence"] == 0.9
        assert entries[0].metadata["task_id"] == "t1"
        assert "duration_s" in entries[0].metadata

    @pytest.mark.asyncio
    async def test_completion_emits_queen_analysis_event(self, analyzer, daemon):
        """analyze_completion should emit a queen_analysis event on daemon."""
        worker = _make_worker(state_since=time.time() - 120)
        worker.process.set_content("$ tests pass")
        task = _make_task()

        analyzer.queen.ask = AsyncMock(
            return_value={
                "done": True,
                "resolution": "Committed fix abc123",
                "confidence": 0.9,
            }
        )

        await analyzer.analyze_completion(worker, task)

        emit_calls = [c for c in daemon.emit.call_args_list if c[0][0] == "queen_analysis"]
        assert len(emit_calls) == 1
        _, wn, action, resolution, conf = emit_calls[0][0]
        assert wn == "api"
        assert action == "complete_task"
        assert conf == 0.9

    @pytest.mark.asyncio
    async def test_completion_not_done_emits_wait_event(self, analyzer, daemon):
        """analyze_completion with done=False should emit queen_analysis with action='wait'."""
        worker = _make_worker(state_since=time.time() - 60)
        worker.process.set_content("running...")
        task = _make_task()

        analyzer.queen.ask = AsyncMock(
            return_value={
                "done": False,
                "resolution": "Still running tests",
                "confidence": 0.8,
            }
        )

        await analyzer.analyze_completion(worker, task)

        emit_calls = [c for c in daemon.emit.call_args_list if c[0][0] == "queen_analysis"]
        assert len(emit_calls) == 1
        assert emit_calls[0][0][2] == "wait"


# ---------------------------------------------------------------------------
# gather_context tests
# ---------------------------------------------------------------------------


class TestGatherContext:
    """Tests for gather_context -- captures all worker process outputs."""

    @pytest.mark.asyncio
    async def test_gathers_all_workers(self, analyzer, daemon):
        """Should capture each worker's process output and build hive context."""
        w1 = _make_worker(name="api")
        w2 = _make_worker(name="web")
        w1.process.set_content("output for api")
        w2.process.set_content("output for web")
        daemon.workers = [w1, w2]

        with patch(_BUILD_HIVE, return_value="HIVE CONTEXT") as mock_build:
            result = await analyzer.gather_context()

        assert result == "HIVE CONTEXT"
        mock_build.assert_called_once()
        call_kwargs = mock_build.call_args
        worker_outputs = call_kwargs[1]["worker_outputs"]
        assert "api" in worker_outputs
        assert "web" in worker_outputs

    @pytest.mark.asyncio
    async def test_tolerates_capture_failures(self, analyzer, daemon):
        """Failed process captures should be silently skipped."""
        w1 = _make_worker(name="api")
        w2 = _make_worker(name="web")
        w1.process.get_content = MagicMock(side_effect=OSError("no process"))
        w2.process.set_content("web output")
        daemon.workers = [w1, w2]

        with patch(_BUILD_HIVE, return_value="CONTEXT") as mock_build:
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

        with patch(_BUILD_HIVE, return_value="EMPTY") as mock_build:
            result = await analyzer.gather_context()

        assert result == "EMPTY"
        worker_outputs = mock_build.call_args[1]["worker_outputs"]
        assert worker_outputs == {}

    @pytest.mark.asyncio
    async def test_oserror_skipped(self, analyzer, daemon):
        """OSError during get_content should be silently skipped."""
        w1 = _make_worker(name="api")
        w1.process.get_content = MagicMock(side_effect=OSError("process dead"))
        daemon.workers = [w1]

        with patch(_BUILD_HIVE, return_value="CTX") as mock_build:
            result = await analyzer.gather_context()

        assert result == "CTX"
        worker_outputs = mock_build.call_args[1]["worker_outputs"]
        assert worker_outputs == {}

    @pytest.mark.asyncio
    async def test_process_error_skipped(self, analyzer, daemon):
        """ProcessError during get_content should be silently skipped."""
        w1 = _make_worker(name="api")
        w1.process.get_content = MagicMock(side_effect=ProcessError("gone"))
        daemon.workers = [w1]

        with patch(_BUILD_HIVE, return_value="CTX") as mock_build:
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
        """analyze_worker should read process content and call queen.analyze_worker."""
        worker = _make_worker()
        worker.process.set_content("worker output")
        daemon.workers = [worker]
        daemon._require_worker = MagicMock(return_value=worker)

        analyzer.queen.analyze_worker = AsyncMock(
            return_value={
                "assessment": "idle",
                "action": "wait",
            }
        )

        with patch(_BUILD_TASK_INFO, return_value="task info"):
            result = await analyzer.analyze_worker("api")

        assert result["action"] == "wait"
        analyzer.queen.analyze_worker.assert_called_once()

    @pytest.mark.asyncio
    async def test_analyze_worker_with_force(self, analyzer, daemon):
        """analyze_worker(force=True) should pass force to queen.analyze_worker."""
        worker = _make_worker()
        worker.process.set_content("content")
        daemon._require_worker = MagicMock(return_value=worker)

        analyzer.queen.analyze_worker = AsyncMock(return_value={"ok": True})

        with patch(_BUILD_TASK_INFO, return_value=""):
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
