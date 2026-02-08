"""Tests for cli.py — Click CLI commands."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from click.testing import CliRunner

from swarm.cli import main


@pytest.fixture
def runner():
    return CliRunner()


def test_help(runner):
    result = runner.invoke(main, ["--help"])
    assert result.exit_code == 0
    assert "Swarm" in result.output


def test_version(runner):
    result = runner.invoke(main, ["--version"])
    assert result.exit_code == 0
    assert "version" in result.output.lower()


def test_validate_no_config(runner, tmp_path, monkeypatch):
    """validate should handle missing config gracefully."""
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(main, ["validate"])
    # Auto-detect config returns defaults which are valid
    assert result.exit_code == 0


def test_validate_valid_config(runner, tmp_path):
    """validate should succeed with a valid config."""
    config = tmp_path / "swarm.yaml"
    config.write_text("""
session_name: test
workers:
  - name: api
    path: /tmp
groups:
  - name: all
    workers: [api]
""")
    result = runner.invoke(main, ["validate", "-c", str(config)])
    assert result.exit_code == 0
    assert "Config OK" in result.output


def test_validate_invalid_config(runner, tmp_path):
    """validate should report errors in invalid config."""
    config = tmp_path / "swarm.yaml"
    config.write_text("""
session_name: test
workers:
  - name: api
    path: /tmp
  - name: api
    path: /tmp
""")
    result = runner.invoke(main, ["validate", "-c", str(config)])
    assert result.exit_code != 0
    assert "Duplicate worker name" in result.output


def test_tasks_list_empty(runner, monkeypatch, tmp_path):
    """tasks list should show empty board."""
    monkeypatch.setattr("swarm.tasks.store._DEFAULT_PATH", tmp_path / "tasks.json")
    result = runner.invoke(main, ["tasks", "list"])
    assert result.exit_code == 0
    assert "No tasks" in result.output


def test_tasks_create(runner, monkeypatch, tmp_path):
    """tasks create should create a task."""
    monkeypatch.setattr("swarm.tasks.store._DEFAULT_PATH", tmp_path / "tasks.json")
    result = runner.invoke(main, ["tasks", "create", "--title", "Fix bug"])
    assert result.exit_code == 0
    assert "Created task" in result.output
    assert "Fix bug" in result.output


def test_tasks_create_no_title(runner):
    """tasks create without --title should fail."""
    result = runner.invoke(main, ["tasks", "create"])
    assert result.exit_code != 0


def test_tasks_assign_missing_args(runner):
    """tasks assign without required args should fail."""
    result = runner.invoke(main, ["tasks", "assign"])
    assert result.exit_code != 0


def test_status_no_session(runner, monkeypatch):
    """status should handle no active hive."""
    monkeypatch.setattr(
        "swarm.cli.load_config",
        lambda p=None: _make_config(),
    )
    with (
        patch("swarm.tmux.hive.find_swarm_session", new_callable=AsyncMock, return_value=None),
        patch("swarm.tmux.hive.discover_workers", new_callable=AsyncMock, return_value=[]),
    ):
        result = runner.invoke(main, ["status"])
        assert result.exit_code == 0
        assert "No active hive" in result.output


def _make_config():
    from swarm.config import HiveConfig

    return HiveConfig(session_name="nonexistent")
