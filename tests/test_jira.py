"""Tests for Jira integration (Phase 6)."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from swarm.config import HiveConfig, JiraConfig
from swarm.integrations.jira import (
    JiraSyncService,
    JiraSyncStats,
    _extract_text,
    _find_transition,
    _jira_issue_to_task,
)
from swarm.tasks.task import SwarmTask, TaskPriority, TaskStatus, TaskType

# --- JiraConfig ---


class TestJiraConfig:
    def test_defaults(self) -> None:
        cfg = JiraConfig()
        assert cfg.enabled is False
        assert cfg.url == ""
        assert cfg.sync_interval_minutes == 5.0
        assert "pending" in cfg.status_map

    def test_resolved_token_plain(self) -> None:
        cfg = JiraConfig(token="abc123")
        assert cfg.resolved_token() == "abc123"

    def test_resolved_token_env_var(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MY_JIRA_TOKEN", "secret")
        cfg = JiraConfig(token="$MY_JIRA_TOKEN")
        assert cfg.resolved_token() == "secret"

    def test_resolved_token_missing_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("MISSING_VAR", raising=False)
        cfg = JiraConfig(token="$MISSING_VAR")
        assert cfg.resolved_token() == ""


# --- Config Integration ---


class TestJiraConfigIntegration:
    def test_hive_config_has_jira(self) -> None:
        cfg = HiveConfig()
        assert isinstance(cfg.jira, JiraConfig)
        assert cfg.jira.enabled is False

    def test_validation_disabled_no_errors(self) -> None:
        cfg = HiveConfig()
        errors = cfg.validate()
        jira_errors = [e for e in errors if "jira" in e]
        assert jira_errors == []

    def test_validation_enabled_missing_fields(self) -> None:
        cfg = HiveConfig(jira=JiraConfig(enabled=True))
        errors = cfg.validate()
        jira_errors = [e for e in errors if "jira" in e]
        assert len(jira_errors) == 4  # url, email, token, project

    def test_validation_enabled_complete(self) -> None:
        cfg = HiveConfig(
            jira=JiraConfig(
                enabled=True,
                url="https://x.atlassian.net",
                email="a@b.com",
                token="tok",
                project="PROJ",
            )
        )
        errors = cfg.validate()
        jira_errors = [e for e in errors if "jira" in e]
        assert jira_errors == []

    def test_validation_bad_interval(self) -> None:
        cfg = HiveConfig(jira=JiraConfig(sync_interval_minutes=0))
        errors = cfg.validate()
        assert any("sync_interval_minutes" in e for e in errors)

    def test_serialization(self) -> None:
        from swarm.config import serialize_config

        cfg = HiveConfig(
            jira=JiraConfig(
                enabled=True,
                url="https://x.atlassian.net",
                email="a@b.com",
                token="tok",
                project="PROJ",
            )
        )
        data = serialize_config(cfg)
        assert "jira" in data
        assert data["jira"]["enabled"] is True
        assert data["jira"]["url"] == "https://x.atlassian.net"
        assert data["jira"]["project"] == "PROJ"

    def test_serialization_disabled_omitted(self) -> None:
        from swarm.config import serialize_config

        cfg = HiveConfig()
        data = serialize_config(cfg)
        assert "jira" not in data


# --- Helpers ---


class TestExtractText:
    def test_plain_string(self) -> None:
        assert _extract_text("hello world") == "hello world"

    def test_adf_document(self) -> None:
        adf = {
            "type": "doc",
            "version": 1,
            "content": [
                {
                    "type": "paragraph",
                    "content": [
                        {"type": "text", "text": "Hello"},
                        {"type": "text", "text": "world"},
                    ],
                }
            ],
        }
        assert _extract_text(adf) == "Hello world"

    def test_none(self) -> None:
        assert _extract_text(None) == ""

    def test_empty_dict(self) -> None:
        assert _extract_text({}) == ""


class TestFindTransition:
    def test_match_by_name(self) -> None:
        transitions = [
            {"id": "1", "name": "To Do"},
            {"id": "2", "name": "In Progress"},
            {"id": "3", "name": "Done"},
        ]
        assert _find_transition(transitions, "Done") == "3"

    def test_match_case_insensitive(self) -> None:
        transitions = [{"id": "1", "name": "In Progress"}]
        assert _find_transition(transitions, "in progress") == "1"

    def test_match_by_to_status(self) -> None:
        transitions = [
            {"id": "5", "name": "Move to Done", "to": {"name": "Done"}},
        ]
        assert _find_transition(transitions, "Done") == "5"

    def test_no_match(self) -> None:
        transitions = [{"id": "1", "name": "In Progress"}]
        assert _find_transition(transitions, "Done") is None

    def test_empty(self) -> None:
        assert _find_transition([], "Done") is None


class TestJiraIssueToTask:
    def test_basic_conversion(self) -> None:
        fields = {
            "summary": "Fix login bug",
            "description": "Users can't login",
            "issuetype": {"name": "Bug"},
            "priority": {"name": "High"},
        }
        task = _jira_issue_to_task("PROJ-123", fields)
        assert task.title == "Fix login bug"
        assert task.description == "Users can't login"
        assert task.jira_key == "PROJ-123"
        assert task.task_type == TaskType.BUG
        assert task.priority == TaskPriority.HIGH

    def test_story_maps_to_feature(self) -> None:
        fields = {
            "summary": "Add dashboard",
            "issuetype": {"name": "Story"},
            "priority": {"name": "Medium"},
        }
        task = _jira_issue_to_task("PROJ-1", fields)
        assert task.task_type == TaskType.FEATURE
        assert task.priority == TaskPriority.NORMAL

    def test_unknown_type_defaults_to_chore(self) -> None:
        fields = {"summary": "Deploy", "issuetype": {"name": "Unknown"}}
        task = _jira_issue_to_task("PROJ-1", fields)
        assert task.task_type == TaskType.CHORE

    def test_adf_description(self) -> None:
        fields = {
            "summary": "Test",
            "description": {
                "type": "doc",
                "version": 1,
                "content": [
                    {
                        "type": "paragraph",
                        "content": [{"type": "text", "text": "ADF body"}],
                    }
                ],
            },
        }
        task = _jira_issue_to_task("PROJ-1", fields)
        assert task.description == "ADF body"

    def test_missing_fields(self) -> None:
        task = _jira_issue_to_task("PROJ-1", {})
        assert task.title == "PROJ-1"
        assert task.description == ""
        assert task.task_type == TaskType.CHORE
        assert task.priority == TaskPriority.NORMAL


# --- JiraSyncService ---


class TestJiraSyncService:
    def _make_service(self, **kwargs: object) -> JiraSyncService:
        defaults = {
            "enabled": True,
            "url": "https://x.atlassian.net",
            "email": "a@b.com",
            "token": "tok",
            "project": "PROJ",
        }
        defaults.update(kwargs)
        cfg = JiraConfig(**defaults)  # type: ignore[arg-type]
        return JiraSyncService(cfg)

    def test_enabled(self) -> None:
        svc = self._make_service()
        assert svc.enabled is True

    def test_disabled_no_url(self) -> None:
        svc = self._make_service(url="")
        assert svc.enabled is False

    def test_disabled_flag(self) -> None:
        svc = self._make_service(enabled=False)
        assert svc.enabled is False

    def test_get_status(self) -> None:
        svc = self._make_service()
        status = svc.get_status()
        assert status["enabled"] is True
        assert status["url"] == "https://x.atlassian.net"
        assert status["total_syncs"] == 0

    @pytest.mark.asyncio
    async def test_import_disabled(self) -> None:
        svc = self._make_service(enabled=False)
        result = await svc.import_issues({})
        assert result == []

    @pytest.mark.asyncio
    async def test_import_deduplicates(self) -> None:
        svc = self._make_service()
        svc.client.search_issues = AsyncMock(
            return_value=[
                {
                    "key": "PROJ-1",
                    "fields": {"summary": "Existing", "issuetype": {"name": "Task"}},
                },
                {
                    "key": "PROJ-2",
                    "fields": {"summary": "New", "issuetype": {"name": "Bug"}},
                },
            ]
        )
        existing = {
            "abc": SwarmTask(title="Existing", jira_key="PROJ-1"),
        }
        new_tasks = await svc.import_issues(existing)
        assert len(new_tasks) == 1
        assert new_tasks[0].jira_key == "PROJ-2"
        assert svc.stats.total_imported == 1

    @pytest.mark.asyncio
    async def test_import_handles_error(self) -> None:
        import aiohttp

        svc = self._make_service()
        svc.client.search_issues = AsyncMock(side_effect=aiohttp.ClientError("connection failed"))
        result = await svc.import_issues({})
        assert result == []
        assert svc.stats.errors == 1

    @pytest.mark.asyncio
    async def test_export_status(self) -> None:
        svc = self._make_service()
        svc.client.get_transitions = AsyncMock(
            return_value=[
                {"id": "31", "name": "In Progress"},
            ]
        )
        svc.client.transition_issue = AsyncMock(return_value=True)
        task = SwarmTask(title="Test", jira_key="PROJ-1")
        ok = await svc.export_status(task, TaskStatus.IN_PROGRESS)
        assert ok is True
        svc.client.transition_issue.assert_called_once_with("PROJ-1", "31")
        assert svc.stats.total_exported == 1

    @pytest.mark.asyncio
    async def test_export_status_no_jira_key(self) -> None:
        svc = self._make_service()
        task = SwarmTask(title="Test")
        ok = await svc.export_status(task, TaskStatus.IN_PROGRESS)
        assert ok is False

    @pytest.mark.asyncio
    async def test_export_no_matching_transition(self) -> None:
        svc = self._make_service()
        svc.client.get_transitions = AsyncMock(return_value=[{"id": "1", "name": "To Do"}])
        task = SwarmTask(title="Test", jira_key="PROJ-1")
        ok = await svc.export_status(task, TaskStatus.IN_PROGRESS)
        assert ok is False

    @pytest.mark.asyncio
    async def test_post_completion_comment(self) -> None:
        svc = self._make_service()
        svc.client.add_comment = AsyncMock(return_value=True)
        task = SwarmTask(
            title="Test",
            jira_key="PROJ-1",
            assigned_worker="w1",
            resolution="Fixed the issue",
        )
        ok = await svc.post_completion_comment(task, summary="All done")
        assert ok is True
        call_body = svc.client.add_comment.call_args[0][1]
        assert "w1" in call_body
        assert "Fixed the issue" in call_body
        assert "All done" in call_body

    @pytest.mark.asyncio
    async def test_post_completion_no_jira_key(self) -> None:
        svc = self._make_service()
        task = SwarmTask(title="Test")
        ok = await svc.post_completion_comment(task)
        assert ok is False


# --- JiraSyncStats ---


class TestJiraSyncStats:
    def test_defaults(self) -> None:
        stats = JiraSyncStats()
        assert stats.total_syncs == 0
        assert stats.total_imported == 0
        assert stats.total_exported == 0
        assert stats.errors == 0
        assert stats.last_error == ""
