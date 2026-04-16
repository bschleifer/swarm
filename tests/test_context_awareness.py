"""Tests for context window awareness."""

from __future__ import annotations

from unittest.mock import MagicMock

from swarm.config import DroneConfig
from swarm.drones.log import DroneLog
from swarm.drones.state_tracker import WorkerStateTracker
from swarm.notify.bus import EventType, NotificationBus, Severity
from swarm.worker.usage import estimate_context_usage
from swarm.worker.worker import TokenUsage, Worker, WorkerState


class TestEstimateContextUsage:
    def test_zero_tokens(self) -> None:
        usage = TokenUsage()
        assert estimate_context_usage(usage) == 0.0

    def test_last_turn_half_context(self) -> None:
        usage = TokenUsage(input_tokens=2_000_000, last_turn_input_tokens=500_000)
        pct = estimate_context_usage(usage, "claude")
        assert 0.49 < pct < 0.51

    def test_last_turn_full_context(self) -> None:
        usage = TokenUsage(input_tokens=3_000_000, last_turn_input_tokens=1_000_000)
        pct = estimate_context_usage(usage, "claude")
        assert pct == 1.0

    def test_last_turn_over_context_capped(self) -> None:
        usage = TokenUsage(input_tokens=5_000_000, last_turn_input_tokens=2_000_000)
        pct = estimate_context_usage(usage, "claude")
        assert pct == 1.0

    def test_falls_back_to_cumulative_when_no_last_turn(self) -> None:
        usage = TokenUsage(input_tokens=500_000)
        pct = estimate_context_usage(usage, "claude")
        assert 0.49 < pct < 0.51

    def test_codex_smaller_window(self) -> None:
        usage = TokenUsage(last_turn_input_tokens=100_000)
        pct = estimate_context_usage(usage, "codex")
        assert 0.49 < pct < 0.51

    def test_unknown_provider_uses_claude_default(self) -> None:
        usage = TokenUsage(last_turn_input_tokens=500_000)
        pct = estimate_context_usage(usage, "unknown_provider")
        assert 0.49 < pct < 0.51

    def test_last_turn_preferred_over_cumulative(self) -> None:
        """Cumulative input_tokens is 5M (would be 500% of window).
        Last turn is 200k (20% of window). Should use last turn."""
        usage = TokenUsage(input_tokens=5_000_000, last_turn_input_tokens=200_000)
        pct = estimate_context_usage(usage, "claude")
        assert 0.19 < pct < 0.21


class TestWorkerContextPct:
    def test_default_zero(self) -> None:
        w = Worker(name="api", path="/tmp/api")
        assert w.context_pct == 0.0

    def test_in_api_dict(self) -> None:
        w = Worker(name="api", path="/tmp/api")
        w.context_pct = 0.75
        d = w.to_api_dict()
        assert d["context_pct"] == 0.75


class TestContextPressureNotification:
    def test_emit_context_pressure_warning(self) -> None:
        bus = NotificationBus(debounce_seconds=0)
        received = []
        bus.add_backend(received.append)

        bus.emit_context_pressure("api", 0.72, "warning")
        assert len(received) == 1
        assert received[0].event_type == EventType.CONTEXT_PRESSURE
        assert received[0].severity == Severity.WARNING
        assert "72%" in received[0].message

    def test_emit_context_pressure_critical(self) -> None:
        bus = NotificationBus(debounce_seconds=0)
        received = []
        bus.add_backend(received.append)

        bus.emit_context_pressure("api", 0.95, "critical")
        assert len(received) == 1
        assert received[0].severity == Severity.URGENT


