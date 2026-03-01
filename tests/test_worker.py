"""Tests for worker/worker.py — Worker dataclass and state transitions."""

import time

import pytest

from swarm.worker.worker import (
    SLEEPING_THRESHOLD,
    TokenUsage,
    Worker,
    WorkerState,
    format_duration,
    worker_state_counts,
)


class TestWorkerState:
    def test_indicator_values(self):
        assert WorkerState.BUZZING.indicator == "."
        assert WorkerState.WAITING.indicator == "?"
        assert WorkerState.RESTING.indicator == "~"
        assert WorkerState.SLEEPING.indicator == "z"
        assert WorkerState.STUNG.indicator == "!"

    def test_display_is_lowercase(self):
        assert WorkerState.BUZZING.display == "buzzing"
        assert WorkerState.WAITING.display == "waiting"
        assert WorkerState.RESTING.display == "resting"
        assert WorkerState.SLEEPING.display == "sleeping"
        assert WorkerState.STUNG.display == "stung"


class TestWorkerUpdateState:
    def test_buzzing_to_stung_requires_two_confirmations(self):
        """STUNG needs 2 consecutive readings to prevent spurious revives.

        Regression: Claude Code briefly exits between operations, making the
        shell the foreground process for one poll cycle. Without debounce, the
        drone immediately sends 'claude --continue' into an active session.
        """
        w = Worker(name="t", path="/tmp")
        assert w.state == WorkerState.BUZZING

        # First STUNG reading — should NOT change
        changed = w.update_state(WorkerState.STUNG)
        assert changed is False
        assert w.state == WorkerState.BUZZING

        # Second consecutive STUNG reading — NOW it changes
        changed = w.update_state(WorkerState.STUNG)
        assert changed is True
        assert w.state == WorkerState.STUNG

    def test_transient_stung_debounced(self):
        """Single STUNG reading followed by BUZZING should NOT trigger STUNG.

        Regression: Claude Code restarts between tool calls, causing a brief
        moment where the foreground process is the shell. The next poll sees
        Claude back, so the STUNG was transient and should be ignored.
        """
        w = Worker(name="t", path="/tmp")

        # One STUNG blip
        w.update_state(WorkerState.STUNG)
        assert w.state == WorkerState.BUZZING

        # Claude is back
        changed = w.update_state(WorkerState.BUZZING)
        assert changed is False  # still BUZZING, no change
        assert w.state == WorkerState.BUZZING

        # Another single STUNG blip — counter was reset, needs 2 again
        changed = w.update_state(WorkerState.STUNG)
        assert changed is False
        assert w.state == WorkerState.BUZZING

    def test_buzzing_to_resting_requires_three_confirmations(self):
        w = Worker(name="t", path="/tmp")

        # First RESTING signal — should NOT change
        changed = w.update_state(WorkerState.RESTING)
        assert changed is False
        assert w.state == WorkerState.BUZZING

        # Second RESTING signal — still not enough
        changed = w.update_state(WorkerState.RESTING)
        assert changed is False
        assert w.state == WorkerState.BUZZING

        # Third RESTING signal — NOW it changes
        changed = w.update_state(WorkerState.RESTING)
        assert changed is True
        assert w.state == WorkerState.RESTING

    def test_resting_to_buzzing_immediate(self):
        w = Worker(name="t", path="/tmp", state=WorkerState.RESTING)
        changed = w.update_state(WorkerState.BUZZING)
        assert changed is True
        assert w.state == WorkerState.BUZZING

    def test_same_state_no_change(self):
        w = Worker(name="t", path="/tmp")
        changed = w.update_state(WorkerState.BUZZING)
        assert changed is False

    def test_state_since_updated_on_change(self):
        w = Worker(name="t", path="/tmp")
        old_since = w.state_since
        time.sleep(0.01)
        w.update_state(WorkerState.STUNG)  # first STUNG — debounced
        w.update_state(WorkerState.STUNG)  # second STUNG — accepted
        assert w.state_since > old_since

    def test_buzzing_to_waiting_single_confirmation(self):
        """WAITING is a strong signal (prompt detected) — transitions immediately."""
        w = Worker(name="t", path="/tmp")

        # First WAITING signal — changes immediately (1 confirmation)
        changed = w.update_state(WorkerState.WAITING)
        assert changed is True
        assert w.state == WorkerState.WAITING

    def test_buzzing_to_resting_still_needs_three(self):
        """RESTING is flicker-prone — still requires 3 confirmations."""
        w = Worker(name="t", path="/tmp")

        changed = w.update_state(WorkerState.RESTING)
        assert changed is False
        changed = w.update_state(WorkerState.RESTING)
        assert changed is False
        changed = w.update_state(WorkerState.RESTING)
        assert changed is True
        assert w.state == WorkerState.RESTING

    def test_hysteresis_resets_on_buzzing(self):
        w = Worker(name="t", path="/tmp")
        # One RESTING signal
        w.update_state(WorkerState.RESTING)
        assert w.state == WorkerState.BUZZING
        # Interrupted by BUZZING
        w.update_state(WorkerState.BUZZING)
        # One RESTING signal again — should NOT change (counter reset)
        changed = w.update_state(WorkerState.RESTING)
        assert changed is False
        assert w.state == WorkerState.BUZZING

    def test_hysteresis_resets_on_idle_to_idle_transition(self):
        """Counter resets on RESTING→WAITING so hysteresis doesn't carry over."""
        w = Worker(name="t", path="/tmp", state=WorkerState.RESTING)
        w._resting_confirmations = 2  # simulate accumulated confirmations
        changed = w.update_state(WorkerState.WAITING)
        assert changed is True
        assert w.state == WorkerState.WAITING
        assert w._resting_confirmations == 0

    def test_hysteresis_resets_on_waiting_to_resting(self):
        """Counter resets on WAITING→RESTING transition."""
        w = Worker(name="t", path="/tmp", state=WorkerState.WAITING)
        w._resting_confirmations = 2
        changed = w.update_state(WorkerState.RESTING)
        assert changed is True
        assert w.state == WorkerState.RESTING
        assert w._resting_confirmations == 0


