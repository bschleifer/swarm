"""Tests for drones/rules.py — decision logic."""

import time

from swarm.drones.rules import Decision, decide
from swarm.config import DroneConfig
from swarm.worker.worker import WorkerState

from tests.conftest import make_worker as _make_worker

import pytest


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
        escalated.add("%api")
        w = _make_worker(state=WorkerState.STUNG)
        decide(w, "$ ", escalated=escalated)
        assert "%api" not in escalated


class TestDecideBuzzing:
    def test_buzzing_worker_does_nothing(self, escalated):
        w = _make_worker(state=WorkerState.BUZZING)
        d = decide(w, "esc to interrupt", escalated=escalated)
        assert d.decision == Decision.NONE
        assert "working" in d.reason


class TestDecideResting:
    def test_choice_prompt_continues(self, escalated):
        w = _make_worker(state=WorkerState.WAITING)
        content = """> 1. Always allow
  2. Yes
  3. No
Enter to select · ↑/↓ to navigate"""
        d = decide(w, content, escalated=escalated)
        assert d.decision == Decision.CONTINUE
        assert "choice" in d.reason

    def test_user_question_escalates(self, escalated):
        """AskUserQuestion prompts must escalate — never auto-continue."""
        w = _make_worker(state=WorkerState.WAITING)
        content = """\
How would you like to proceed?
> 1. Fix both issues
  2. File issues for later
  3. Done for now
  4. Type something.

  5. Chat about this
Enter to select · ↑/↓ to navigate · Esc to cancel"""
        d = decide(w, content, escalated=escalated)
        assert d.decision == Decision.ESCALATE
        assert "user question" in d.reason

    def test_user_question_only_fires_once(self, escalated):
        """User question escalation should not spam."""
        w = _make_worker(state=WorkerState.WAITING)
        content = """\
Which approach?
> 1. Option A
  2. Option B
  3. Type something.
Enter to select"""
        d1 = decide(w, content, escalated=escalated)
        assert d1.decision == Decision.ESCALATE
        d2 = decide(w, content, escalated=escalated)
        assert d2.decision == Decision.NONE

    def test_empty_prompt_continues(self, escalated):
        w = _make_worker(state=WorkerState.WAITING)
        d = decide(w, "> ", escalated=escalated)
        assert d.decision == Decision.CONTINUE
        assert "empty prompt" in d.reason

    def test_idle_prompt_does_nothing(self, escalated):
        w = _make_worker(state=WorkerState.RESTING)
        d = decide(w, '> Try "how does auth work"\n? for shortcuts', escalated=escalated)
        assert d.decision == Decision.NONE
        assert "idle" in d.reason

    def test_waiting_worker_goes_through_decide_resting(self, escalated):
        """WAITING workers should be handled by _decide_resting, same as RESTING."""
        w = _make_worker(state=WorkerState.WAITING)
        content = """> 1. Yes
  2. No
Enter to select"""
        d = decide(w, content, escalated=escalated)
        assert d.decision == Decision.CONTINUE

    def test_unknown_state_escalates_after_threshold(self, escalated):
        w = _make_worker(
            state=WorkerState.WAITING,
            resting_since=time.time() - 20,
        )
        d = decide(w, "some unknown content without prompts", escalated=escalated)
        assert d.decision == Decision.ESCALATE

    def test_unknown_state_waits_before_threshold(self, escalated):
        w = _make_worker(
            state=WorkerState.WAITING,
            resting_since=time.time() - 5,
        )
        d = decide(w, "some unknown content without prompts", escalated=escalated)
        assert d.decision == Decision.NONE

    def test_escalation_only_fires_once(self, escalated):
        w = _make_worker(
            state=WorkerState.WAITING,
            resting_since=time.time() - 20,
        )
        d1 = decide(w, "unknown state", escalated=escalated)
        assert d1.decision == Decision.ESCALATE
        d2 = decide(w, "unknown state", escalated=escalated)
        assert d2.decision == Decision.NONE


class TestReviveLimits:
    def test_stung_escalates_after_max_revives(self, escalated):
        cfg = DroneConfig(max_revive_attempts=3)
        w = _make_worker(state=WorkerState.STUNG)
        w.revive_count = 3
        d = decide(w, "$ ", config=cfg, escalated=escalated)
        assert d.decision == Decision.ESCALATE
        assert "crash loop" in d.reason

    def test_stung_revives_when_under_limit(self, escalated):
        cfg = DroneConfig(max_revive_attempts=3)
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
        cfg = DroneConfig(escalation_threshold=60.0)
        w = _make_worker(
            state=WorkerState.WAITING,
            resting_since=time.time() - 20,
        )
        d = decide(w, "some unknown content", config=cfg, escalated=escalated)
        # 20s < 60s threshold, should NOT escalate
        assert d.decision == Decision.NONE

    def test_low_escalation_threshold(self, escalated):
        cfg = DroneConfig(escalation_threshold=2.0)
        w = _make_worker(
            state=WorkerState.WAITING,
            resting_since=time.time() - 5,
        )
        d = decide(w, "some unknown content", config=cfg, escalated=escalated)
        assert d.decision == Decision.ESCALATE


