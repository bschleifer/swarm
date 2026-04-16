"""Tests for the claude_code_security service handler.

The handler wraps ``claude code security scan`` (an external CLI), parses
its JSON output, maps severity → task priority, and deduplicates findings
across scans via a persistent state file. Tests mock the subprocess so
they can run without the real CLI installed.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from swarm.services.handlers.claude_code_security import ClaudeCodeSecurity
from swarm.services.registry import ServiceContext


@pytest.fixture()
def ctx() -> ServiceContext:
    return ServiceContext(pipeline_id="p1", step_id="s1")


def _mock_subprocess(stdout: bytes, returncode: int = 0) -> AsyncMock:
    """Build an AsyncMock subprocess.create_subprocess_exec returns."""
    proc = MagicMock()
    proc.returncode = returncode
    proc.communicate = AsyncMock(return_value=(stdout, b""))
    fake_exec = AsyncMock(return_value=proc)
    return fake_exec


class TestClaudeCodeSecurityConfig:
    async def test_missing_target_dir(self, ctx):
        result = await ClaudeCodeSecurity().execute({}, ctx)
        assert not result.success
        assert "target_dir" in result.error.lower()

    async def test_target_dir_not_found(self, ctx):
        result = await ClaudeCodeSecurity().execute(
            {"target_dir": "/nonexistent/definitely/not/here"}, ctx
        )
        assert not result.success
        assert "not found" in result.error.lower() or "exist" in result.error.lower()


class TestClaudeCodeSecurityScanParsing:
    async def test_parses_findings_and_maps_severity(self, tmp_path, ctx):
        findings = {
            "findings": [
                {
                    "severity": "critical",
                    "rule_id": "sql-injection",
                    "title": "SQL injection in login",
                    "description": "User input concatenated into SQL",
                    "path": "src/auth.ts",
                    "line": 42,
                },
                {
                    "severity": "high",
                    "rule_id": "xss",
                    "title": "Stored XSS in comments",
                    "description": "Unescaped user HTML rendered",
                    "path": "src/render.ts",
                    "line": 101,
                },
                {
                    "severity": "low",
                    "rule_id": "weak-hash",
                    "title": "MD5 used",
                    "description": "Consider sha256",
                    "path": "src/legacy.ts",
                    "line": 7,
                },
            ]
        }
        fake_exec = _mock_subprocess(json.dumps(findings).encode())

        with patch("asyncio.create_subprocess_exec", fake_exec):
            result = await ClaudeCodeSecurity().execute(
                {"target_dir": str(tmp_path), "dedup_state_path": str(tmp_path / "dedup.json")},
                ctx,
            )

        assert result.success
        assert result.data["total_findings"] == 3
        by_pri = {f["priority"]: f["severity"] for f in result.data["findings"]}
        assert by_pri["urgent"] == "critical"
        assert by_pri["high"] == "high"
        assert by_pri["low"] == "low"

    async def test_malformed_json_fails_gracefully(self, tmp_path, ctx):
        fake_exec = _mock_subprocess(b"not-json")

        with patch("asyncio.create_subprocess_exec", fake_exec):
            result = await ClaudeCodeSecurity().execute({"target_dir": str(tmp_path)}, ctx)

        assert not result.success
        assert "json" in result.error.lower() or "parse" in result.error.lower()

    async def test_cli_exit_nonzero_propagates(self, tmp_path, ctx):
        fake_exec = _mock_subprocess(b"", returncode=2)

        with patch("asyncio.create_subprocess_exec", fake_exec):
            result = await ClaudeCodeSecurity().execute({"target_dir": str(tmp_path)}, ctx)

        assert not result.success
        assert "exit" in result.error.lower() or "failed" in result.error.lower()


class TestClaudeCodeSecurityDedup:
    async def test_skips_known_findings_on_second_run(self, tmp_path, ctx):
        findings = {
            "findings": [
                {
                    "severity": "high",
                    "rule_id": "xss",
                    "title": "XSS in comments",
                    "description": "bad",
                    "path": "src/x.ts",
                    "line": 10,
                },
            ]
        }
        dedup = tmp_path / "dedup.json"
        fake_exec = _mock_subprocess(json.dumps(findings).encode())

        with patch("asyncio.create_subprocess_exec", fake_exec):
            first = await ClaudeCodeSecurity().execute(
                {"target_dir": str(tmp_path), "dedup_state_path": str(dedup)}, ctx
            )
            second = await ClaudeCodeSecurity().execute(
                {"target_dir": str(tmp_path), "dedup_state_path": str(dedup)}, ctx
            )

        assert first.data["new_findings"] == 1
        assert first.data["skipped_dup"] == 0
        assert second.data["new_findings"] == 0
        assert second.data["skipped_dup"] == 1

    async def test_dedup_state_persists(self, tmp_path, ctx):
        findings = {
            "findings": [
                {
                    "severity": "high",
                    "rule_id": "xss",
                    "title": "XSS",
                    "description": "bad",
                    "path": "src/x.ts",
                    "line": 10,
                },
            ]
        }
        dedup = tmp_path / "dedup.json"
        fake_exec = _mock_subprocess(json.dumps(findings).encode())

        with patch("asyncio.create_subprocess_exec", fake_exec):
            await ClaudeCodeSecurity().execute(
                {"target_dir": str(tmp_path), "dedup_state_path": str(dedup)}, ctx
            )

        assert dedup.exists()
        state = json.loads(dedup.read_text())
        assert "hashes" in state
        assert len(state["hashes"]) == 1

    async def test_severity_filter(self, tmp_path, ctx):
        findings = {
            "findings": [
                {
                    "severity": "critical",
                    "rule_id": "sqli",
                    "title": "SQLi",
                    "description": "bad",
                    "path": "a.ts",
                    "line": 1,
                },
                {
                    "severity": "low",
                    "rule_id": "md5",
                    "title": "MD5",
                    "description": "ok",
                    "path": "b.ts",
                    "line": 2,
                },
            ]
        }
        fake_exec = _mock_subprocess(json.dumps(findings).encode())

        with patch("asyncio.create_subprocess_exec", fake_exec):
            result = await ClaudeCodeSecurity().execute(
                {
                    "target_dir": str(tmp_path),
                    "severity_filter": ["critical", "high"],
                    "dedup_state_path": str(tmp_path / "dedup.json"),
                },
                ctx,
            )

        assert result.success
        assert result.data["total_findings"] == 1
        assert result.data["findings"][0]["severity"] == "critical"


class TestClaudeCodeSecurityRegistered:
    def test_registered_in_defaults(self) -> None:
        from swarm.services.handlers import register_defaults
        from swarm.services.registry import ServiceRegistry

        reg = ServiceRegistry()
        register_defaults(reg)
        assert reg.has("claude_code_security")


class TestClaudeCodeSecurityTimeout:
    async def test_times_out(self, tmp_path, ctx):
        async def _hang(*_args, **_kwargs):
            import asyncio as _asyncio

            await _asyncio.sleep(10)

        proc = MagicMock()
        proc.communicate = _hang
        proc.kill = MagicMock()
        proc.wait = AsyncMock()
        fake_exec = AsyncMock(return_value=proc)

        with patch("asyncio.create_subprocess_exec", fake_exec):
            result = await ClaudeCodeSecurity().execute(
                {"target_dir": str(tmp_path), "timeout": 0.1}, ctx
            )

        assert not result.success
        assert "time" in result.error.lower() or "timeout" in result.error.lower()
