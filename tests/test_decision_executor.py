"""Tests for drones/decision_executor.py — DecisionExecutor."""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from swarm.config.models import DroneConfig
from swarm.drones.decision_executor import DecisionExecutor
from swarm.drones.log import DroneAction, DroneLog, SystemAction
from swarm.drones.rules import Decision, DroneDecision
from swarm.worker.worker import WorkerState
from tests.conftest import make_worker


def _make_executor(
    workers: list | None = None,
    log: DroneLog | None = None,
    auto_mode: bool = False,
) -> tuple:
    """Create a DecisionExecutor with test defaults."""
    if workers is None:
        workers = [make_worker("api"), make_worker("web")]
    if log is None:
        log = DroneLog()
    events: list[tuple] = []
    drone_config = DroneConfig()
    escalated: dict[str, float] = {}
    revive_history: dict[str, list[float]] = {}

    directive_executor = MagicMock()
    directive_executor.has_pending_bash_approval.return_value = False
    directive_executor.has_idle_prompt.return_value = False
    directive_executor.has_operator_text_at_prompt.return_value = False

    def emit(event: str, *args: object) -> None:
        events.append((event, *args))

    def get_provider(worker: object) -> MagicMock:
        provider = MagicMock()
        provider.approval_response.return_value = "y"
        return provider

    de = DecisionExecutor(
        workers=workers,
        log=log,
        pool=None,
        drone_config=drone_config,
        auto_mode=auto_mode,
        emit=emit,
        get_provider=get_provider,
        directive_executor=directive_executor,
        escalated=escalated,
        revive_history=revive_history,
    )
    return de, log, events, escalated, revive_history


class TestDroneContinuedCallback:
    """Verify callback registration and default no-op."""

    def test_default_callback_is_noop(self) -> None:
        de, *_ = _make_executor()
        # Should not raise
        de._drone_continued_callback("test_worker")

    def test_set_callback_replaces_default(self) -> None:
        de, *_ = _make_executor()
        calls: list[str] = []
        de.set_drone_continued_callback(lambda name: calls.append(name))

        de._drone_continued_callback("api")

        assert calls == ["api"]

    def test_callback_receives_worker_name(self) -> None:
        de, *_ = _make_executor()
        received: list[str] = []
        de.set_drone_continued_callback(lambda name: received.append(name))

        de._drone_continued_callback("web")
        de._drone_continued_callback("api")

        assert received == ["web", "api"]


class TestEmitDecisions:
    """Verify set_emit_decisions toggle."""

    def test_emit_decisions_default_off(self) -> None:
        de, *_ = _make_executor()
        assert de._emit_decisions is False

    def test_set_emit_decisions_enables(self) -> None:
        de, *_ = _make_executor()
        de.set_emit_decisions(True)
        assert de._emit_decisions is True

    def test_set_emit_decisions_disables(self) -> None:
        de, *_ = _make_executor()
        de.set_emit_decisions(True)
        de.set_emit_decisions(False)
        assert de._emit_decisions is False


class TestShouldSkipDecide:
    """Verify _should_skip_decide logic."""

    def test_skip_when_disabled(self) -> None:
        de, *_ = _make_executor()
        worker = make_worker("api")
        assert de._should_skip_decide(worker, changed=False, enabled=False) is True

    def test_no_skip_when_enabled_no_escalation(self) -> None:
        de, *_ = _make_executor()
        worker = make_worker("api")
        assert de._should_skip_decide(worker, changed=False, enabled=True) is False

    def test_skip_when_escalated_no_change(self) -> None:
        de, _, _, escalated, _ = _make_executor()
        worker = make_worker("api")
        escalated["api"] = time.monotonic()

        assert de._should_skip_decide(worker, changed=False, enabled=True) is True

    def test_no_skip_when_escalated_with_change(self) -> None:
        de, _, _, escalated, _ = _make_executor()
        worker = make_worker("api")
        escalated["api"] = time.monotonic()

        assert de._should_skip_decide(worker, changed=True, enabled=True) is False

    def test_expired_escalation_clears_and_allows(self) -> None:
        de, _, _, escalated, _ = _make_executor()
        worker = make_worker("api")
        # Set escalation 200s ago (past 180s timeout)
        escalated["api"] = time.monotonic() - 200

        result = de._should_skip_decide(worker, changed=False, enabled=True)

        assert result is False
        assert "api" not in escalated


class TestEscalationSpamDetection:
    """Verify consecutive escalation suppression in _run_decision_sync."""

    def test_clear_escalation_spam(self) -> None:
        de, *_ = _make_executor()
        de._consecutive_escalations["api"] = (3, "some reason")

        de.clear_escalation_spam("api")

        assert "api" not in de._consecutive_escalations

    def test_clear_escalation_spam_noop_for_missing(self) -> None:
        de, *_ = _make_executor()
        # Should not raise
        de.clear_escalation_spam("nonexistent")


