"""Tests for testing/log.py — TestRunLog and TestLogEntry."""

import json

from swarm.testing.config import InfraSnapshot, compute_env_hash
from swarm.testing.log import TestLogEntry, TestRunLog


class TestInfraSnapshot:
    def test_defaults(self):
        snap = InfraSnapshot()
        assert snap.model == ""
        assert snap.worker_count == 0
        assert snap.env_keys == []

    def test_as_dict_roundtrip(self):
        snap = InfraSnapshot(model="claude-opus-4-7", worker_count=3, port=9091)
        d = snap.as_dict()
        assert d["model"] == "claude-opus-4-7"
        assert d["worker_count"] == 3
        assert d["port"] == 9091


class TestComputeEnvHash:
    def test_empty_env_returns_empty_digest(self):
        digest, keys = compute_env_hash({})
        assert digest == ""
        assert keys == []

    def test_tracked_var_produces_stable_digest(self):
        env = {"CLAUDE_MODEL": "claude-opus-4-7"}
        d1, k1 = compute_env_hash(env)
        d2, k2 = compute_env_hash(env)
        assert d1 == d2  # deterministic
        assert k1 == k2 == ["CLAUDE_MODEL"]
        assert len(d1) == 12  # truncated sha256

    def test_different_values_produce_different_digests(self):
        d1, _ = compute_env_hash({"CLAUDE_MODEL": "a"})
        d2, _ = compute_env_hash({"CLAUDE_MODEL": "b"})
        assert d1 != d2

    def test_untracked_vars_ignored(self):
        d1, k1 = compute_env_hash({"FOO": "bar"})
        assert d1 == ""
        assert k1 == []


class TestRunLogInfraHeader:
    def test_run_log_writes_infra_header(self, tmp_path):
        infra = InfraSnapshot(model="claude-opus-4-7", worker_count=2, port=9091)
        TestRunLog("run1", tmp_path, infra=infra)
        log_path = tmp_path / "test-run-run1.jsonl"
        assert log_path.exists()
        first_line = log_path.read_text().splitlines()[0]
        payload = json.loads(first_line)
        assert "infra" in payload
        assert payload["infra"]["model"] == "claude-opus-4-7"
        assert payload["infra"]["worker_count"] == 2

    def test_run_log_default_infra_still_writes_header(self, tmp_path):
        TestRunLog("run2", tmp_path)
        log_path = tmp_path / "test-run-run2.jsonl"
        first_line = log_path.read_text().splitlines()[0]
        payload = json.loads(first_line)
        assert "infra" in payload
        assert payload["infra"]["model"] == ""

    def test_run_log_infra_preserved_for_report(self, tmp_path):
        infra = InfraSnapshot(model="claude-sonnet-4-6", worker_count=1)
        log = TestRunLog("run3", tmp_path, infra=infra)
        assert log.infra.model == "claude-sonnet-4-6"
        assert log.infra.worker_count == 1


class TestTestLogEntry:
    def test_defaults(self):
        entry = TestLogEntry()
        assert entry.event_type == ""
        assert entry.worker_name == ""
        assert entry.rule_index == -1
        assert entry.queen_confidence == -1.0
        assert entry.timestamp > 0

    def test_custom_fields(self):
        entry = TestLogEntry(
            event_type="drone_decision",
            worker_name="api",
            decision="CONTINUE",
            rule_pattern="Read|Write",
            rule_index=2,
        )
        assert entry.event_type == "drone_decision"
        assert entry.worker_name == "api"
        assert entry.rule_index == 2


class TestTestRunLog:
    def test_init_creates_dir(self, tmp_path):
        report_dir = tmp_path / "reports"
        log = TestRunLog("abc123", report_dir)
        assert report_dir.exists()
        assert log.run_id == "abc123"

    def test_record_drone_decision(self, tmp_path):
        log = TestRunLog("test1", tmp_path)
        log.record_drone_decision(
            worker_name="api",
            content="worker output here",
            decision="CONTINUE",
            reason="choice menu",
            rule_pattern="Read",
            rule_index=0,
        )
        assert len(log.entries) == 1
        entry = log.entries[0]
        assert entry.event_type == "drone_decision"
        assert entry.worker_name == "api"
        assert entry.decision == "CONTINUE"
        assert entry.rule_pattern == "Read"

    def test_record_operator_decision(self, tmp_path):
        log = TestRunLog("test2", tmp_path)
        log.record_operator_decision(
            proposal_id="p123",
            proposal_type="assignment",
            worker_name="web",
            approved=True,
            reasoning="looks good",
            confidence=0.9,
            latency_ms=150.5,
        )
        assert len(log.entries) == 1
        entry = log.entries[0]
        assert entry.event_type == "operator_decision"
        assert entry.operator_actor == "simulated-operator"
        assert entry.queen_confidence == 0.9

    def test_record_state_change(self, tmp_path):
        log = TestRunLog("test3", tmp_path)
        log.record_state_change("api", "BUZZING", "RESTING")
        assert len(log.entries) == 1
        assert "BUZZING -> RESTING" in log.entries[0].detail

    def test_record_queen_analysis(self, tmp_path):
        log = TestRunLog("test4", tmp_path)
        log.record_queen_analysis(
            worker_name="api",
            action="send_message",
            reasoning="worker needs task",
            confidence=0.85,
        )
        assert len(log.entries) == 1
        assert log.entries[0].queen_action == "send_message"

    def test_writes_jsonl(self, tmp_path):
        log = TestRunLog("test5", tmp_path)
        log.record_drone_decision("api", "content", "NONE", "idle")
        log.record_state_change("api", "RESTING", "BUZZING")

        lines = log.log_path.read_text().strip().split("\n")
        # Line 0 is the infra header; entries begin at index 1.
        assert len(lines) == 3
        header = json.loads(lines[0])
        assert "infra" in header

        entry1 = json.loads(lines[1])
        assert entry1["event_type"] == "drone_decision"

        entry2 = json.loads(lines[2])
        assert entry2["event_type"] == "state_change"

    def test_entries_are_copies(self, tmp_path):
        log = TestRunLog("test6", tmp_path)
        log.record_drone_decision("api", "c", "NONE", "r")
        entries = log.entries
        entries.clear()
        assert len(log.entries) == 1  # original unaffected
