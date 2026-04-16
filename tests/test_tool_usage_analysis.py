"""Tests for swarm.analysis.tool_usage — MCP usage aggregation."""

from __future__ import annotations

import time

from swarm.analysis.tool_usage import (
    ToolStats,
    aggregate,
    parse_mcp_detail,
)


class TestParseMcpDetail:
    def test_tool_with_snippet(self):
        assert parse_mcp_detail("mcp:complete_task → Task #3 completed.") == (
            "complete_task",
            "Task #3 completed.",
        )

    def test_tool_without_snippet(self):
        assert parse_mcp_detail("mcp:check_messages") == ("check_messages", "")

    def test_non_mcp_line_returns_none(self):
        assert parse_mcp_detail("progress: phase=implementing") is None
        assert parse_mcp_detail("") is None
        assert parse_mcp_detail("some other action") is None


class TestAggregate:
    def _entry(self, **kwargs):
        defaults = {
            "timestamp": 1000.0,
            "worker_name": "api",
            "detail": "",
        }
        defaults.update(kwargs)
        return defaults

    def test_empty_entries(self):
        assert aggregate([]) == []

    def test_groups_by_tool(self):
        entries = [
            self._entry(detail="mcp:check_messages → No pending messages."),
            self._entry(detail="mcp:check_messages → No pending messages."),
            self._entry(detail="mcp:task_status → No tasks found."),
        ]
        stats = aggregate(entries)
        by_tool = {s.tool: s for s in stats}
        assert by_tool["check_messages"].calls == 2
        assert by_tool["task_status"].calls == 1

    def test_sort_most_called_first(self):
        entries = [self._entry(detail="mcp:a") for _ in range(1)] + [
            self._entry(detail="mcp:b") for _ in range(5)
        ]
        stats = aggregate(entries)
        assert stats[0].tool == "b"
        assert stats[1].tool == "a"

    def test_tracks_workers(self):
        entries = [
            self._entry(detail="mcp:complete_task → ok", worker_name="api"),
            self._entry(detail="mcp:complete_task → ok", worker_name="web"),
            self._entry(detail="mcp:complete_task → ok", worker_name="api"),
        ]
        stats = aggregate(entries)
        assert stats[0].workers == {"api", "web"}

    def test_detects_error_snippets(self):
        entries = [
            self._entry(detail="mcp:send_message → Missing 'to' or 'content'"),
            self._entry(detail="mcp:send_message → Message sent to platform."),
            self._entry(detail="mcp:send_message → Failed to send message."),
        ]
        stats = aggregate(entries)
        s = stats[0]
        assert s.calls == 3
        assert s.errors == 2
        assert round(s.error_rate, 3) == round(2 / 3, 3)

    def test_collects_distinct_error_samples(self):
        entries = [
            self._entry(detail=f"mcp:send_message → {msg}")
            for msg in [
                "Missing 'to' or 'content'",
                "Missing 'to' or 'content'",  # duplicate
                "Failed to send message.",
                "Invalid recipient",
                "Missing 'to' or 'content'",  # duplicate
            ]
        ]
        stats = aggregate(entries)
        samples = stats[0].error_samples
        # Duplicates collapsed; order preserved
        assert samples == [
            "Missing 'to' or 'content'",
            "Failed to send message.",
            "Invalid recipient",
        ]

    def test_caps_error_samples(self):
        entries = [self._entry(detail=f"mcp:x → Error variant {i}") for i in range(20)]
        stats = aggregate(entries)
        assert len(stats[0].error_samples) == ToolStats.MAX_ERROR_SAMPLES

    def test_first_and_last_timestamp(self):
        entries = [
            self._entry(timestamp=100.0, detail="mcp:x → a"),
            self._entry(timestamp=50.0, detail="mcp:x → a"),
            self._entry(timestamp=200.0, detail="mcp:x → a"),
        ]
        stats = aggregate(entries)
        s = stats[0]
        assert s.first_timestamp == 50.0
        assert s.last_timestamp == 200.0

    def test_ignores_non_mcp_entries(self):
        entries = [
            self._entry(detail="mcp:check_messages → ok"),
            self._entry(detail="progress: phase=implementing"),
            self._entry(detail="→ api: custom message"),
        ]
        stats = aggregate(entries)
        assert len(stats) == 1
        assert stats[0].tool == "check_messages"


class TestToolStatsReportRow:
    def test_includes_expected_fields(self):
        s = ToolStats(tool="complete_task")
        s.record(
            {"timestamp": 1000.0, "worker_name": "api"},
            "Task #3 completed.",
        )
        s.record(
            {"timestamp": 2000.0, "worker_name": "web"},
            "No active task found.",
        )
        row = s.to_report_row()
        assert row["tool"] == "complete_task"
        assert row["calls"] == 2
        assert row["errors"] == 1
        assert row["workers"] == ["api", "web"]
        assert row["first_timestamp"] == 1000.0
        assert row["last_timestamp"] == 2000.0


class TestAnalyzeToolsCLI:
    def test_end_to_end_with_real_db(self, tmp_path, monkeypatch):
        """End-to-end: write some mcp entries to a real DB, invoke the
        CLI, confirm the summary appears."""
        from click.testing import CliRunner

        from swarm.cli import main
        from swarm.db.core import SwarmDB

        db_path = tmp_path / "swarm.db"
        db = SwarmDB(db_path)
        now = time.time()
        for detail, ts in [
            ("mcp:complete_task → Task done.", now - 100),
            ("mcp:complete_task → No active task found.", now - 50),
            ("mcp:check_messages → No pending messages.", now - 10),
        ]:
            db.execute(
                """
                INSERT INTO buzz_log (timestamp, action, worker_name, detail, category)
                VALUES (?, 'OPERATOR', 'api', ?, 'worker')
                """,
                (ts, detail),
            )
        db.commit()
        db.close()

        runner = CliRunner()
        result = runner.invoke(main, ["analyze-tools", "--since=1d", "--db", str(db_path)])
        assert result.exit_code == 0, result.output
        assert "complete_task" in result.output
        assert "check_messages" in result.output
        # Errors section should surface the "No active task" message
        assert "No active task found" in result.output

    def test_json_output(self, tmp_path):
        from click.testing import CliRunner

        from swarm.cli import main
        from swarm.db.core import SwarmDB

        db_path = tmp_path / "swarm.db"
        db = SwarmDB(db_path)
        db.execute(
            """
            INSERT INTO buzz_log (timestamp, action, worker_name, detail, category)
            VALUES (?, 'OPERATOR', 'api', 'mcp:check_messages → ok', 'worker')
            """,
            (time.time(),),
        )
        db.commit()
        db.close()

        runner = CliRunner()
        result = runner.invoke(
            main, ["analyze-tools", "--since=1d", "--db", str(db_path), "--json"]
        )
        assert result.exit_code == 0, result.output
        import json

        payload = json.loads(result.output)
        assert "tools" in payload
        assert payload["tools"][0]["tool"] == "check_messages"

    def test_invalid_window_errors(self):
        from click.testing import CliRunner

        from swarm.cli import main

        runner = CliRunner()
        result = runner.invoke(main, ["analyze-tools", "--since=garbage"])
        assert result.exit_code != 0
