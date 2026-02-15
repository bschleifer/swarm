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


class TestGenerateIfPending:
    """Tests for generate_if_pending() — fallback report on shutdown."""

    @pytest.mark.asyncio
    async def test_generates_when_no_report_exists(self, tmp_path):
        """Should generate a report if one hasn't been written yet."""
        log = TestRunLog("pending-test", tmp_path)
        log.record_drone_decision("api", "c", "CONTINUE", "r")

        gen = ReportGenerator(log, tmp_path)
        report_path = await gen.generate_if_pending()

        assert report_path is not None
        assert report_path.exists()
        content = report_path.read_text()
        assert "pending-test" in content

    @pytest.mark.asyncio
    async def test_skips_when_report_already_exists(self, tmp_path):
        """Should return None if a report was already written."""
        log = TestRunLog("already-done", tmp_path)
        log.record_drone_decision("api", "c", "CONTINUE", "r")

        gen = ReportGenerator(log, tmp_path)
        # Generate the first report
        first_path = await gen.generate()
        assert first_path.exists()

        # Fallback should skip
        result = await gen.generate_if_pending()
        assert result is None

    @pytest.mark.asyncio
    async def test_skips_when_no_entries(self, tmp_path):
        """Should return None for empty logs (no data to report on)."""
        log = TestRunLog("empty-run", tmp_path)

        gen = ReportGenerator(log, tmp_path)
        result = await gen.generate_if_pending()
        assert result is None

    def test_report_exists_false_initially(self, tmp_path):
        """report_exists() should return False before any report is written."""
        log = TestRunLog("check-exists", tmp_path)
        gen = ReportGenerator(log, tmp_path)
        assert gen.report_exists() is False

    @pytest.mark.asyncio
    async def test_report_exists_true_after_generate(self, tmp_path):
        """report_exists() should return True after a report is generated."""
        log = TestRunLog("check-after", tmp_path)
        log.record_drone_decision("api", "c", "CONTINUE", "r")

        gen = ReportGenerator(log, tmp_path)
        await gen.generate()
        assert gen.report_exists() is True