class TestApprovalRules:
    """Approval rules on choice menu prompts."""

    def _choice_content(self, selected: str = "Always allow") -> str:
        return f"""> 1. {selected}
  2. Yes
  3. No
Enter to select · ↑/↓ to navigate"""

    def test_approve_rule_matches(self, escalated):
        from swarm.config import DroneApprovalRule

        cfg = DroneConfig(approval_rules=[DroneApprovalRule("Always allow", "approve")])
        w = _make_worker(state=WorkerState.WAITING)
        d = decide(w, self._choice_content(), config=cfg, escalated=escalated)
        assert d.decision == Decision.CONTINUE

    def test_escalate_rule_matches(self, escalated):
        from swarm.config import DroneApprovalRule

        cfg = DroneConfig(approval_rules=[DroneApprovalRule("delete|remove", "escalate")])
        w = _make_worker(state=WorkerState.WAITING)
        content = self._choice_content("delete old files")
        d = decide(w, content, config=cfg, escalated=escalated)
        assert d.decision == Decision.ESCALATE
        assert "choice requires approval" in d.reason

    def test_first_match_wins(self, escalated):
        from swarm.config import DroneApprovalRule

        cfg = DroneConfig(
            approval_rules=[
                DroneApprovalRule("Always", "approve"),
                DroneApprovalRule("Always", "escalate"),
            ]
        )
        w = _make_worker(state=WorkerState.WAITING)
        d = decide(w, self._choice_content(), config=cfg, escalated=escalated)
        assert d.decision == Decision.CONTINUE  # first rule wins

    def test_no_rules_legacy_continue(self, escalated):
        cfg = DroneConfig(approval_rules=[])
        w = _make_worker(state=WorkerState.WAITING)
        d = decide(w, self._choice_content(), config=cfg, escalated=escalated)
        assert d.decision == Decision.CONTINUE

    def test_case_insensitive(self, escalated):
        from swarm.config import DroneApprovalRule

        cfg = DroneConfig(approval_rules=[DroneApprovalRule("always allow", "escalate")])
        w = _make_worker(state=WorkerState.WAITING)
        d = decide(w, self._choice_content("Always Allow"), config=cfg, escalated=escalated)
        assert d.decision == Decision.ESCALATE

    def test_escalate_rule_only_fires_once(self, escalated):
        """Choice-menu escalation should not spam — escalate once, then NONE."""
        from swarm.config import DroneApprovalRule

        cfg = DroneConfig(approval_rules=[DroneApprovalRule("delete", "escalate")])
        w = _make_worker(state=WorkerState.WAITING)
        content = self._choice_content("delete old files")
        d1 = decide(w, content, config=cfg, escalated=escalated)
        assert d1.decision == Decision.ESCALATE
        d2 = decide(w, content, config=cfg, escalated=escalated)
        assert d2.decision == Decision.NONE
        assert "already escalated" in d2.reason


class TestPlanEscalation:
    """Plan approval prompts always escalate — never auto-approve."""

    def _plan_content(self) -> str:
        return """Here is my plan for implementing the feature:

## Plan
1. Create the new module
2. Add tests
3. Update docs

Do you want me to proceed with this plan?
> 1. Yes, proceed
  2. No, revise
  3. Cancel
Enter to select"""

    def test_plan_prompt_always_escalates(self, escalated):
        w = _make_worker(state=WorkerState.WAITING)
        d = decide(w, self._plan_content(), escalated=escalated)
        assert d.decision == Decision.ESCALATE
        assert "plan" in d.reason.lower()

    def test_plan_escalation_only_fires_once(self, escalated):
        """Plan escalation should not spam — escalate once, then NONE until worker resumes."""
        w = _make_worker(state=WorkerState.WAITING)
        d1 = decide(w, self._plan_content(), escalated=escalated)
        assert d1.decision == Decision.ESCALATE
        d2 = decide(w, self._plan_content(), escalated=escalated)
        assert d2.decision == Decision.NONE
        assert "already escalated" in d2.reason

    def test_plan_escalation_resets_after_buzzing(self, escalated):
        """After worker goes back to BUZZING, next plan prompt re-escalates."""
        w = _make_worker(state=WorkerState.WAITING)
        d1 = decide(w, self._plan_content(), escalated=escalated)
        assert d1.decision == Decision.ESCALATE
        # Worker resumes working — BUZZING clears the escalated set
        w.state = WorkerState.BUZZING
        decide(w, "esc to interrupt", escalated=escalated)
        # New plan prompt → should escalate again
        w.state = WorkerState.WAITING
        d2 = decide(w, self._plan_content(), escalated=escalated)
        assert d2.decision == Decision.ESCALATE

    def test_plan_escalates_even_with_approve_rules(self, escalated):
        from swarm.config import DroneApprovalRule

        cfg = DroneConfig(approval_rules=[DroneApprovalRule(".*", "approve")])
        w = _make_worker(state=WorkerState.WAITING)
        d = decide(w, self._plan_content(), config=cfg, escalated=escalated)
        assert d.decision == Decision.ESCALATE

    def test_non_plan_choice_not_affected(self, escalated):
        w = _make_worker(state=WorkerState.WAITING)
        content = """> 1. Yes
  2. No
Enter to select"""
        d = decide(w, content, escalated=escalated)
        assert d.decision == Decision.CONTINUE
