"""Integration tests for test mode — verify wiring without real PTY processes."""

from __future__ import annotations

from swarm.config import DroneApprovalRule, DroneConfig
from swarm.drones.rules import Decision, DroneDecision, decide
from swarm.worker.worker import WorkerState

from tests.conftest import make_worker


class TestDroneDecisionEnrichment:
    """Verify DroneDecision carries rule_pattern/rule_index."""

    def test_decision_has_rule_fields(self):
        d = DroneDecision(Decision.CONTINUE, "test")
        assert d.rule_pattern == ""
        assert d.rule_index == -1

    def test_decision_with_rule_match(self):
        d = DroneDecision(Decision.CONTINUE, "test", rule_pattern="Read", rule_index=0)
        assert d.rule_pattern == "Read"
        assert d.rule_index == 0

    def test_approval_rule_populates_fields(self):
        """When a rule matches, the decision should carry the pattern/index."""
        config = DroneConfig(
            approval_rules=[
                DroneApprovalRule(pattern=r"Write\(", action="approve"),
                DroneApprovalRule(pattern=r"Bash command", action="escalate"),
            ]
        )
        w = make_worker(state=WorkerState.WAITING)
        content = """> 1. Always allow
  2. Yes
  3. No
Write(/tmp/foo) — allow this tool?
Enter to select · ↑/↓ to navigate"""
        esc: set[str] = set()
        d = decide(w, content, config, escalated=esc)
        assert d.decision == Decision.CONTINUE
        assert d.rule_pattern == r"Write\("
        assert d.rule_index == 0

    def test_escalate_rule_populates_fields(self):
        config = DroneConfig(
            approval_rules=[
                DroneApprovalRule(pattern=r"Bash command", action="escalate"),
            ]
        )
        w = make_worker(state=WorkerState.WAITING)
        content = """> 1. Always allow
  2. Yes
  3. No
Bash command: ls -la /tmp/
Enter to select · ↑/↓ to navigate"""
        esc: set[str] = set()
        d = decide(w, content, config, escalated=esc)
        assert d.decision == Decision.ESCALATE
        assert d.rule_pattern == r"Bash command"
        assert d.rule_index == 0

    def test_no_rule_match_empty_fields(self):
        """Decisions without rule matches should have empty rule fields."""
        w = make_worker(state=WorkerState.BUZZING)
        d = decide(w, "esc to interrupt", escalated=set())
        assert d.decision == Decision.NONE
        assert d.rule_pattern == ""
        assert d.rule_index == -1


class TestProposalHook:
    """Verify ProposalManager._on_new_proposal hook works."""

    def test_hook_field_exists(self):
        from unittest.mock import MagicMock

        from swarm.server.proposals import ProposalManager
        from swarm.tasks.proposal import ProposalStore

        store = ProposalStore()
        daemon = MagicMock()
        mgr = ProposalManager(store, daemon)
        assert mgr._on_new_proposal is None


class TestPilotEmitDecisions:
    """Verify pilot._emit_decisions flag exists."""

    def test_flag_defaults_false(self):
        from swarm.drones.log import DroneLog
        from swarm.drones.pilot import DronePilot

        pilot = DronePilot([], DroneLog())
        assert pilot._emit_decisions is False
