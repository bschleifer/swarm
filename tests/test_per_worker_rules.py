"""Tests for per-worker approval rules in drone decision engine."""

from __future__ import annotations

from swarm.config.models import DroneApprovalRule, DroneConfig
from swarm.drones.rules import Decision, _effective_config, decide
from swarm.worker.worker import Worker, WorkerState


def _make_worker(name: str = "api", state: WorkerState = WorkerState.WAITING) -> Worker:
    w = Worker(name=name, path=f"/tmp/{name}")
    w.state = state
    return w


def _choice_content(selected: str = "Always allow") -> str:
    """Create content that the Claude provider recognizes as a choice prompt."""
    return f"""> 1. {selected}
  2. Yes
  3. No
Enter to select · ↑/↓ to navigate"""


class TestEffectiveConfig:
    def test_no_worker_rules_returns_same_config(self) -> None:
        cfg = DroneConfig()
        result = _effective_config(cfg)
        assert result is cfg

    def test_worker_rules_prepended(self) -> None:
        global_rules = [DroneApprovalRule(pattern=r"npm", action="approve")]
        worker_rules = [DroneApprovalRule(pattern=r"docker", action="approve")]
        cfg = DroneConfig(approval_rules=global_rules)
        result = _effective_config(cfg, worker_rules=worker_rules)
        assert len(result.approval_rules) == 2
        assert result.approval_rules[0].pattern == r"docker"
        assert result.approval_rules[1].pattern == r"npm"

    def test_preserves_other_config(self) -> None:
        cfg = DroneConfig(poll_interval=10.0, max_revive_attempts=5)
        worker_rules = [DroneApprovalRule(pattern=r"test", action="approve")]
        result = _effective_config(cfg, worker_rules=worker_rules)
        assert result.poll_interval == 10.0
        assert result.max_revive_attempts == 5


class TestDecideWithWorkerRules:
    def test_worker_rule_approves_before_global_escalate(self) -> None:
        """Worker-level approve rule should win over global escalate default."""
        worker_rules = [DroneApprovalRule(pattern=r"Always allow", action="approve")]
        cfg = DroneConfig()  # no global rules → default escalate
        worker = _make_worker()

        decision = decide(
            worker,
            _choice_content(),
            cfg,
            escalated={},
            worker_rules=worker_rules,
        )
        assert decision.decision == Decision.CONTINUE

    def test_worker_rule_escalates_overrides_global_approve(self) -> None:
        """Worker-level escalate rule should override a global approve rule."""
        global_rules = [DroneApprovalRule(pattern=r"Always", action="approve")]
        worker_rules = [DroneApprovalRule(pattern=r"delete", action="escalate")]
        cfg = DroneConfig(approval_rules=global_rules)
        worker = _make_worker()

        decision = decide(
            worker,
            _choice_content("delete old files"),
            cfg,
            escalated={},
            worker_rules=worker_rules,
        )
        assert decision.decision == Decision.ESCALATE

    def test_no_worker_rules_falls_back_to_global(self) -> None:
        """Without worker rules, global rules apply normally."""
        global_rules = [DroneApprovalRule(pattern=r"Always allow", action="approve")]
        cfg = DroneConfig(approval_rules=global_rules)
        worker = _make_worker()

        decision = decide(
            worker,
            _choice_content(),
            cfg,
            escalated={},
            worker_rules=None,
        )
        assert decision.decision == Decision.CONTINUE