class TestReviveLoopDetection:
    """Verify _is_revive_loop and _record_revive."""

    def test_no_history_not_a_loop(self) -> None:
        de, *_ = _make_executor()
        assert de._is_revive_loop("api") is False

    def test_few_revives_not_a_loop(self) -> None:
        de, _, _, _, revive_history = _make_executor()
        now = time.monotonic()
        revive_history["api"] = [now - 5, now - 3]

        assert de._is_revive_loop("api") is False

    def test_many_revives_is_a_loop(self) -> None:
        de, _, _, _, revive_history = _make_executor()
        now = time.monotonic()
        # Default buzzing_confirm_count is 12, _revive_loop_max = 12
        revive_history["api"] = [now - i for i in range(15)]

        assert de._is_revive_loop("api") is True

    def test_old_revives_pruned(self) -> None:
        de, _, _, _, revive_history = _make_executor()
        # Set entries far outside the window
        revive_history["api"] = [0.0] * 20

        assert de._is_revive_loop("api") is False
        # Old entries should be pruned
        assert len(revive_history["api"]) == 0

    def test_record_revive_appends(self) -> None:
        de, _, _, _, revive_history = _make_executor()
        de._record_revive("api")

        assert len(revive_history["api"]) == 1

    def test_record_revive_prunes_dead_workers(self) -> None:
        workers = [make_worker("api")]
        de, _, _, _, revive_history = _make_executor(workers=workers)
        revive_history["dead_worker"] = [time.monotonic()]

        de._record_revive("api")

        assert "dead_worker" not in revive_history
        assert "api" in revive_history