class TestRestingDuration:
    def test_zero_when_not_resting(self):
        w = Worker(name="t", path="/tmp")
        assert w.resting_duration == 0.0

    def test_positive_when_resting(self):
        w = Worker(
            name="t",
            path="/tmp",
            state=WorkerState.RESTING,
            state_since=time.time() - 10,
        )
        assert w.resting_duration >= 9.0

    def test_positive_when_waiting(self):
        w = Worker(
            name="t",
            path="/tmp",
            state=WorkerState.WAITING,
            state_since=time.time() - 10,
        )
        assert w.resting_duration >= 9.0


class TestDisplayState:
    def test_buzzing_always_buzzing(self):
        w = Worker(name="t", path="/tmp", state=WorkerState.BUZZING)
        assert w.display_state == WorkerState.BUZZING

    def test_resting_below_threshold(self):
        w = Worker(
            name="t",
            path="/tmp",
            state=WorkerState.RESTING,
            state_since=time.time() - 10,
        )
        assert w.display_state == WorkerState.RESTING

    def test_resting_above_threshold_becomes_sleeping(self):
        w = Worker(
            name="t",
            path="/tmp",
            state=WorkerState.RESTING,
            state_since=time.time() - (SLEEPING_THRESHOLD + 10),
        )
        assert w.display_state == WorkerState.SLEEPING

    def test_waiting_never_sleeping(self):
        w = Worker(
            name="t",
            path="/tmp",
            state=WorkerState.WAITING,
            state_since=time.time() - (SLEEPING_THRESHOLD + 10),
        )
        assert w.display_state == WorkerState.WAITING

    def test_stung_never_sleeping(self):
        w = Worker(
            name="t",
            path="/tmp",
            state=WorkerState.STUNG,
            state_since=time.time() - (SLEEPING_THRESHOLD + 10),
        )
        assert w.display_state == WorkerState.STUNG


class TestFormatDuration:
    def test_zero(self):
        assert format_duration(0) == "0s"

    def test_seconds(self):
        assert format_duration(30) == "30s"

    def test_minutes(self):
        assert format_duration(90) == "1m"

    def test_hours(self):
        assert format_duration(3700) == "1h"

    def test_days(self):
        assert format_duration(90000) == "1d"

    def test_negative_clamped(self):
        assert format_duration(-5) == "0s"


class TestTokenUsage:
    def test_total_tokens(self):
        u = TokenUsage(input_tokens=100, output_tokens=50)
        assert u.total_tokens == 150

    def test_add_accumulates(self):
        a = TokenUsage(input_tokens=10, output_tokens=5, cache_read_tokens=100, cost_usd=0.01)
        b = TokenUsage(input_tokens=20, output_tokens=10, cache_creation_tokens=50, cost_usd=0.02)
        a.add(b)
        assert a.input_tokens == 30
        assert a.output_tokens == 15
        assert a.cache_read_tokens == 100
        assert a.cache_creation_tokens == 50
        assert a.cost_usd == pytest.approx(0.03)

    def test_to_dict(self):
        u = TokenUsage(input_tokens=100, output_tokens=50, cost_usd=0.123456)
        d = u.to_dict()
        assert d["input_tokens"] == 100
        assert d["output_tokens"] == 50
        assert d["total_tokens"] == 150
        assert d["cost_usd"] == 0.123456
        assert d["cache_read_tokens"] == 0
        assert d["cache_creation_tokens"] == 0

    def test_default_values(self):
        u = TokenUsage()
        assert u.total_tokens == 0
        assert u.cost_usd == 0.0

    def test_worker_to_api_dict_includes_usage(self):
        w = Worker(name="t", path="/tmp")
        w.usage = TokenUsage(input_tokens=500, output_tokens=100, cost_usd=0.05)
        d = w.to_api_dict()
        assert "usage" in d
        assert d["usage"]["input_tokens"] == 500
        assert d["usage"]["total_tokens"] == 600


