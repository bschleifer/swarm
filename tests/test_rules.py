"""Tests for buzz/rules.py — decision logic."""

import time

from swarm.buzz.rules import BuzzDecision, Decision, decide
from swarm.config import BuzzConfig
from swarm.worker.worker import Worker, WorkerState


import pytest


def _make_worker(
    name: str = "test",
    state: WorkerState = WorkerState.RESTING,
    pane_id: str = "%99",
    resting_since: float | None = None,
) -> Worker:
    w = Worker(name=name, path="/tmp", pane_id=pane_id, state=state)
    if resting_since is not None:
        w.state_since = resting_since
    return w


@pytest.fixture
def escalated():
    """Provide a fresh escalated set for each test."""
    return set()


class TestDecideStung:
    def test_stung_worker_gets_revived(self, escalated):
        w = _make_worker(state=WorkerState.STUNG)
        d = decide(w, "$ ", escalated=escalated)
        assert d.decision == Decision.REVIVE
        assert "exited" in d.reason

    def test_stung_clears_escalation(self, escalated):
        escalated.add("%99")
        w = _make_worker(state=WorkerState.STUNG)
        decide(w, "$ ", escalated=escalated)
        assert "%99" not in escalated


class TestDecideBuzzing:
    def test_buzzing_worker_does_nothing(self, escalated):
        w = _make_worker(state=WorkerState.BUZZING)
        d = decide(w, "esc to interrupt", escalated=escalated)
        assert d.decision == Decision.NONE
        assert "working" in d.reason


class TestDecideResting:
    def test_choice_prompt_continues(self, escalated):
        w = _make_worker(state=WorkerState.RESTING)
        content = """> 1. Always allow
  2. Yes
  3. No
Enter to select · ↑/↓ to navigate"""
        d = decide(w, content, escalated=escalated)
        assert d.decision == Decision.CONTINUE
        assert "choice" in d.reason

    def test_empty_prompt_continues(self, escalated):
        w = _make_worker(state=WorkerState.RESTING)
        d = decide(w, "> ", escalated=escalated)
        assert d.decision == Decision.CONTINUE
        assert "empty prompt" in d.reason

    def test_idle_prompt_does_nothing(self, escalated):
        w = _make_worker(state=WorkerState.RESTING)
        d = decide(w, '> Try "how does auth work"\n? for shortcuts', escalated=escalated)
        assert d.decision == Decision.NONE
        assert "idle" in d.reason

    def test_unknown_state_escalates_after_threshold(self, escalated):
        w = _make_worker(
            state=WorkerState.RESTING,
            resting_since=time.time() - 20,
        )
        d = decide(w, "some unknown content without prompts", escalated=escalated)
        assert d.decision == Decision.ESCALATE

    def test_unknown_state_waits_before_threshold(self, escalated):
        w = _make_worker(
            state=WorkerState.RESTING,
            resting_since=time.time() - 5,
        )
        d = decide(w, "some unknown content without prompts", escalated=escalated)
        assert d.decision == Decision.NONE

    def test_escalation_only_fires_once(self, escalated):
        w = _make_worker(
            state=WorkerState.RESTING,
            resting_since=time.time() - 20,
        )
        d1 = decide(w, "unknown state", escalated=escalated)
        assert d1.decision == Decision.ESCALATE
        d2 = decide(w, "unknown state", escalated=escalated)
        assert d2.decision == Decision.NONE


class TestReviveLimits:
    def test_stung_escalates_after_max_revives(self, escalated):
        cfg = BuzzConfig(max_revive_attempts=3)
        w = _make_worker(state=WorkerState.STUNG)
        w.revive_count = 3
        d = decide(w, "$ ", config=cfg, escalated=escalated)
        assert d.decision == Decision.ESCALATE
        assert "crash loop" in d.reason

    def test_stung_revives_when_under_limit(self, escalated):
        cfg = BuzzConfig(max_revive_attempts=3)
        w = _make_worker(state=WorkerState.STUNG)
        w.revive_count = 2
        d = decide(w, "$ ", config=cfg, escalated=escalated)
        assert d.decision == Decision.REVIVE

    def test_revive_count_resets_on_buzzing(self):
        w = _make_worker(state=WorkerState.STUNG)
        w.revive_count = 2
        # Transition to BUZZING resets count
        w.update_state(WorkerState.BUZZING)
        assert w.revive_count == 0


class TestDecideWithConfig:
    def test_custom_escalation_threshold(self, escalated):
        cfg = BuzzConfig(escalation_threshold=60.0)
        w = _make_worker(
            state=WorkerState.RESTING,
            resting_since=time.time() - 20,
        )
        d = decide(w, "some unknown content", config=cfg, escalated=escalated)
        # 20s < 60s threshold, should NOT escalate
        assert d.decision == Decision.NONE

    def test_low_escalation_threshold(self, escalated):
        cfg = BuzzConfig(escalation_threshold=2.0)
        w = _make_worker(
            state=WorkerState.RESTING,
            resting_since=time.time() - 5,
        )
        d = decide(w, "some unknown content", config=cfg, escalated=escalated)
        assert d.decision == Decision.ESCALATE
