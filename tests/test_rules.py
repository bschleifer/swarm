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

    def test_stung_preserves_escalation_until_buzzing(self, escalated):
        """STUNG should NOT clear escalation — it clears when worker goes BUZZING."""
        escalated.add("%api")
        w = _make_worker(state=WorkerState.STUNG)
        decide(w, "$ ", escalated=escalated)
        # Escalation stays until worker recovers to BUZZING
        assert "%api" in escalated

    def test_buzzing_clears_escalation_after_stung(self, escalated):
        """After STUNG → revive → BUZZING, escalation should be cleared."""
        escalated.add("%api")
        w = _make_worker(state=WorkerState.STUNG)
        decide(w, "$ ", escalated=escalated)
        assert "%api" in escalated  # still set during STUNG
        w.state = WorkerState.BUZZING
        decide(w, "esc to interrupt", escalated=escalated)
        assert "%api" not in escalated  # cleared by BUZZING


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

    def test_crash_loop_escalation_fires_only_once(self, escalated):
        """Regression: STUNG with exhausted revives should escalate once, then NONE.

        Previously, _esc.discard() at the top of the STUNG branch undid
        the _esc.add() from the previous cycle, causing infinite re-escalation.
        """
        cfg = DroneConfig(max_revive_attempts=3)
        w = _make_worker(state=WorkerState.STUNG)
        w.revive_count = 3
        d1 = decide(w, "$ ", config=cfg, escalated=escalated)
        assert d1.decision == Decision.ESCALATE
        assert "crash loop" in d1.reason
        # Second call should return NONE (already escalated)
        d2 = decide(w, "$ ", config=cfg, escalated=escalated)
        assert d2.decision == Decision.NONE
        assert "already escalated" in d2.reason
        # Third call — still NONE (no spam)
        d3 = decide(w, "$ ", config=cfg, escalated=escalated)
        assert d3.decision == Decision.NONE

    def test_revive_count_resets_on_buzzing(self):
        w = _make_worker(state=WorkerState.STUNG)
        w.revive_count = 2
        # Transition to BUZZING resets count
        w.update_state(WorkerState.BUZZING)
        assert w.revive_count == 0

    def test_revive_grace_blocks_stung(self):
        """After revive, STUNG readings are ignored for the grace period."""
        w = _make_worker(state=WorkerState.BUZZING)
        w.record_revive()  # sets _revive_at to now
        # Poll detects shell (STUNG) right after revive — should be ignored
        changed = w.update_state(WorkerState.STUNG)
        assert not changed
        assert w.state == WorkerState.BUZZING

    def test_revive_grace_expires(self):
        """After the grace period, STUNG readings are accepted again."""
        w = _make_worker(state=WorkerState.BUZZING)
        w.record_revive()
        # Simulate grace period expiring
        w._revive_at -= w._REVIVE_GRACE + 1
        changed = w.update_state(WorkerState.STUNG)
        assert changed
        assert w.state == WorkerState.STUNG

    def test_revive_grace_allows_non_stung(self):
        """Grace period only blocks STUNG — other transitions still work."""
        w = _make_worker(state=WorkerState.BUZZING)
        w.record_revive()
        # RESTING requires 2 confirmations, so first returns False
        w.update_state(WorkerState.RESTING)
        changed = w.update_state(WorkerState.RESTING)
        assert changed
        assert w.state == WorkerState.RESTING


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

    def test_plan_word_in_conversation_does_not_trigger_plan_escalation(self, escalated):
        """Regression: 'plan' in worker output should not cause plan escalation.

        When a worker implementing a plan shows a permission prompt (e.g., grep),
        the drone should treat it as a regular choice, not a plan approval prompt.
        The escalation reason should NOT contain 'plan requires user approval'.
        """
        w = _make_worker(state=WorkerState.WAITING)
        content = """Phase 1 of the plan is complete.
The plan was already approved by the operator.
Now executing the approved plan for type safety fixes.

Grep command
  grep -r "list\\[dict\\]" src/
> 1. Allow
  2. Allow always
  3. Deny
Enter to select"""
        d = decide(w, content, escalated=escalated)
        # Should be treated as a regular choice, NOT a plan escalation
        assert d.decision == Decision.CONTINUE
        assert "plan requires" not in d.reason.lower()


