"""Tests for testing/log.py — TestRunLog and TestLogEntry."""

import json

from swarm.testing.log import TestLogEntry, TestRunLog


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
        assert len(lines) == 2

        entry1 = json.loads(lines[0])
        assert entry1["event_type"] == "drone_decision"

        entry2 = json.loads(lines[1])
        assert entry2["event_type"] == "state_change"

    def test_entries_are_copies(self, tmp_path):
        log = TestRunLog("test6", tmp_path)
        log.record_drone_decision("api", "c", "NONE", "r")
        entries = log.entries
        entries.clear()
        assert len(log.entries) == 1  # original unaffected
