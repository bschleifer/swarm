"""Tests for worker/worker.py — Worker dataclass and state transitions."""

import time

from swarm.worker.worker import (
    SLEEPING_THRESHOLD,
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
        w = Worker(name="t", path="/tmp", pane_id="%0")
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
        w = Worker(name="t", path="/tmp", pane_id="%0")

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

    def test_buzzing_to_resting_requires_two_confirmations(self):
        w = Worker(name="t", path="/tmp", pane_id="%0")

        # First RESTING signal — should NOT change
        changed = w.update_state(WorkerState.RESTING)
        assert changed is False
        assert w.state == WorkerState.BUZZING

        # Second RESTING signal — NOW it changes
        changed = w.update_state(WorkerState.RESTING)
        assert changed is True
        assert w.state == WorkerState.RESTING

    def test_resting_to_buzzing_immediate(self):
        w = Worker(name="t", path="/tmp", pane_id="%0", state=WorkerState.RESTING)
        changed = w.update_state(WorkerState.BUZZING)
        assert changed is True
        assert w.state == WorkerState.BUZZING

    def test_same_state_no_change(self):
        w = Worker(name="t", path="/tmp", pane_id="%0")
        changed = w.update_state(WorkerState.BUZZING)
        assert changed is False

    def test_state_since_updated_on_change(self):
        w = Worker(name="t", path="/tmp", pane_id="%0")
        old_since = w.state_since
        time.sleep(0.01)
        w.update_state(WorkerState.STUNG)  # first STUNG — debounced
        w.update_state(WorkerState.STUNG)  # second STUNG — accepted
        assert w.state_since > old_since

    def test_buzzing_to_waiting_requires_two_confirmations(self):
        w = Worker(name="t", path="/tmp", pane_id="%0")

        # First WAITING signal — should NOT change
        changed = w.update_state(WorkerState.WAITING)
        assert changed is False
        assert w.state == WorkerState.BUZZING

        # Second WAITING signal — NOW it changes
        changed = w.update_state(WorkerState.WAITING)
        assert changed is True
        assert w.state == WorkerState.WAITING

    def test_hysteresis_resets_on_buzzing(self):
        w = Worker(name="t", path="/tmp", pane_id="%0")
        # One RESTING signal
        w.update_state(WorkerState.RESTING)
        assert w.state == WorkerState.BUZZING
        # Interrupted by BUZZING
        w.update_state(WorkerState.BUZZING)
        # One RESTING signal again — should NOT change (counter reset)
        changed = w.update_state(WorkerState.RESTING)
        assert changed is False
        assert w.state == WorkerState.BUZZING


class TestRestingDuration:
    def test_zero_when_not_resting(self):
        w = Worker(name="t", path="/tmp", pane_id="%0")
        assert w.resting_duration == 0.0

    def test_positive_when_resting(self):
        w = Worker(
            name="t",
            path="/tmp",
            pane_id="%0",
            state=WorkerState.RESTING,
            state_since=time.time() - 10,
        )
        assert w.resting_duration >= 9.0

    def test_positive_when_waiting(self):
        w = Worker(
            name="t",
            path="/tmp",
            pane_id="%0",
            state=WorkerState.WAITING,
            state_since=time.time() - 10,
        )
        assert w.resting_duration >= 9.0


class TestDisplayState:
    def test_buzzing_always_buzzing(self):
        w = Worker(name="t", path="/tmp", pane_id="%0", state=WorkerState.BUZZING)
        assert w.display_state == WorkerState.BUZZING

    def test_resting_below_threshold(self):
        w = Worker(
            name="t",
            path="/tmp",
            pane_id="%0",
            state=WorkerState.RESTING,
            state_since=time.time() - 10,
        )
        assert w.display_state == WorkerState.RESTING

    def test_resting_above_threshold_becomes_sleeping(self):
        w = Worker(
            name="t",
            path="/tmp",
            pane_id="%0",
            state=WorkerState.RESTING,
            state_since=time.time() - (SLEEPING_THRESHOLD + 10),
        )
        assert w.display_state == WorkerState.SLEEPING

    def test_waiting_never_sleeping(self):
        w = Worker(
            name="t",
            path="/tmp",
            pane_id="%0",
            state=WorkerState.WAITING,
            state_since=time.time() - (SLEEPING_THRESHOLD + 10),
        )
        assert w.display_state == WorkerState.WAITING

    def test_stung_never_sleeping(self):
        w = Worker(
            name="t",
            path="/tmp",
            pane_id="%0",
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


class TestWorkerStateCounts:
    def test_includes_sleeping(self):
        workers = [
            Worker(
                name="a",
                path="/tmp",
                pane_id="%0",
                state=WorkerState.RESTING,
                state_since=time.time() - (SLEEPING_THRESHOLD + 10),
            ),
            Worker(name="b", path="/tmp", pane_id="%1", state=WorkerState.BUZZING),
            Worker(name="c", path="/tmp", pane_id="%2", state=WorkerState.RESTING),
        ]
        counts = worker_state_counts(workers)
        assert counts["sleeping"] == 1
        assert counts["resting"] == 1
        assert counts["buzzing"] == 1
        assert counts["total"] == 3