class TestContextErrorCompactGuard:
    """Regression tests for the six-/compact-in-queue bug.

    Prior to the fix, ``_check_context_error`` would re-queue a
    ``/compact`` deferred action on every poll while the error text
    remained in the worker's scrollback. Once Claude Code switched
    its native auto mode on the worker would queue up 6+ ``/compact``
    commands in its pending-message buffer before executing any of
    them. The guard here is ``worker.compacting`` — if a compact is
    already in flight, skip re-queueing.
    """

    def _make_tracker(self) -> tuple[WorkerStateTracker, MagicMock]:
        """Minimal tracker with a mocked decision_executor."""
        tracker = WorkerStateTracker.__new__(WorkerStateTracker)
        tracker.workers = []
        tracker.log = DroneLog()
        tracker.task_board = None
        tracker.drone_config = DroneConfig()
        tracker._get_provider = MagicMock()
        tracker._emit = MagicMock()
        de = MagicMock()
        de._deferred_actions = []
        tracker._decision_executor = de
        tracker._rate_limit_seen = {}
        return tracker, de

    def _buzzing_worker(self, name: str = "api") -> Worker:
        w = Worker(name=name, path="/tmp")
        w.state = WorkerState.BUZZING
        return w

    def test_tier1_compact_sets_compacting_flag(self) -> None:
        tracker, de = self._make_tracker()
        w = self._buzzing_worker()
        tracker._check_context_error(w, "Error: prompt is too long, retry later")
        assert w.compacting is True
        assert w.recovery_attempts == 1
        assert len(de._deferred_actions) == 1
        assert de._deferred_actions[0][0] == "compact"

    def test_second_poll_does_not_requeue_while_compacting(self) -> None:
        """The bug: on poll 2 (same error still in scrollback, worker
        still BUZZING, compacting flag still True) we must NOT queue
        another compact."""
        tracker, de = self._make_tracker()
        w = self._buzzing_worker()
        tracker._check_context_error(w, "Error: prompt is too long")
        assert len(de._deferred_actions) == 1

        # Poll 2 — same conditions
        tracker._check_context_error(w, "Error: prompt is too long")
        assert len(de._deferred_actions) == 1  # still one, not two
        assert w.recovery_attempts == 1  # didn't advance to 2

    def test_recovery_resumes_after_compacting_clears(self) -> None:
        """When the in-flight compact completes (``compacting = False``)
        but the error is still showing, the tier-2 revive path should
        still be reachable — the guard only prevents double-queue."""
        tracker, de = self._make_tracker()
        w = self._buzzing_worker()
        tracker._check_context_error(w, "Error: prompt is too long")
        w.compacting = False  # simulate PostCompact hook clearing it

        tracker._check_context_error(w, "Error: prompt is too long")
        # recovery_attempts should now advance to 2 → queues "revive"
        assert w.recovery_attempts == 2
        actions = [a[0] for a in de._deferred_actions]
        assert actions == ["compact", "revive"]

    def test_bare_context_window_phrase_no_longer_matches(self) -> None:
        """``"context window"`` on its own (a common English phrase in
        LLM chats) used to trigger tier-1 recovery. The tightened regex
        now requires the full error shapes Claude Code actually emits."""
        tracker, de = self._make_tracker()
        w = self._buzzing_worker()
        tracker._check_context_error(w, "The worker was discussing the Claude context window size")
        assert w.compacting is False
        assert w.recovery_attempts == 0
        assert de._deferred_actions == []

    def test_full_error_shapes_still_match(self) -> None:
        """Make sure the tightened regex still fires on the real
        errors: "prompt is too long", "context window exceeded",
        "maximum context length", "token limit exceeded"."""
        for err in (
            "Error: prompt is too long, please retry",
            "context window exceeded — please compact",
            "context window is full",
            "context window limit reached",
            "maximum context length of 200000 tokens",
            "token limit exceeded for this request",
        ):
            tracker, de = self._make_tracker()
            w = self._buzzing_worker()
            tracker._check_context_error(w, err)
            assert w.compacting is True, f"regex should match: {err!r}"
            assert len(de._deferred_actions) == 1, f"should queue compact for: {err!r}"