class TestSafetyPatterns:
    """Built-in safety patterns escalate destructive operations."""

    def test_drop_table_escalates(self, escalated):
        from swarm.config import DroneApprovalRule

        cfg = DroneConfig(approval_rules=[DroneApprovalRule("Bash", "approve")])
        w = _make_worker(state=WorkerState.WAITING)
        content = """Bash command
  psql -c "DROP TABLE users;"
Do you want to proceed?
> 1. Yes
  2. Yes, and don't ask again
  3. No
Esc to cancel"""
        d = decide(w, content, config=cfg, escalated=escalated)
        assert d.decision == Decision.ESCALATE

    def test_truncate_escalates(self, escalated):
        from swarm.config import DroneApprovalRule

        cfg = DroneConfig(approval_rules=[DroneApprovalRule("Bash", "approve")])
        w = _make_worker(state=WorkerState.WAITING)
        content = """Bash command
  psql -c "TRUNCATE nexus_call_log;"
Do you want to proceed?
> 1. Yes
  2. No
Esc to cancel"""
        d = decide(w, content, config=cfg, escalated=escalated)
        assert d.decision == Decision.ESCALATE

    def test_safe_select_on_production_db_approves(self, escalated):
        """SELECT queries on production databases should NOT be blocked."""
        from swarm.config import DroneApprovalRule

        cfg = DroneConfig(approval_rules=[DroneApprovalRule("Bash", "approve")])
        w = _make_worker(state=WorkerState.WAITING)
        content = """Bash command
  PGPASSWORD='secret' psql -h db.postgres.database.azure.com \
  -U admin -d v6_production -c "
  SELECT id, \"stagingRecordId\", \"callType\"
  FROM nexus_call_log
  WHERE \"stagingRecordId\" = 11525;
  " 2>&1
  Query production DB for call logs

Do you want to proceed?
> 1. Yes
  2. Yes, and don't ask again for PGPASSWORD psql commands
  3. No
Esc to cancel"""
        d = decide(w, content, config=cfg, escalated=escalated)
        assert d.decision == Decision.CONTINUE

    def test_read_uploads_approves(self, escalated):
        """Read from swarm uploads should be approved by tool-name rules."""
        from swarm.config import DroneApprovalRule

        cfg = DroneConfig(approval_rules=[DroneApprovalRule("Read", "approve")])
        w = _make_worker(state=WorkerState.WAITING)
        content = """Read file
  Read(~/.swarm/uploads/09b31b4bcc13_image.png)
Do you want to proceed?
> 1. Yes
  2. Yes, allow reading from uploads/ during this session
  3. No
Esc to cancel"""
        d = decide(w, content, config=cfg, escalated=escalated)
        assert d.decision == Decision.CONTINUE

    def test_allowed_read_path_approves_without_rules(self, escalated):
        """allowed_read_paths auto-approves Read from configured dirs."""
        cfg = DroneConfig(allowed_read_paths=["~/.swarm/uploads/"])
        w = _make_worker(state=WorkerState.WAITING)
        content = """Read file
  Read(~/.swarm/uploads/09b31b4bcc13_image.png)
Do you want to proceed?
> 1. Yes
  2. No
Esc to cancel"""
        d = decide(w, content, config=cfg, escalated=escalated)
        assert d.decision == Decision.CONTINUE
        assert "allowed path" in d.reason

    def test_allowed_read_path_rejects_other_dirs(self, escalated):
        """Read from non-allowed dirs falls through to rules."""
        from swarm.config import DroneApprovalRule

        cfg = DroneConfig(
            allowed_read_paths=["~/.swarm/uploads/"],
            approval_rules=[DroneApprovalRule("Bash", "approve")],
        )
        w = _make_worker(state=WorkerState.WAITING)
        content = """Read file
  Read(/etc/passwd)
Do you want to proceed?
> 1. Yes
  2. No
Esc to cancel"""
        d = decide(w, content, config=cfg, escalated=escalated)
        # No approval rules → falls through to ESCALATE (fail-safe)
        assert d.decision == Decision.ESCALATE

    def test_allowed_read_path_with_absolute_path(self, escalated):
        """allowed_read_paths works with absolute paths too."""
        cfg = DroneConfig(allowed_read_paths=["/home/bschleifer/.swarm/uploads/"])
        w = _make_worker(state=WorkerState.WAITING)
        content = """Read file
  Read(/home/bschleifer/.swarm/uploads/file.txt)
Do you want to proceed?
> 1. Yes
  2. No
Esc to cancel"""
        d = decide(w, content, config=cfg, escalated=escalated)
        assert d.decision == Decision.CONTINUE
        assert "allowed path" in d.reason

    def test_allowed_read_path_blocks_traversal(self, escalated):
        """Path traversal via ../ must NOT bypass allowed_read_paths."""
        from swarm.config import DroneApprovalRule

        cfg = DroneConfig(
            allowed_read_paths=["~/.swarm/uploads/"],
            approval_rules=[DroneApprovalRule("Bash", "approve")],
        )
        w = _make_worker(state=WorkerState.WAITING)
        content = """Read file
  Read(~/.swarm/uploads/../../../etc/passwd)
Do you want to proceed?
> 1. Yes
  2. No
Esc to cancel"""
        d = decide(w, content, config=cfg, escalated=escalated)
        assert d.decision == Decision.ESCALATE

    def test_allowed_read_path_no_prefix_false_positive(self, escalated):
        """uploads/ must not match uploads_evil/ (prefix attack)."""
        from swarm.config import DroneApprovalRule

        cfg = DroneConfig(
            allowed_read_paths=["~/.swarm/uploads"],
            approval_rules=[DroneApprovalRule("Bash", "approve")],
        )
        w = _make_worker(state=WorkerState.WAITING)
        content = """Read file
  Read(~/.swarm/uploads_evil/secret.txt)
Do you want to proceed?
> 1. Yes
  2. No
Esc to cancel"""
        d = decide(w, content, config=cfg, escalated=escalated)
        assert d.decision == Decision.ESCALATE

    def test_allowed_read_uses_last_match(self, escalated):
        """When scrollback has multiple Read()s, only the last one matters."""
        cfg = DroneConfig(allowed_read_paths=["~/.swarm/uploads/"])
        w = _make_worker(state=WorkerState.WAITING)
        # Older Read from a non-allowed path is higher in scrollback;
        # the CURRENT prompt is a Read from uploads — should approve.
        content = """Read file
  Read(/home/user/projects/secret.py)
Do you want to proceed?
> 1. Yes
  2. No
Esc to cancel

... more output ...

Read file
  Read(~/.swarm/uploads/screenshot.png)
Do you want to proceed?
> 1. Yes
  2. No
Esc to cancel"""
        d = decide(w, content, config=cfg, escalated=escalated)
        assert d.decision == Decision.CONTINUE
        assert "allowed path" in d.reason

    def test_allowed_read_old_uploads_does_not_shadow(self, escalated):
        """An old Read from uploads shouldn't auto-approve a new Read from elsewhere."""
        from swarm.config import DroneApprovalRule

        cfg = DroneConfig(
            allowed_read_paths=["~/.swarm/uploads/"],
            approval_rules=[DroneApprovalRule("Bash", "approve")],
        )
        w = _make_worker(state=WorkerState.WAITING)
        # Old Read from uploads higher in scrollback, current Read from /etc/passwd
        content = """Read file
  Read(~/.swarm/uploads/old_image.png)
Do you want to proceed?
> 1. Yes

... more output ...

Read file
  Read(/etc/passwd)
Do you want to proceed?
> 1. Yes
  2. No
Esc to cancel"""
        d = decide(w, content, config=cfg, escalated=escalated)
        assert d.decision == Decision.ESCALATE

    def test_caret_anchor_matches_line_start_multiline(self, escalated):
        """Rules with ^ should match start-of-line, not just start-of-string."""
        from swarm.config import DroneApprovalRule

        cfg = DroneConfig(approval_rules=[DroneApprovalRule("^Do you want to proceed", "approve")])
        w = _make_worker(state=WorkerState.WAITING)
        # "Do you want to proceed" is NOT at position 0 of the string
        content = """Bash command
  git commit -m "fix typo"
Do you want to proceed?
> 1. Yes
  2. No
Esc to cancel"""
        d = decide(w, content, config=cfg, escalated=escalated)
        assert d.decision == Decision.CONTINUE

    def test_rules_match_full_content_not_just_summary(self, escalated):
        """Approval rules must see the full pane content, not just the choice summary."""
        from swarm.config import DroneApprovalRule

        cfg = DroneConfig(approval_rules=[DroneApprovalRule("psql", "approve")])
        w = _make_worker(state=WorkerState.WAITING)
        content = """Bash command
  psql -c "SELECT 1;"
Do you want to proceed?
> 1. Yes
  2. No
Esc to cancel"""
        d = decide(w, content, config=cfg, escalated=escalated)
        assert d.decision == Decision.CONTINUE