class TestRunDecisionSync:
    """Verify _run_decision_sync routes decisions correctly."""

    def test_continue_decision_defers_action(self, monkeypatch: pytest.MonkeyPatch) -> None:
        de, *_ = _make_executor()
        worker = make_worker("api", state=WorkerState.WAITING)
        worker.process.set_content("> ")

        decision = DroneDecision(
            decision=Decision.CONTINUE, reason="auto-approve", source="builtin"
        )
        monkeypatch.setattr("swarm.drones.decision_executor.decide", lambda *a, **kw: decision)

        result = de._run_decision_sync(worker, "> ")

        assert result is True
        assert len(de._deferred_actions) == 1
        assert de._deferred_actions[0][0] == "continue"

    def test_revive_decision_defers_action(self, monkeypatch: pytest.MonkeyPatch) -> None:
        de, *_ = _make_executor()
        worker = make_worker("api", state=WorkerState.STUNG)

        decision = DroneDecision(decision=Decision.REVIVE, reason="process died", source="builtin")
        monkeypatch.setattr("swarm.drones.decision_executor.decide", lambda *a, **kw: decision)

        result = de._run_decision_sync(worker, "")

        assert result is True
        assert len(de._deferred_actions) == 1
        assert de._deferred_actions[0][0] == "revive"

    def test_escalate_decision_logs_and_emits(self, monkeypatch: pytest.MonkeyPatch) -> None:
        de, log, events, *_ = _make_executor()
        worker = make_worker("api", state=WorkerState.WAITING)

        decision = DroneDecision(
            decision=Decision.ESCALATE, reason="dangerous command", source="builtin"
        )
        monkeypatch.setattr("swarm.drones.decision_executor.decide", lambda *a, **kw: decision)

        result = de._run_decision_sync(worker, "DROP TABLE users")

        assert result is True
        escalate_events = [e for e in events if e[0] == "escalate"]
        assert len(escalate_events) == 1
        escalated_entries = [e for e in log.entries if e.action == SystemAction.ESCALATED]
        assert len(escalated_entries) == 1

    def test_escalate_spam_suppression(self, monkeypatch: pytest.MonkeyPatch) -> None:
        de, log, events, *_ = _make_executor()
        worker = make_worker("api", state=WorkerState.WAITING)
        reason = "same dangerous thing"

        decision = DroneDecision(decision=Decision.ESCALATE, reason=reason, source="builtin")
        monkeypatch.setattr("swarm.drones.decision_executor.decide", lambda *a, **kw: decision)

        # First 3 escalations should go through (1st, 2nd normal; 3rd = spam detection)
        for _ in range(3):
            de._run_decision_sync(worker, "content")

        escalate_events = [e for e in events if e[0] == "escalate"]
        assert len(escalate_events) == 3

        # 4th+ should be suppressed
        events.clear()
        de._run_decision_sync(worker, "content")
        escalate_events = [e for e in events if e[0] == "escalate"]
        assert len(escalate_events) == 0

    def test_escalate_different_reason_resets_counter(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        de, _, events, *_ = _make_executor()
        worker = make_worker("api", state=WorkerState.WAITING)

        # 3 escalations with same reason
        decision1 = DroneDecision(decision=Decision.ESCALATE, reason="reason_a", source="builtin")
        monkeypatch.setattr("swarm.drones.decision_executor.decide", lambda *a, **kw: decision1)
        for _ in range(3):
            de._run_decision_sync(worker, "content")

        # Now different reason — counter resets
        decision2 = DroneDecision(decision=Decision.ESCALATE, reason="reason_b", source="builtin")
        monkeypatch.setattr("swarm.drones.decision_executor.decide", lambda *a, **kw: decision2)
        events.clear()
        de._run_decision_sync(worker, "content")

        escalate_events = [e for e in events if e[0] == "escalate"]
        assert len(escalate_events) == 1  # not suppressed

    def test_emit_decisions_flag(self, monkeypatch: pytest.MonkeyPatch) -> None:
        de, _, events, *_ = _make_executor()
        worker = make_worker("api", state=WorkerState.WAITING)
        de.set_emit_decisions(True)

        decision = DroneDecision(decision=Decision.CONTINUE, reason="ok", source="builtin")
        monkeypatch.setattr("swarm.drones.decision_executor.decide", lambda *a, **kw: decision)

        de._run_decision_sync(worker, "content")

        decision_events = [e for e in events if e[0] == "drone_decision"]
        assert len(decision_events) == 1

    def test_none_decision_returns_false(self, monkeypatch: pytest.MonkeyPatch) -> None:
        de, *_ = _make_executor()
        worker = make_worker("api")

        decision = DroneDecision(decision=Decision.NONE, reason="", source="builtin")
        monkeypatch.setattr("swarm.drones.decision_executor.decide", lambda *a, **kw: decision)

        result = de._run_decision_sync(worker, "content")
        assert result is False


class TestSafeWorkerAction:
    """Verify _safe_worker_action success/failure handling."""

    @pytest.mark.asyncio
    async def test_success_logs_and_returns_true(self) -> None:
        de, log, *_ = _make_executor()
        worker = make_worker("api")
        decision = DroneDecision(
            decision=Decision.CONTINUE, reason="auto", source="builtin", rule_pattern=".*"
        )

        coro = AsyncMock()()
        result = await de._safe_worker_action(
            worker, coro, DroneAction.CONTINUED, decision, include_rule_pattern=True
        )

        assert result is True
        assert de._had_substantive_action is True
        entries = [e for e in log.entries if e.action == SystemAction.CONTINUED]
        assert len(entries) == 1

    @pytest.mark.asyncio
    async def test_failure_returns_false(self) -> None:
        from swarm.pty.process import ProcessError

        de, log, *_ = _make_executor()
        worker = make_worker("api")

        async def failing_coro() -> None:
            raise ProcessError("dead")

        result = await de._safe_worker_action(worker, failing_coro(), DroneAction.CONTINUED, None)

        assert result is False
        assert de._had_substantive_action is False

    @pytest.mark.asyncio
    async def test_logs_prompt_snippet(self) -> None:
        de, log, *_ = _make_executor()
        worker = make_worker("api")
        decision = DroneDecision(decision=Decision.CONTINUE, reason="ok", source="rule")

        result = await de._safe_worker_action(
            worker,
            AsyncMock()(),
            DroneAction.CONTINUED,
            decision,
            prompt_snippet="Do you want to proceed?",
        )

        assert result is True
        entry = log.entries[-1]
        assert entry.metadata.get("prompt_snippet") == "Do you want to proceed?"


class TestExecuteDeferredCompact:
    """Verify _execute_deferred_compact."""

    @pytest.mark.asyncio
    async def test_compact_sends_command(self) -> None:
        de, *_ = _make_executor()
        worker = make_worker("api", state=WorkerState.BUZZING)
        proc = worker.process

        await de._execute_deferred_compact(worker, proc)

        assert any("/compact" in k for k in proc.keys_sent)

    @pytest.mark.asyncio
    async def test_compact_skips_non_buzzing(self) -> None:
        de, *_ = _make_executor()
        worker = make_worker("api", state=WorkerState.RESTING)
        worker.compacting = True

        await de._execute_deferred_compact(worker, worker.process)

        assert worker.compacting is False
        assert len(worker.process.keys_sent) == 0

    @pytest.mark.asyncio
    async def test_compact_clears_flag_when_no_process(self) -> None:
        de, *_ = _make_executor()
        worker = make_worker("api", state=WorkerState.BUZZING)
        worker.compacting = True
        worker.process = None

        await de._execute_deferred_compact(worker, None)

        assert worker.compacting is False
