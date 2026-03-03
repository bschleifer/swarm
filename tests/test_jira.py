"""Tests for Jira integration (OAuth 2.0 only)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from swarm.config import HiveConfig, JiraConfig
from swarm.integrations.jira import (
    _SWARM_PRIORITY_TO_JIRA,
    _SWARM_TYPE_TO_JIRA,
    JiraSyncService,
    JiraSyncStats,
    _extract_text,
    _find_transition,
    _jira_issue_to_task,
)
from swarm.tasks.task import SwarmTask, TaskPriority, TaskStatus, TaskType


def _mock_mgr(connected: bool = True) -> MagicMock:
    """Create a mock JiraTokenManager that reports the given connected state."""
    mgr = MagicMock()
    mgr.is_connected.return_value = connected
    mgr.api_base_url = "https://api.atlassian.com/ex/jira/test-cloud"
    return mgr


# --- JiraConfig ---


class TestJiraConfig:
    def test_defaults(self) -> None:
        cfg = JiraConfig()
        assert cfg.enabled is False
        assert cfg.sync_interval_minutes == 5.0
        assert "pending" in cfg.status_map

    def test_resolved_client_secret_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MY_SECRET", "s3cret")
        cfg = JiraConfig(client_secret="$MY_SECRET")
        assert cfg.resolved_client_secret() == "s3cret"

    def test_resolved_client_secret_plain(self) -> None:
        cfg = JiraConfig(client_secret="plain")
        assert cfg.resolved_client_secret() == "plain"


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
        assert len(jira_errors) == 3  # client_id, client_secret, project

    def test_validation_enabled_complete(self) -> None:
        cfg = HiveConfig(
            jira=JiraConfig(
                enabled=True,
                client_id="cid",
                client_secret="csecret",
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
                client_id="cid",
                client_secret="csecret",
                cloud_id="cloud-1",
                project="PROJ",
            )
        )
        data = serialize_config(cfg)
        assert "jira" in data
        assert data["jira"]["enabled"] is True
        assert data["jira"]["client_id"] == "cid"
        assert data["jira"]["client_secret"] == "csecret"
        assert data["jira"]["cloud_id"] == "cloud-1"
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
        mgr = kwargs.pop("_mgr", None) or _mock_mgr()
        defaults: dict[str, object] = {
            "enabled": True,
            "project": "PROJ",
        }
        defaults.update(kwargs)
        cfg = JiraConfig(**defaults)  # type: ignore[arg-type]
        return JiraSyncService(cfg, token_manager=mgr)

    def test_enabled(self) -> None:
        svc = self._make_service()
        assert svc.enabled is True

    def test_disabled_no_manager(self) -> None:
        cfg = JiraConfig(enabled=True, project="PROJ")
        svc = JiraSyncService(cfg, token_manager=None)
        assert svc.enabled is False

    def test_disabled_disconnected(self) -> None:
        svc = self._make_service(_mgr=_mock_mgr(connected=False))
        assert svc.enabled is False

    def test_disabled_flag(self) -> None:
        svc = self._make_service(enabled=False)
        assert svc.enabled is False

    def test_get_status(self) -> None:
        svc = self._make_service()
        status = svc.get_status()
        assert status["enabled"] is True
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


# --- Label Filtering ---


class TestLabelFiltering:
    def _make_service(self, **kwargs: object) -> JiraSyncService:
        defaults: dict[str, object] = {
            "enabled": True,
            "project": "PROJ",
        }
        defaults.update(kwargs)
        cfg = JiraConfig(**defaults)  # type: ignore[arg-type]
        return JiraSyncService(cfg, token_manager=_mock_mgr())

    @pytest.mark.asyncio
    async def test_import_label_filters_client_side(self) -> None:
        """Label filtering happens client-side, not in JQL."""
        svc = self._make_service(import_label="swarm")
        svc.client.search_issues = AsyncMock(
            return_value=[
                {
                    "key": "PROJ-1",
                    "fields": {
                        "summary": "Match",
                        "issuetype": {"name": "Task"},
                        "labels": ["swarm"],
                    },
                },
                {
                    "key": "PROJ-2",
                    "fields": {
                        "summary": "No label",
                        "issuetype": {"name": "Task"},
                        "labels": [],
                    },
                },
            ]
        )
        tasks = await svc.import_issues({})
        assert len(tasks) == 1
        assert tasks[0].jira_key == "PROJ-1"
        # JQL should NOT contain label filter
        jql = svc.client.search_issues.call_args[0][0]
        assert "labels" not in jql

    @pytest.mark.asyncio
    async def test_import_label_case_insensitive(self) -> None:
        """Label filter matches regardless of case (Swarm, swarm, SWARM)."""
        svc = self._make_service(import_label="swarm")
        svc.client.search_issues = AsyncMock(
            return_value=[
                {
                    "key": "PROJ-1",
                    "fields": {
                        "summary": "Uppercase",
                        "issuetype": {"name": "Task"},
                        "labels": ["Swarm"],
                    },
                },
                {
                    "key": "PROJ-2",
                    "fields": {
                        "summary": "Mixed",
                        "issuetype": {"name": "Task"},
                        "labels": ["SWARM"],
                    },
                },
                {
                    "key": "PROJ-3",
                    "fields": {
                        "summary": "Wrong",
                        "issuetype": {"name": "Task"},
                        "labels": ["other"],
                    },
                },
            ]
        )
        tasks = await svc.import_issues({})
        assert len(tasks) == 2
        assert {t.jira_key for t in tasks} == {"PROJ-1", "PROJ-2"}

    @pytest.mark.asyncio
    async def test_import_label_empty_no_filter(self) -> None:
        """When import_label is empty, all issues are returned."""
        svc = self._make_service(import_label="")
        svc.client.search_issues = AsyncMock(
            return_value=[
                {
                    "key": "PROJ-1",
                    "fields": {
                        "summary": "Any",
                        "issuetype": {"name": "Task"},
                        "labels": [],
                    },
                },
            ]
        )
        tasks = await svc.import_issues({})
        assert len(tasks) == 1


# --- OAuth ---


class TestOAuth:
    def test_enabled_no_manager(self) -> None:
        cfg = JiraConfig(enabled=True)
        svc = JiraSyncService(cfg, token_manager=None)
        assert svc.enabled is False

    def test_enabled_connected(self) -> None:
        cfg = JiraConfig(enabled=True)
        svc = JiraSyncService(cfg, token_manager=_mock_mgr(connected=True))
        assert svc.enabled is True

    def test_enabled_disconnected(self) -> None:
        cfg = JiraConfig(enabled=True)
        svc = JiraSyncService(cfg, token_manager=_mock_mgr(connected=False))
        assert svc.enabled is False

    def test_validation_missing_fields(self) -> None:
        cfg = HiveConfig(jira=JiraConfig(enabled=True, project="P"))
        errors = cfg.validate()
        jira_errors = [e for e in errors if "jira" in e]
        assert any("client_id" in e for e in jira_errors)
        assert any("client_secret" in e for e in jira_errors)

    def test_validation_complete(self) -> None:
        cfg = HiveConfig(
            jira=JiraConfig(
                enabled=True,
                client_id="cid",
                client_secret="csecret",
                project="PROJ",
            )
        )
        errors = cfg.validate()
        jira_errors = [e for e in errors if "jira" in e]
        assert jira_errors == []


# --- Issue Creation ---


class TestIssueCreation:
    def _make_service(self, **kwargs: object) -> JiraSyncService:
        defaults: dict[str, object] = {
            "enabled": True,
            "project": "PROJ",
        }
        defaults.update(kwargs)
        cfg = JiraConfig(**defaults)  # type: ignore[arg-type]
        return JiraSyncService(cfg, token_manager=_mock_mgr())

    @pytest.mark.asyncio
    async def test_create_issue_basic(self) -> None:
        svc = self._make_service()
        svc.client.create_issue = AsyncMock(return_value={"key": "PROJ-99", "id": "10001"})
        task = SwarmTask(
            title="Fix bug",
            description="Something broken",
            task_type=TaskType.BUG,
            priority=TaskPriority.HIGH,
        )
        key = await svc.create_jira_issue(task)
        assert key == "PROJ-99"
        call_kw = svc.client.create_issue.call_args
        assert call_kw[1]["issue_type"] == "Bug"
        assert call_kw[1]["priority"] == "High"
        assert svc.stats.total_exported == 1

    @pytest.mark.asyncio
    async def test_create_jira_issue_maps_types(self) -> None:
        svc = self._make_service()
        svc.client.create_issue = AsyncMock(return_value={"key": "PROJ-1", "id": "1"})
        task = SwarmTask(
            title="New feature",
            task_type=TaskType.FEATURE,
            priority=TaskPriority.URGENT,
        )
        await svc.create_jira_issue(task)
        call_kw = svc.client.create_issue.call_args
        assert call_kw[1]["issue_type"] == "Story"
        assert call_kw[1]["priority"] == "Highest"

    @pytest.mark.asyncio
    async def test_create_jira_issue_when_disabled(self) -> None:
        svc = self._make_service(enabled=False)
        task = SwarmTask(title="Test")
        with pytest.raises(RuntimeError, match="not enabled"):
            await svc.create_jira_issue(task)

    def test_reverse_map_completeness(self) -> None:
        """All TaskType and TaskPriority enum values are covered."""
        for tt in TaskType:
            assert tt in _SWARM_TYPE_TO_JIRA, f"Missing {tt}"
        for tp in TaskPriority:
            assert tp in _SWARM_PRIORITY_TO_JIRA, f"Missing {tp}"
