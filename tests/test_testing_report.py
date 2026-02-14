"""Tests for testing/report.py — ReportGenerator."""

from __future__ import annotations

import pytest

from swarm.testing.log import TestRunLog
from swarm.testing.report import ReportGenerator


class TestReportGeneratorStats:
    def test_empty_log(self, tmp_path):
        log = TestRunLog("empty", tmp_path)
        gen = ReportGenerator(log, tmp_path)
        stats = gen._compute_stats()
        assert stats["total_entries"] == 0
        assert stats["uncovered_decisions"] == 0
        assert stats["operator_approve_count"] == 0

    def test_decision_counts(self, tmp_path):
        log = TestRunLog("counts", tmp_path)
        log.record_drone_decision("api", "c", "CONTINUE", "r", "Read", 0)
        log.record_drone_decision("api", "c", "CONTINUE", "r", "Write", 1)
        log.record_drone_decision("api", "c", "ESCALATE", "r", "", -1)
        log.record_drone_decision("api", "c", "NONE", "r")

        gen = ReportGenerator(log, tmp_path)
        stats = gen._compute_stats()

        assert stats["decision_counts"]["CONTINUE"] == 2
        assert stats["decision_counts"]["ESCALATE"] == 1
        assert stats["decision_counts"]["NONE"] == 1
        assert stats["rule_hits"]["Read"] == 1
        assert stats["rule_hits"]["Write"] == 1
        assert stats["uncovered_decisions"] == 1

    def test_operator_stats(self, tmp_path):
        log = TestRunLog("ops", tmp_path)
        log.record_operator_decision("p1", "assignment", "api", True, "ok", 0.9, 100.0)
        log.record_operator_decision("p2", "escalation", "web", False, "no", 0.3, 200.0)
        log.record_operator_decision("p3", "completion", "api", True, "done", 0.95, 50.0)

        gen = ReportGenerator(log, tmp_path)
        stats = gen._compute_stats()

        assert stats["operator_approve_count"] == 2
        assert stats["operator_reject_count"] == 1
        assert abs(stats["avg_operator_latency_ms"] - 116.7) < 1.0
        assert abs(stats["avg_queen_confidence"] - 0.717) < 0.01

    def test_state_changes(self, tmp_path):
        log = TestRunLog("states", tmp_path)
        log.record_state_change("api", "BUZZING", "RESTING")
        log.record_state_change("api", "RESTING", "BUZZING")
        log.record_state_change("web", "BUZZING", "RESTING")

        gen = ReportGenerator(log, tmp_path)
        stats = gen._compute_stats()

        assert stats["state_changes"]["BUZZING -> RESTING"] == 2
        assert stats["state_changes"]["RESTING -> BUZZING"] == 1


class TestReportGeneratorWrite:
    def test_write_report(self, tmp_path):
        log = TestRunLog("write-test", tmp_path)
        log.record_drone_decision("api", "c", "CONTINUE", "r", "Read", 0)
        log.record_operator_decision("p1", "assignment", "api", True, "ok", 0.9, 100.0)

        gen = ReportGenerator(log, tmp_path)
        stats = gen._compute_stats()
        report_path = gen._write_report(stats, "Test analysis content")

        assert report_path.exists()
        content = report_path.read_text()
        assert "# Swarm Test Run Report" in content
        assert "write-test" in content
        assert "Test analysis content" in content
        assert "CONTINUE" in content

    @pytest.mark.asyncio
    async def test_generate_with_no_claude(self, tmp_path):
        """Test that generate() completes even when claude CLI is unavailable."""
        log = TestRunLog("no-claude", tmp_path)
        log.record_drone_decision("api", "c", "CONTINUE", "r")

        gen = ReportGenerator(log, tmp_path)
        report_path = await gen.generate()

        assert report_path.exists()
        content = report_path.read_text()
        assert "# Swarm Test Run Report" in content
