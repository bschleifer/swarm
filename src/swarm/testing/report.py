"""ReportGenerator — produces analysis reports from test run logs."""

from __future__ import annotations

import asyncio
import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

from swarm.logging import get_logger
from swarm.testing.log import TestLogEntry, TestRunLog

_log = get_logger("testing.report")


def _tally_drone(
    entry: TestLogEntry,
    decision_counts: Counter[str],
    rule_hits: Counter[str],
) -> int:
    """Process a drone_decision entry. Returns 1 if uncovered, 0 otherwise."""
    decision_counts[entry.decision] += 1
    if entry.rule_pattern:
        rule_hits[entry.rule_pattern] += 1
    elif entry.decision in ("CONTINUE", "ESCALATE") and entry.rule_index == -1:
        return 1
    return 0


def _tally_operator(
    entry: TestLogEntry,
    operator_latencies: list[float],
    queen_confidences: list[float],
) -> tuple[int, int]:
    """Process an operator_decision entry. Returns (approvals, rejections)."""
    approved = 1 if "approved" in entry.detail else 0
    rejected = 0 if approved else 1
    if entry.decision_latency_ms > 0:
        operator_latencies.append(entry.decision_latency_ms)
    if entry.queen_confidence >= 0:
        queen_confidences.append(entry.queen_confidence)
    return approved, rejected


