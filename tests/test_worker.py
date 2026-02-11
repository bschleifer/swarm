"""Tests for worker/worker.py — Worker dataclass and state transitions."""

import time

from swarm.worker.worker import Worker, WorkerState


class TestWorkerState:
    def test_indicator_values(self):
        assert WorkerState.BUZZING.indicator == "."
        assert WorkerState.WAITING.indicator == "?"
        assert WorkerState.RESTING.indicator == "~"
        assert WorkerState.STUNG.indicator == "!"

    def test_display_is_lowercase(self):
        assert WorkerState.BUZZING.display == "buzzing"
        assert WorkerState.WAITING.display == "waiting"
        assert WorkerState.RESTING.display == "resting"
        assert WorkerState.STUNG.display == "stung"


class TestWorkerUpdateState:
    def test_buzzing_to_stung_immediate(self):
        w = Worker(name="t", path="/tmp", pane_id="%0")
        assert w.state == WorkerState.BUZZING
        changed = w.update_state(WorkerState.STUNG)
        assert changed is True
        assert w.state == WorkerState.STUNG

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
        w.update_state(WorkerState.STUNG)
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