class TestBuiltinSafePatterns:
    """Built-in safe patterns auto-approve read-only operations."""

    def test_bash_ls_approves(self, escalated):
        """Bash ls command should be auto-approved as safe operation."""
        w = _make_worker(state=WorkerState.WAITING)
        content = """Bash command
  Bash(ls /tmp/swarm*)
Do you want to proceed?
> 1. Yes
  2. No
Esc to cancel"""
        d = decide(w, content, escalated=escalated)
        assert d.decision == Decision.CONTINUE
        assert "safe operation" in d.reason

    def test_read_tool_goes_through_rules(self, escalated):
        """Read tool should go through allowed_read_paths / approval_rules, not safe patterns."""
        from swarm.config import DroneApprovalRule

        cfg = DroneConfig(approval_rules=[DroneApprovalRule("Read", "approve")])
        w = _make_worker(state=WorkerState.WAITING)
        content = """Read file
  Read(/home/user/projects/readme.md)
Do you want to proceed?
> 1. Yes
  2. No
Esc to cancel"""
        d = decide(w, content, config=cfg, escalated=escalated)
        assert d.decision == Decision.CONTINUE

    def test_glob_tool_approves(self, escalated):
        """Glob tool prompt should be auto-approved as safe operation."""
        w = _make_worker(state=WorkerState.WAITING)
        content = """Glob pattern
  Glob(src/**/*.py)
Do you want to proceed?
> 1. Yes
  2. No
Esc to cancel"""
        d = decide(w, content, escalated=escalated)
        assert d.decision == Decision.CONTINUE
        assert "safe operation" in d.reason

    def test_grep_tool_approves(self, escalated):
        """Grep tool prompt should be auto-approved as safe operation."""
        w = _make_worker(state=WorkerState.WAITING)
        content = """Search content
  Grep(error)
Do you want to proceed?
> 1. Yes
  2. No
Esc to cancel"""
        d = decide(w, content, escalated=escalated)
        assert d.decision == Decision.CONTINUE
        assert "safe operation" in d.reason

    def test_bash_cat_approves(self, escalated):
        """Bash cat (read-only) should be auto-approved."""
        w = _make_worker(state=WorkerState.WAITING)
        content = """Bash command
  Bash(cat /tmp/report.txt)
Do you want to proceed?
> 1. Yes
  2. No
Esc to cancel"""
        d = decide(w, content, escalated=escalated)
        assert d.decision == Decision.CONTINUE
        assert "safe operation" in d.reason

    def test_safe_pattern_blocked_by_destructive(self, escalated):
        """Safe pattern should NOT override destructive safety patterns."""
        from swarm.config import DroneApprovalRule

        cfg = DroneConfig(approval_rules=[DroneApprovalRule("Bash", "approve")])
        w = _make_worker(state=WorkerState.WAITING)
        content = """Bash command
  Bash(rm -rf /tmp/important)
Do you want to proceed?
> 1. Yes
  2. No
Esc to cancel"""
        d = decide(w, content, config=cfg, escalated=escalated)
        assert d.decision == Decision.ESCALATE

    def test_user_question_not_affected_by_safe_patterns(self, escalated):
        """User questions should still escalate even with safe-looking content."""
        w = _make_worker(state=WorkerState.WAITING)
        content = """\
Which file should I Read?
> 1. Read(src/main.py)
  2. Read(src/utils.py)
  3. Type something.
Enter to select · Esc to cancel"""
        d = decide(w, content, escalated=escalated)
        assert d.decision == Decision.ESCALATE
        assert "user question" in d.reason