class ReportGenerator:
    """Generates markdown reports with stats and AI-powered suggestions."""

    def __init__(self, test_log: TestRunLog, report_dir: Path | None = None) -> None:
        self._test_log = test_log
        self._report_dir = report_dir or Path("~/.swarm/reports").expanduser()
        self._report_dir.mkdir(parents=True, exist_ok=True)

    async def generate(self) -> Path:
        """Generate a full test report. Returns the path to the markdown file."""
        stats = self._compute_stats()
        analysis = await self._run_analysis(stats)
        return self._write_report(stats, analysis)

    def report_exists(self) -> bool:
        """Check if a report has already been written for this test run."""
        report_path = self._report_dir / f"test-run-{self._test_log.run_id}.md"
        return report_path.exists()

    async def generate_if_pending(self) -> Path | None:
        """Generate a report only if one hasn't been written yet.

        Called on daemon shutdown as a fallback — ensures every test run
        produces a report even if hive_complete never fired.
        Returns the report path, or None if a report already existed.
        """
        if self.report_exists():
            _log.info("report already exists for run %s — skipping", self._test_log.run_id)
            return None
        if not self._test_log.entries:
            _log.info("no entries for run %s — skipping report", self._test_log.run_id)
            return None
        _log.info("generating fallback report for run %s", self._test_log.run_id)
        return await self.generate()

    def _compute_stats(self) -> dict[str, Any]:
        """Compute summary statistics from the test log entries."""
        entries = self._test_log.entries

        decision_counts: Counter[str] = Counter()
        rule_hits: Counter[str] = Counter()
        uncovered_decisions = 0

        operator_approve_count = 0
        operator_reject_count = 0
        operator_latencies: list[float] = []

        state_changes: Counter[str] = Counter()
        queen_confidences: list[float] = []

        for entry in entries:
            if entry.event_type == "drone_decision":
                uncovered_decisions += _tally_drone(entry, decision_counts, rule_hits)
            elif entry.event_type == "operator_decision":
                a, r = _tally_operator(entry, operator_latencies, queen_confidences)
                operator_approve_count += a
                operator_reject_count += r
            elif entry.event_type == "state_change":
                state_changes[entry.detail] += 1
            elif entry.event_type == "queen_analysis":
                if entry.queen_confidence >= 0:
                    queen_confidences.append(entry.queen_confidence)

        avg_op_lat = (
            sum(operator_latencies) / len(operator_latencies) if operator_latencies else 0.0
        )
        avg_q_conf = sum(queen_confidences) / len(queen_confidences) if queen_confidences else 0.0

        return {
            "total_entries": len(entries),
            "decision_counts": dict(decision_counts),
            "rule_hits": dict(rule_hits),
            "uncovered_decisions": uncovered_decisions,
            "operator_approve_count": operator_approve_count,
            "operator_reject_count": operator_reject_count,
            "avg_operator_latency_ms": round(avg_op_lat, 1),
            "state_changes": dict(state_changes),
            "avg_queen_confidence": round(avg_q_conf, 3),
            "queen_confidence_values": queen_confidences,
        }

    async def _run_analysis(self, stats: dict[str, Any]) -> str:
        """Run AI analysis on the stats and sample log entries.

        Uses ``claude -p`` for actionable suggestions. Falls back to
        a placeholder if claude is not available.
        """
        entries = self._test_log.entries
        # Sample up to 20 diverse entries for context
        sample_entries = (
            entries[:20] if len(entries) <= 20 else entries[:: max(1, len(entries) // 20)][:20]
        )

        sample_json = json.dumps(
            [
                {
                    "event_type": e.event_type,
                    "worker": e.worker_name,
                    "decision": e.decision,
                    "reason": e.detail,
                    "rule_pattern": e.rule_pattern,
                    "rule_index": e.rule_index,
                    "queen_confidence": e.queen_confidence,
                }
                for e in sample_entries
            ],
            indent=2,
        )

        prompt = self._build_analysis_prompt(stats, sample_json)

        try:
            proc = await asyncio.create_subprocess_exec(
                "claude",
                "-p",
                prompt,
                "--output-format",
                "text",
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
            if proc.returncode == 0 and stdout:
                return stdout.decode().strip()
            _log.warning(
                "claude analysis failed (rc=%s): %s", proc.returncode, stderr.decode()[:200]
            )
        except (FileNotFoundError, asyncio.TimeoutError):
            _log.info("claude not available for analysis — using placeholder")
        except Exception:
            _log.warning("AI analysis failed", exc_info=True)

        return (
            "_AI analysis unavailable — claude CLI not found or timed out._\n\n"
            "Review the raw stats above and the JSONL log file for manual analysis."
        )

    @staticmethod
    def _build_analysis_prompt(stats: dict[str, Any], sample_json: str) -> str:
        """Build the prompt for AI analysis."""
        return (
            "You are analyzing a Swarm test run. "
            "Here are the aggregated stats:\n\n"
            f"```json\n{json.dumps(stats, indent=2)}\n```\n\n"
            "And here are sample log entries:\n\n"
            f"```json\n{sample_json}\n```\n\n"
            "Provide actionable suggestions in these categories:\n"
            "1. **Rule Changes**: Which approval_rules patterns "
            "should be added, modified, or removed?\n"
            "2. **Threshold Adjustments**: Should escalation_threshold, "
            "auto_resolve_delay, or poll_interval change?\n"
            "3. **Uncovered Patterns**: Which drone decisions had "
            "no matching rule? What patterns would cover them?\n"
            "4. **Queen Prompt Improvements**: Based on confidence "
            "scores and decisions, how could Queen prompts improve?\n"
            "5. **General Observations**: Any other optimizations?\n\n"
            "Be specific and actionable. "
            "Reference actual patterns and numbers from the data."
        )

    def _write_report(self, stats: dict[str, Any], analysis: str) -> Path:
        """Write the markdown report file."""
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        report_path = self._report_dir / f"test-run-{self._test_log.run_id}.md"

        decision_table = "\n".join(f"| {k} | {v} |" for k, v in stats["decision_counts"].items())
        rule_table = "\n".join(f"| `{k}` | {v} |" for k, v in stats["rule_hits"].items())
        state_table = "\n".join(f"| {k} | {v} |" for k, v in stats["state_changes"].items())

        content = f"""# Swarm Test Run Report

**Run ID:** {self._test_log.run_id}
**Generated:** {timestamp}
**Log file:** `{self._test_log.log_path}`

---

## Summary

| Metric | Value |
|--------|-------|
| Total log entries | {stats["total_entries"]} |
| Uncovered decisions (no rule match) | {stats["uncovered_decisions"]} |
| Operator approvals | {stats["operator_approve_count"]} |
| Operator rejections | {stats["operator_reject_count"]} |
| Avg operator latency | {stats["avg_operator_latency_ms"]}ms |
| Avg Queen confidence | {stats["avg_queen_confidence"]} |

## Decision Distribution

| Decision | Count |
|----------|-------|
{decision_table}

## Rule Hit Distribution

| Pattern | Hits |
|---------|------|
{rule_table}

## State Changes

| Transition | Count |
|------------|-------|
{state_table}

---

## AI Analysis

{analysis}

---

## Suggested Rule Changes

_Based on the analysis above, consider updating your \
`swarm.yaml` `drones.approval_rules` section._

---

*Report generated by Swarm Test Mode*
"""

        report_path.write_text(content)
        _log.info("report written to %s", report_path)
        return report_path
