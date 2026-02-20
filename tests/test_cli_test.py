"""Tests for the ``swarm test`` CLI command and helpers."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from click.testing import CliRunner

from swarm.cli import _setup_test_config, main
from swarm.config import GroupConfig, HiveConfig, TestConfig, WorkerConfig


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture
def sample_config():
    return HiveConfig(
        session_name="test",
        workers=[
            WorkerConfig(name="api", path="/tmp/api"),
            WorkerConfig(name="web", path="/tmp/web"),
        ],
        groups=[GroupConfig(name="backend", workers=["api"])],
    )


# --- TestConfig port ---


class TestTestConfigPort:
    """Verify TestConfig.port defaults and parsing."""

    def test_default_port(self):
        tc = TestConfig()
        assert tc.port == 9091

    def test_custom_port(self):
        tc = TestConfig(port=9999)
        assert tc.port == 9999

    def test_port_parsed_from_yaml(self, tmp_path):
        """Port is read from the test: section in swarm.yaml."""
        from swarm.config import load_config

        proj = tmp_path / "proj"
        proj.mkdir()
        cfg_file = tmp_path / "swarm.yaml"
        cfg_file.write_text(
            f"""
session_name: test
workers:
  - name: w1
    path: {proj}
test:
  port: 8888
"""
        )
        cfg = load_config(str(cfg_file))
        assert cfg.test.port == 8888

    def test_port_default_when_not_in_yaml(self, tmp_path):
        """When test.port is not in YAML, it defaults to 9091."""
        from swarm.config import load_config

        proj = tmp_path / "proj"
        proj.mkdir()
        cfg_file = tmp_path / "swarm.yaml"
        cfg_file.write_text(
            f"""
session_name: test
workers:
  - name: w1
    path: {proj}
