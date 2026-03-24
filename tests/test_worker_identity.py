"""Tests for worker identity files and per-worker approval rules."""

from __future__ import annotations

from pathlib import Path

from swarm.config.models import DroneApprovalRule, WebhookConfig, WorkerConfig


class TestWorkerIdentity:
    def test_no_identity_returns_empty(self) -> None:
        wc = WorkerConfig(name="api", path="/tmp/api")
        assert wc.load_identity() == ""

    def test_resolved_identity_path_none_when_empty(self) -> None:
        wc = WorkerConfig(name="api", path="/tmp/api")
        assert wc.resolved_identity_path() is None

    def test_load_identity_from_file(self, tmp_path: Path) -> None:
        identity_file = tmp_path / "api.md"
        identity_file.write_text("# API Worker\nSpecializes in REST APIs.")
        wc = WorkerConfig(name="api", path="/tmp/api", identity=str(identity_file))
        content = wc.load_identity()
        assert "Specializes in REST APIs" in content

    def test_load_identity_missing_file(self) -> None:
        wc = WorkerConfig(name="api", path="/tmp/api", identity="/nonexistent/file.md")
        assert wc.load_identity() == ""

    def test_resolved_identity_path(self, tmp_path: Path) -> None:
        identity_file = tmp_path / "api.md"
        identity_file.touch()
        wc = WorkerConfig(name="api", path="/tmp/api", identity=str(identity_file))
        assert wc.resolved_identity_path() == identity_file.resolve()


class TestPerWorkerApprovalRules:
    def test_worker_config_with_rules(self) -> None:
        rules = [
            DroneApprovalRule(pattern=r"Bash\(docker", action="approve"),
            DroneApprovalRule(pattern=r"rm\s+-rf", action="escalate"),
        ]
        wc = WorkerConfig(name="infra", path="/tmp/infra", approval_rules=rules)
        assert len(wc.approval_rules) == 2
        assert wc.approval_rules[0].action == "approve"

    def test_worker_config_with_allowed_tools(self) -> None:
        wc = WorkerConfig(
            name="docs",
            path="/tmp/docs",
            allowed_tools=["Read", "Grep", "Glob"],
        )
        assert wc.allowed_tools == ["Read", "Grep", "Glob"]

    def test_worker_config_defaults_empty(self) -> None:
        wc = WorkerConfig(name="api", path="/tmp/api")
        assert wc.approval_rules == []
        assert wc.allowed_tools == []


class TestWebhookConfig:
    def test_defaults(self) -> None:
        wh = WebhookConfig()
        assert wh.url == ""
        assert wh.events == []

    def test_with_events(self) -> None:
        wh = WebhookConfig(
            url="https://hooks.slack.com/test",
            events=["worker_stung", "task_completed"],
        )
        assert wh.url == "https://hooks.slack.com/test"
        assert len(wh.events) == 2