class TestPushToMainEscalation:
    """git push to main/master always escalates."""

    def test_push_origin_main_escalates(self, escalated):
        from swarm.config import DroneApprovalRule

        cfg = DroneConfig(approval_rules=[DroneApprovalRule("Bash", "approve")])
        w = _make_worker(state=WorkerState.WAITING)
        content = """Bash command
  git push origin main
Do you want to proceed?
> 1. Yes
  2. No
Esc to cancel"""
        d = decide(w, content, config=cfg, escalated=escalated)
        assert d.decision == Decision.ESCALATE

    def test_push_upstream_master_escalates(self, escalated):
        from swarm.config import DroneApprovalRule

        cfg = DroneConfig(approval_rules=[DroneApprovalRule("Bash", "approve")])
        w = _make_worker(state=WorkerState.WAITING)
        content = """Bash command
  git push upstream master
Do you want to proceed?
> 1. Yes
  2. No
Esc to cancel"""
        d = decide(w, content, config=cfg, escalated=escalated)
        assert d.decision == Decision.ESCALATE

    def test_push_feature_branch_approves(self, escalated):
        from swarm.config import DroneApprovalRule

        cfg = DroneConfig(approval_rules=[DroneApprovalRule("Bash", "approve")])
        w = _make_worker(state=WorkerState.WAITING)
        content = """Bash command
  git push origin feature/my-branch
Do you want to proceed?
> 1. Yes
  2. No
Esc to cancel"""
        d = decide(w, content, config=cfg, escalated=escalated)
        assert d.decision == Decision.CONTINUE