class TestWorkerStateCounts:
    def test_includes_sleeping(self):
        workers = [
            Worker(
                name="a",
                path="/tmp",
                state=WorkerState.RESTING,
                state_since=time.time() - (SLEEPING_THRESHOLD + 10),
            ),
            Worker(name="b", path="/tmp", state=WorkerState.BUZZING),
            Worker(name="c", path="/tmp", state=WorkerState.RESTING),
        ]
        counts = worker_state_counts(workers)
        assert counts["sleeping"] == 1
        assert counts["resting"] == 1
        assert counts["buzzing"] == 1
        assert counts["total"] == 3


class TestToApiDictCache:
    """Tests for to_api_dict() caching."""

    def test_caches_result(self):
        """Second call within TTL returns the same object."""
        w = Worker(name="t", path="/tmp")
        d1 = w.to_api_dict()
        d2 = w.to_api_dict()
        assert d1 is d2

    def test_expires_after_ttl(self):
        """Cache expires after _API_DICT_TTL seconds."""
        w = Worker(name="t", path="/tmp")
        d1 = w.to_api_dict()
        # Simulate time passing beyond TTL
        w._api_dict_cache_time -= 2.0
        d2 = w.to_api_dict()
        assert d1 is not d2

    def test_invalidated_on_update_state(self):
        """Cache is cleared when state changes via update_state()."""
        w = Worker(name="t", path="/tmp", state=WorkerState.RESTING)
        d1 = w.to_api_dict()
        assert w._api_dict_cache is not None
        w.update_state(WorkerState.BUZZING)
        assert w._api_dict_cache is None
        d2 = w.to_api_dict()
        assert d1 is not d2

    def test_invalidated_on_force_state(self):
        """Cache is cleared when state changes via force_state()."""
        w = Worker(name="t", path="/tmp")
        w.to_api_dict()
        assert w._api_dict_cache is not None
        w.force_state(WorkerState.STUNG)
        assert w._api_dict_cache is None

    def test_not_invalidated_on_same_state(self):
        """Cache is NOT cleared when update_state returns False."""
        w = Worker(name="t", path="/tmp")
        d1 = w.to_api_dict()
        # Same state — no change
        changed = w.update_state(WorkerState.BUZZING)
        assert changed is False
        assert w._api_dict_cache is d1


class TestConfigurableThresholds:
    """Tests for config-driven state detection thresholds."""

    def test_default_thresholds(self):
        w = Worker(name="t", path="/tmp")
        assert w.buzzing_confirm_count == 3
        assert w.stung_confirm_count == 2
        assert w.revive_grace == 15.0

    def test_custom_buzzing_confirm_count(self):
        """Custom buzzing_confirm_count changes BUZZING→RESTING hysteresis."""
        w = Worker(name="t", path="/tmp", buzzing_confirm_count=5)

        for _ in range(4):
            changed = w.update_state(WorkerState.RESTING)
            assert changed is False
            assert w.state == WorkerState.BUZZING

        # 5th confirmation — NOW it changes
        changed = w.update_state(WorkerState.RESTING)
        assert changed is True
        assert w.state == WorkerState.RESTING

    def test_custom_stung_confirm_count(self):
        """Custom stung_confirm_count changes STUNG hysteresis."""
        w = Worker(name="t", path="/tmp", stung_confirm_count=3)

        # First two STUNG readings — not enough
        w.update_state(WorkerState.STUNG)
        assert w.state == WorkerState.BUZZING
        w.update_state(WorkerState.STUNG)
        assert w.state == WorkerState.BUZZING

        # Third STUNG reading — NOW it changes
        changed = w.update_state(WorkerState.STUNG)
        assert changed is True
        assert w.state == WorkerState.STUNG

    def test_custom_revive_grace(self):
        """Custom revive_grace overrides the grace period after revive."""
        w = Worker(name="t", path="/tmp", revive_grace=0.05)
        w.record_revive()

        # Immediately after revive — STUNG suppressed by grace
        changed = w.update_state(WorkerState.STUNG)
        assert changed is False

        # Wait past the short grace
        time.sleep(0.06)
        w.update_state(WorkerState.STUNG)  # first confirmation
        changed = w.update_state(WorkerState.STUNG)  # second confirmation
        assert changed is True
        assert w.state == WorkerState.STUNG