"""
        )
        cfg = load_config(str(cfg_file))
        assert cfg.test.port == 9091

    def test_serialize_includes_port(self):
        """Serializer includes port when non-default."""
        from swarm.config import _serialize_test

        tc = TestConfig(port=7777)
        result = _serialize_test(tc)
        assert result is not None
        assert result["port"] == 7777

    def test_serialize_returns_dict_for_defaults(self):
        """Serializer always returns a dict (templates access unconditionally)."""
        from swarm.config import _serialize_test

        tc = TestConfig()
        result = _serialize_test(tc)
        assert isinstance(result, dict)
        assert result["port"] == 9091


# --- _setup_test_config ---


class TestSetupTestConfig:
    """Test the _setup_test_config helper."""

    @patch("swarm.testing.project.TestProjectManager")
    def test_configures_single_worker(self, mock_mgr_cls, sample_config):
        """Helper creates a single test-worker and enables test mode."""
        mock_mgr = MagicMock()
        mock_mgr.setup.return_value = Path("/tmp/test-project")
        mock_mgr_cls.return_value = mock_mgr

        with patch("swarm.cli.click"):
            mgr, project_dir = _setup_test_config(sample_config)

        assert project_dir == Path("/tmp/test-project")
        assert len(sample_config.workers) == 1
        assert sample_config.workers[0].name == "test-worker"
        assert sample_config.test.enabled is True
        assert sample_config.drones.auto_stop_on_complete is True

    @patch("swarm.testing.project.TestProjectManager")
    def test_setup_failure_exits(self, mock_mgr_cls, sample_config):
        """Helper exits with code 1 if fixture dir is missing."""
        mock_mgr = MagicMock()
        mock_mgr.setup.side_effect = FileNotFoundError("not found")
        mock_mgr_cls.return_value = mock_mgr

        with pytest.raises(SystemExit) as exc_info:
            _setup_test_config(sample_config)
        assert exc_info.value.code == 1


# --- swarm test command ---


class TestTestCommand:
    """Test the ``swarm test`` CLI command."""

    def test_help(self, runner):
        result = runner.invoke(main, ["test", "--help"])
        assert result.exit_code == 0
        assert "--port" in result.output
        assert "--timeout" in result.output
        assert "--no-cleanup" in result.output

    @patch("swarm.server.daemon.run_test_daemon", new_callable=AsyncMock)
    @patch("swarm.cli._setup_test_config")
    @patch("swarm.cli.load_config")
    def test_success_exit_0(self, mock_load, mock_setup, mock_daemon, runner, tmp_path):
        """Successful test run exits with code 0."""
        report = tmp_path / "report.md"
        report.write_text("## Summary\nAll tests passed.\n## Details\n...")

        cfg = HiveConfig(
            session_name="test",
            workers=[WorkerConfig(name="w", path="/tmp/w")],
        )
        mock_load.return_value = cfg
        mock_setup.return_value = (MagicMock(), Path("/tmp/test-proj"))
        mock_daemon.return_value = report

        result = runner.invoke(main, ["test"])
        assert result.exit_code == 0
        assert "All tests passed" in result.output

    @patch("swarm.server.daemon.run_test_daemon", new_callable=AsyncMock)
    @patch("swarm.cli._setup_test_config")
    @patch("swarm.cli.load_config")
    def test_timeout_exit_2(self, mock_load, mock_setup, mock_daemon, runner):
        """Timeout exits with code 2."""
        cfg = HiveConfig(
            session_name="test",
            workers=[WorkerConfig(name="w", path="/tmp/w")],
        )
        mock_load.return_value = cfg
        mock_setup.return_value = (MagicMock(), Path("/tmp/test-proj"))
        mock_daemon.side_effect = TimeoutError("timed out")

        result = runner.invoke(main, ["test", "--timeout", "10"])
        assert result.exit_code == 2

    @patch("swarm.server.daemon.run_test_daemon", new_callable=AsyncMock)
    @patch("swarm.cli._setup_test_config")
    @patch("swarm.cli.load_config")
    @patch("swarm.cli._cleanup_test")
    def test_no_cleanup_skips(self, mock_cleanup, mock_load, mock_setup, mock_daemon, runner):
        """--no-cleanup flag skips cleanup."""
        cfg = HiveConfig(
            session_name="test",
            workers=[WorkerConfig(name="w", path="/tmp/w")],
        )
        mock_load.return_value = cfg
        mock_setup.return_value = (MagicMock(), Path("/tmp/test-proj"))
        mock_daemon.return_value = None

        result = runner.invoke(main, ["test", "--no-cleanup"])
        assert result.exit_code == 0
        mock_cleanup.assert_not_called()


# --- _load_test_tasks deduplication ---


class TestLoadTestTasksDedup:
    """Verify that _load_test_tasks removes stale tasks from previous runs."""

    def test_stale_tasks_removed_before_loading(self):
        """Tasks with matching titles from previous runs are removed first."""
        from swarm.tasks.board import TaskBoard
        from swarm.tasks.task import SwarmTask, TaskPriority, TaskType

        board = TaskBoard()
        # Simulate stale tasks from a previous test run
        stale = SwarmTask(title="Fix divide-by-zero bug in calculator")
        board.add(stale)
        stale2 = SwarmTask(title="Add multiply function to calculator")
        board.add(stale2)

        assert len(board.all_tasks) == 2

        # Now create a mock daemon and call _load_test_tasks
        daemon = MagicMock()
        daemon.task_board = board
        daemon.create_task = MagicMock(
            side_effect=lambda **kw: board.create(
                title=kw["title"],
                description=kw.get("description", ""),
                priority=kw.get("priority", TaskPriority.NORMAL),
                task_type=kw.get("task_type", TaskType.CHORE),
                tags=kw.get("tags"),
            )
        )

        from swarm.server.daemon import SwarmDaemon

        # Call _load_test_tasks as an unbound method with our mock
        SwarmDaemon._load_test_tasks(daemon)

        # All tasks should have unique titles (no duplicates)
        titles = [t.title for t in board.all_tasks]
        assert len(titles) == len(set(titles)), f"Duplicate titles found: {titles}"

        # The stale task IDs should be gone
        assert board.get(stale.id) is None
        assert board.get(stale2.id) is None

    def test_no_stale_tasks_still_loads(self):
        """When no stale tasks exist, loading works normally."""
        from swarm.tasks.board import TaskBoard
        from swarm.tasks.task import TaskPriority, TaskType

        board = TaskBoard()
        assert len(board.all_tasks) == 0

        daemon = MagicMock()
        daemon.task_board = board
        daemon.create_task = MagicMock(
            side_effect=lambda **kw: board.create(
                title=kw["title"],
                description=kw.get("description", ""),
                priority=kw.get("priority", TaskPriority.NORMAL),
                task_type=kw.get("task_type", TaskType.CHORE),
                tags=kw.get("tags"),
            )
        )

        from swarm.server.daemon import SwarmDaemon

        SwarmDaemon._load_test_tasks(daemon)

        # Should have loaded tasks (exact count depends on fixture)
        assert len(board.all_tasks) > 0
        titles = [t.title for t in board.all_tasks]
        assert len(titles) == len(set(titles))


# --- _wire_test_console ---


class TestWireTestConsole:
    """Verify _wire_test_console wires handlers without error."""

    def test_wires_pilot_events(self):
        from swarm.server.daemon import _wire_test_console

        daemon = MagicMock()
        daemon.pilot = MagicMock()
        daemon.pilot.on_state_changed = MagicMock()
        daemon.pilot.on_task_assigned = MagicMock()
        daemon.pilot.on_workers_changed = MagicMock()
        daemon.pilot.on_hive_empty = MagicMock()
        daemon.pilot.on_hive_complete = MagicMock()
        daemon.pilot.on_escalate = MagicMock()
        daemon.pilot._emit_decisions = False
        daemon.on = MagicMock()
        daemon.task_board = MagicMock()

        _wire_test_console(daemon)

        daemon.pilot.on_state_changed.assert_called_once()
        daemon.pilot.on_task_assigned.assert_called_once()
        daemon.pilot.on_workers_changed.assert_called_once()
        daemon.pilot.on_hive_empty.assert_called_once()
        daemon.pilot.on_hive_complete.assert_called_once()
        daemon.pilot.on_escalate.assert_called_once()

    def test_no_pilot_no_crash(self):
        from swarm.server.daemon import _wire_test_console

        daemon = MagicMock()
        daemon.pilot = None
        daemon.on = MagicMock()
        daemon.task_board = MagicMock()

        _wire_test_console(daemon)  # should not raise


# --- _print_test_banner ---


class TestPrintTestBanner:
    """Smoke test for _print_test_banner."""

    def test_no_crash(self, capsys):
        from swarm.server.daemon import _print_test_banner

        daemon = MagicMock()
        daemon.workers = [MagicMock()]
        daemon.task_board.all_tasks = [MagicMock(), MagicMock()]
        daemon.config.session_name = "swarm-test-abc123"

        _print_test_banner(daemon, "0.0.0.0", 9091, 300)

        captured = capsys.readouterr()
        assert "9091" in captured.out
        assert "swarm-test-abc123" in captured.out
        assert "300" in captured.out