class TestScrollbackTrimming:
    """Approval rules and safety patterns only see the last 30 lines."""

    def test_stale_plan_text_does_not_escalate_bash_prompt(self, escalated):
        """Regression: 'plan' in old scrollback should not trigger plan escalation
        on a fresh Bash permission prompt lower in the pane."""
        from swarm.config import DroneApprovalRule

        cfg = DroneConfig(approval_rules=[DroneApprovalRule("Bash", "approve")])
        w = _make_worker(state=WorkerState.WAITING)
        # Build content: old scrollback with "plan" text + 40 blank lines + Bash prompt
        old_scrollback = "Here is the plan for implementing the feature.\n" * 5
        padding = "\n" * 40
        bash_prompt = """Bash command
  uv run ruff check src/
Do you want to proceed?
> 1. Yes
  2. No
Esc to cancel"""
        content = old_scrollback + padding + bash_prompt
        d = decide(w, content, config=cfg, escalated=escalated)
        # The Bash prompt should be approved via the "Bash" approval rule,
        # NOT escalated because of the old "plan" text in scrollback.
        assert d.decision == Decision.CONTINUE

    def test_stale_destructive_text_does_not_escalate_safe_command(self, escalated):
        """Old 'rm -rf' in scrollback should not block a safe 'ls' command."""
        w = _make_worker(state=WorkerState.WAITING)
        old_scrollback = "Previously ran: rm -rf /tmp/old\n" * 3
        padding = "\n" * 40
        safe_prompt = """Bash command
  Bash(ls /tmp/new)
Do you want to proceed?
> 1. Yes
  2. No
Esc to cancel"""
        content = old_scrollback + padding + safe_prompt
        d = decide(w, content, escalated=escalated)
        assert d.decision == Decision.CONTINUE
        assert "safe operation" in d.reason

    def test_recent_destructive_text_still_escalates(self, escalated):
        """Destructive text within the last 30 lines should still escalate."""
        from swarm.config import DroneApprovalRule

        cfg = DroneConfig(approval_rules=[DroneApprovalRule("Bash", "approve")])
        w = _make_worker(state=WorkerState.WAITING)
        content = """Bash command
  rm -rf /var/data
Do you want to proceed?
> 1. Yes
  2. No
Esc to cancel"""
        d = decide(w, content, config=cfg, escalated=escalated)
        assert d.decision == Decision.ESCALATE
