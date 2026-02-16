"""Tests for cli.py — Click CLI commands."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from click.testing import CliRunner

from swarm.cli import _resolve_target, main
from swarm.config import GroupConfig, HiveConfig, WorkerConfig
from swarm.worker.state import WorkerState
from swarm.worker.worker import Worker


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture
def sample_config():
    """Config with 2 workers and 1 group for testing."""
    return HiveConfig(
        session_name="test",
        workers=[
            WorkerConfig(name="api", path="/tmp/api"),
            WorkerConfig(name="web", path="/tmp/web"),
        ],
        groups=[GroupConfig(name="backend", workers=["api"])],
    )


@pytest.fixture
def sample_config_file(tmp_path):
    """Write a valid config file and return its path."""
    api_dir = tmp_path / "api"
    api_dir.mkdir()
    web_dir = tmp_path / "web"
    web_dir.mkdir()
    config = tmp_path / "swarm.yaml"
    config.write_text(f"""
session_name: test
workers:
  - name: api
    path: {api_dir}
  - name: web
    path: {web_dir}
groups:
  - name: backend
    workers: [api]
""")
    return str(config)


# --- Help / Version ---


def test_help(runner):
    result = runner.invoke(main, ["--help"])
    assert result.exit_code == 0
    assert "Swarm" in result.output


def test_version(runner):
    result = runner.invoke(main, ["--version"])
    assert result.exit_code == 0
    assert "version" in result.output.lower()


# --- init ---


def test_init_skip_all(runner, monkeypatch):
    """init --skip-tmux --skip-hooks --skip-config still runs tmux check."""
    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/tmux")

    import subprocess

    mock_result = MagicMock()
    mock_result.stdout = "tmux 3.4"
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: mock_result)
    result = runner.invoke(main, ["init", "--skip-tmux", "--skip-hooks", "--skip-config"])
    assert result.exit_code == 0
    assert "Skipping tmux config" in result.output
    assert "Skipping hooks" in result.output
    assert "Skipping swarm.yaml" in result.output
    assert "System readiness" in result.output


def test_init_tmux_not_installed(runner, monkeypatch):
    """init should report tmux missing."""
    monkeypatch.setattr("shutil.which", lambda _: None)
    result = runner.invoke(main, ["init", "--skip-tmux", "--skip-hooks", "--skip-config"])
    assert result.exit_code == 0
    assert "FAIL" in result.output


def test_init_tmux_too_old(runner, monkeypatch):
    """init should report old tmux version."""
    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/tmux")

    import subprocess

    mock_result = MagicMock()
    mock_result.stdout = "tmux 2.9"
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: mock_result)
    result = runner.invoke(main, ["init", "--skip-tmux", "--skip-hooks", "--skip-config"])
    assert result.exit_code == 0
    assert "requires >= 3.2" in result.output


# --- _require_tmux ---


def test_require_tmux_not_installed(runner, monkeypatch):
    """_require_tmux should exit if tmux is not found."""
    from swarm.cli import _require_tmux

    monkeypatch.setattr("shutil.which", lambda _: None)
    with pytest.raises(SystemExit):
        _require_tmux()


def test_require_tmux_version_too_old(runner, monkeypatch):
    """_require_tmux should exit if tmux version is below minimum."""
    from swarm.cli import _require_tmux

    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/tmux")
    mock_result = MagicMock()
    mock_result.stdout = "tmux 2.9"
    monkeypatch.setattr("subprocess.run", lambda *a, **kw: mock_result)
    with pytest.raises(SystemExit):
        _require_tmux()


def test_require_tmux_unparseable_version(monkeypatch):
    """_require_tmux should proceed if version string can't be parsed."""
    from swarm.cli import _require_tmux

    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/tmux")
    mock_result = MagicMock()
    mock_result.stdout = "tmux next-gen"
    monkeypatch.setattr("subprocess.run", lambda *a, **kw: mock_result)
    # Should not raise
    _require_tmux()


def test_require_tmux_good_version(monkeypatch):
    """_require_tmux should pass with a valid tmux version."""
    from swarm.cli import _require_tmux

    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/tmux")
    mock_result = MagicMock()
    mock_result.stdout = "tmux 3.4"
    monkeypatch.setattr("subprocess.run", lambda *a, **kw: mock_result)
    _require_tmux()  # should not raise


# --- _resolve_target ---


def test_resolve_target_by_group_number(sample_config):
    """Resolve target by group number (1-indexed)."""
    name, workers = _resolve_target(sample_config, "1")
    assert name == "backend"
    assert len(workers) == 1
    assert workers[0].name == "api"


def test_resolve_target_by_worker_number(sample_config):
    """Resolve target by worker number (after groups)."""
    name, workers = _resolve_target(sample_config, "2")
    assert name == "api"
    assert len(workers) == 1


def test_resolve_target_by_worker_number_second(sample_config):
    """Resolve second worker by number."""
    name, workers = _resolve_target(sample_config, "3")
    assert name == "web"
    assert len(workers) == 1


def test_resolve_target_by_group_name(sample_config):
    """Resolve target by group name."""
    name, workers = _resolve_target(sample_config, "backend")
    assert name == "backend"
    assert workers is not None


def test_resolve_target_by_worker_name(sample_config):
    """Resolve target by worker name."""
    name, workers = _resolve_target(sample_config, "web")
    assert name == "web"
    assert len(workers) == 1


def test_resolve_target_not_found(sample_config):
    """Resolve target returns None for unknown name."""
    name, workers = _resolve_target(sample_config, "nonexistent")
    assert name == "nonexistent"
    assert workers is None


def test_resolve_target_number_out_of_range(sample_config):
    """Resolve target with out-of-range number."""
    name, workers = _resolve_target(sample_config, "99")
    # Falls through to name lookup, not found
    assert workers is None


# --- validate ---


def test_validate_no_config(runner, tmp_path, monkeypatch):
    """validate should report errors when no config and no discoverable workers."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    result = runner.invoke(main, ["validate"])
    assert result.exit_code == 1
    assert "error" in result.output.lower()


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


# --- tasks ---


def test_tasks_list_empty(runner, monkeypatch, tmp_path):
    """tasks list should show empty board."""
    monkeypatch.setattr("swarm.tasks.store._DEFAULT_PATH", tmp_path / "tasks.json")
    result = runner.invoke(main, ["tasks", "list"])
    assert result.exit_code == 0
    assert "No tasks" in result.output


def test_tasks_list_with_tasks(runner, monkeypatch, tmp_path):
    """tasks list should display existing tasks."""
    monkeypatch.setattr("swarm.tasks.store._DEFAULT_PATH", tmp_path / "tasks.json")
    # Create a task first
    runner.invoke(main, ["tasks", "create", "--title", "Fix bug"])
    result = runner.invoke(main, ["tasks", "list"])
    assert result.exit_code == 0
    assert "Fix bug" in result.output


def test_tasks_create(runner, monkeypatch, tmp_path):
    """tasks create should create a task."""
    monkeypatch.setattr("swarm.tasks.store._DEFAULT_PATH", tmp_path / "tasks.json")
    result = runner.invoke(main, ["tasks", "create", "--title", "Fix bug"])
    assert result.exit_code == 0
    assert "Created task" in result.output
    assert "Fix bug" in result.output


def test_tasks_create_with_priority(runner, monkeypatch, tmp_path):
    """tasks create should accept priority option."""
    monkeypatch.setattr("swarm.tasks.store._DEFAULT_PATH", tmp_path / "tasks.json")
    result = runner.invoke(main, ["tasks", "create", "--title", "Urgent fix", "--priority", "high"])
    assert result.exit_code == 0
    assert "high" in result.output


def test_tasks_create_no_title(runner):
    """tasks create without --title should fail."""
    result = runner.invoke(main, ["tasks", "create"])
    assert result.exit_code != 0


def test_tasks_assign(runner, monkeypatch, tmp_path):
    """tasks assign should assign a task to a worker."""
    monkeypatch.setattr("swarm.tasks.store._DEFAULT_PATH", tmp_path / "tasks.json")
    # Create task first
    create_result = runner.invoke(main, ["tasks", "create", "--title", "Fix bug"])
    # Extract task ID from output like "Created task [abc123]: Fix bug"
    task_id = create_result.output.split("[")[1].split("]")[0]
    result = runner.invoke(main, ["tasks", "assign", "--task-id", task_id, "--worker", "api"])
    assert result.exit_code == 0
    assert "Assigned" in result.output


def test_tasks_assign_missing_args(runner):
    """tasks assign without required args should fail."""
    result = runner.invoke(main, ["tasks", "assign"])
    assert result.exit_code != 0


def test_tasks_assign_not_found(runner, monkeypatch, tmp_path):
    """tasks assign with bad task ID should fail."""
    monkeypatch.setattr("swarm.tasks.store._DEFAULT_PATH", tmp_path / "tasks.json")
    result = runner.invoke(main, ["tasks", "assign", "--task-id", "bad", "--worker", "api"])
    assert result.exit_code != 0
    assert "not found" in result.output


def test_tasks_complete(runner, monkeypatch, tmp_path):
    """tasks complete should complete a task."""
    monkeypatch.setattr("swarm.tasks.store._DEFAULT_PATH", tmp_path / "tasks.json")
    # Create and assign task first
    create_result = runner.invoke(main, ["tasks", "create", "--title", "Fix bug"])
    task_id = create_result.output.split("[")[1].split("]")[0]
    runner.invoke(main, ["tasks", "assign", "--task-id", task_id, "--worker", "api"])
    result = runner.invoke(main, ["tasks", "complete", "--task-id", task_id])
    assert result.exit_code == 0
    assert "complete" in result.output.lower()


def test_tasks_complete_missing_id(runner, monkeypatch, tmp_path):
    """tasks complete without --task-id should fail."""
    monkeypatch.setattr("swarm.tasks.store._DEFAULT_PATH", tmp_path / "tasks.json")
    result = runner.invoke(main, ["tasks", "complete"])
    assert result.exit_code != 0


def test_tasks_complete_not_found(runner, monkeypatch, tmp_path):
    """tasks complete with bad task ID should fail."""
    monkeypatch.setattr("swarm.tasks.store._DEFAULT_PATH", tmp_path / "tasks.json")
    result = runner.invoke(main, ["tasks", "complete", "--task-id", "bad"])
    assert result.exit_code != 0
    assert "not found" in result.output


# --- status ---


def test_status_no_session(runner, monkeypatch):
    """status should handle no active hive."""
    monkeypatch.setattr("swarm.cli.load_config", lambda p=None: _make_config())
    with (
        patch("swarm.tmux.hive.find_swarm_session", new_callable=AsyncMock, return_value=None),
        patch("swarm.tmux.hive.discover_workers", new_callable=AsyncMock, return_value=[]),
    ):
        result = runner.invoke(main, ["status"])
        assert result.exit_code == 0
        assert "No active hive" in result.output


def test_status_with_workers(runner, monkeypatch):
    """status should display worker states."""
    monkeypatch.setattr("swarm.cli.load_config", lambda p=None: _make_config())
    mock_workers = [Worker(name="api", path="/tmp/api", pane_id="%0")]
    with (
        patch("swarm.tmux.hive.find_swarm_session", new_callable=AsyncMock, return_value="test"),
        patch(
            "swarm.tmux.hive.discover_workers",
            new_callable=AsyncMock,
            return_value=mock_workers,
        ),
        patch("swarm.tmux.cell.get_pane_command", new_callable=AsyncMock, return_value="claude"),
        patch(
            "swarm.tmux.cell.capture_pane",
            new_callable=AsyncMock,
            return_value="$ ",
        ),
    ):
        result = runner.invoke(main, ["status"])
        assert result.exit_code == 0
        assert "api" in result.output


# --- send ---


def test_send_to_worker(runner, monkeypatch, sample_config_file):
    """send should deliver message to matching worker."""
    mock_workers = [Worker(name="api", path="/tmp/api", pane_id="%0")]
    monkeypatch.setattr("swarm.cli.load_config", lambda p=None: _make_config())
    with (
        patch("swarm.tmux.hive.find_swarm_session", new_callable=AsyncMock, return_value="test"),
        patch(
            "swarm.tmux.hive.discover_workers",
            new_callable=AsyncMock,
            return_value=mock_workers,
        ),
        patch("swarm.tmux.cell.send_keys", new_callable=AsyncMock) as mock_send,
    ):
        result = runner.invoke(main, ["send", "api", "hello world"])
        assert result.exit_code == 0
        assert "Sent to api" in result.output
        mock_send.assert_called_once_with("%0", "hello world")


def test_send_to_all(runner, monkeypatch):
    """send all should deliver to every worker."""
    mock_workers = [
        Worker(name="api", path="/tmp/api", pane_id="%0"),
        Worker(name="web", path="/tmp/web", pane_id="%1"),
    ]
    monkeypatch.setattr("swarm.cli.load_config", lambda p=None: _make_config())
    with (
        patch("swarm.tmux.hive.find_swarm_session", new_callable=AsyncMock, return_value="test"),
        patch(
            "swarm.tmux.hive.discover_workers",
            new_callable=AsyncMock,
            return_value=mock_workers,
        ),
        patch("swarm.tmux.cell.send_keys", new_callable=AsyncMock) as mock_send,
    ):
        result = runner.invoke(main, ["send", "all", "deploy"])
        assert result.exit_code == 0
        assert "2 worker(s)" in result.output
        assert mock_send.call_count == 2


def test_send_to_group(runner, monkeypatch, sample_config):
    """send to a group name should deliver to all group members."""
    mock_workers = [
        Worker(name="api", path="/tmp/api", pane_id="%0"),
        Worker(name="web", path="/tmp/web", pane_id="%1"),
    ]
    monkeypatch.setattr("swarm.cli.load_config", lambda p=None: sample_config)
    with (
        patch("swarm.tmux.hive.find_swarm_session", new_callable=AsyncMock, return_value="test"),
        patch(
            "swarm.tmux.hive.discover_workers",
            new_callable=AsyncMock,
            return_value=mock_workers,
        ),
        patch("swarm.tmux.cell.send_keys", new_callable=AsyncMock) as mock_send,
    ):
        result = runner.invoke(main, ["send", "backend", "deploy"])
        assert result.exit_code == 0
        assert "Sent to api" in result.output
        assert "1 worker(s)" in result.output
        mock_send.assert_called_once_with("%0", "deploy")


def test_send_no_matching_worker(runner, monkeypatch):
    """send to unknown worker should report no match."""
    mock_workers = [Worker(name="api", path="/tmp/api", pane_id="%0")]
    monkeypatch.setattr("swarm.cli.load_config", lambda p=None: _make_config())
    with (
        patch("swarm.tmux.hive.find_swarm_session", new_callable=AsyncMock, return_value="test"),
        patch(
            "swarm.tmux.hive.discover_workers",
            new_callable=AsyncMock,
            return_value=mock_workers,
        ),
        patch("swarm.tmux.cell.send_keys", new_callable=AsyncMock),
    ):
        result = runner.invoke(main, ["send", "nonexistent", "hello"])
        assert result.exit_code == 0
        assert "No matching" in result.output


def test_send_no_hive(runner, monkeypatch):
    """send with no active hive should report error."""
    monkeypatch.setattr("swarm.cli.load_config", lambda p=None: _make_config())
    with (
        patch("swarm.tmux.hive.find_swarm_session", new_callable=AsyncMock, return_value=None),
        patch("swarm.tmux.hive.discover_workers", new_callable=AsyncMock, return_value=[]),
    ):
        result = runner.invoke(main, ["send", "api", "hello"])
        assert result.exit_code == 0
        assert "No active hive" in result.output


# --- kill ---


def test_kill_worker(runner, monkeypatch):
    """kill should kill the named worker."""
    mock_workers = [Worker(name="api", path="/tmp/api", pane_id="%0")]
    monkeypatch.setattr("swarm.cli.load_config", lambda p=None: _make_config())
    with (
        patch("swarm.tmux.hive.find_swarm_session", new_callable=AsyncMock, return_value="test"),
        patch(
            "swarm.tmux.hive.discover_workers",
            new_callable=AsyncMock,
            return_value=mock_workers,
        ),
        patch("swarm.worker.manager.kill_worker", new_callable=AsyncMock) as mock_kill,
    ):
        result = runner.invoke(main, ["kill", "api"])
        assert result.exit_code == 0
        assert "Killed worker: api" in result.output
        mock_kill.assert_called_once()


def test_kill_worker_not_found(runner, monkeypatch):
    """kill should report error for unknown worker."""
    mock_workers = [Worker(name="api", path="/tmp/api", pane_id="%0")]
    monkeypatch.setattr("swarm.cli.load_config", lambda p=None: _make_config())
    with (
        patch("swarm.tmux.hive.find_swarm_session", new_callable=AsyncMock, return_value="test"),
        patch(
            "swarm.tmux.hive.discover_workers",
            new_callable=AsyncMock,
            return_value=mock_workers,
        ),
    ):
        result = runner.invoke(main, ["kill", "nonexistent"])
        assert result.exit_code == 0
        assert "not found" in result.output


def test_kill_no_hive(runner, monkeypatch):
    """kill with no active hive should report error."""
    monkeypatch.setattr("swarm.cli.load_config", lambda p=None: _make_config())
    with (
        patch("swarm.tmux.hive.find_swarm_session", new_callable=AsyncMock, return_value=None),
        patch("swarm.tmux.hive.discover_workers", new_callable=AsyncMock, return_value=[]),
    ):
        result = runner.invoke(main, ["kill", "api"])
        assert result.exit_code == 0
        assert "No active hive" in result.output


# --- install-hooks ---


def test_install_hooks(runner):
    """install-hooks should call the install function."""
    with patch("swarm.hooks.install.install") as mock_install:
        result = runner.invoke(main, ["install-hooks"])
        assert result.exit_code == 0
        assert "Hooks installed" in result.output
        mock_install.assert_called_once_with(global_install=False)


def test_install_hooks_global(runner):
    """install-hooks --global should pass global_install=True."""
    with patch("swarm.hooks.install.install") as mock_install:
        result = runner.invoke(main, ["install-hooks", "--global"])
        assert result.exit_code == 0
        mock_install.assert_called_once_with(global_install=True)


# --- web start/stop/status ---


def test_web_start(runner, monkeypatch):
    """web start should delegate to webctl."""
    monkeypatch.setattr("swarm.cli.load_config", lambda p=None: _make_config())
    with patch("swarm.server.webctl.web_start", return_value=(True, "Started on :9090")):
        result = runner.invoke(main, ["web", "start"])
        assert result.exit_code == 0
        assert "Started" in result.output


def test_web_stop(runner):
    """web stop should delegate to webctl."""
    with patch("swarm.server.webctl.web_stop", return_value=(True, "Stopped")):
        result = runner.invoke(main, ["web", "stop"])
        assert result.exit_code == 0
        assert "Stopped" in result.output


def test_web_status_running(runner):
    """web status should show running PID."""
    with patch("swarm.server.webctl.web_is_running", return_value=12345):
        result = runner.invoke(main, ["web", "status"])
        assert result.exit_code == 0
        assert "12345" in result.output
        assert "running" in result.output.lower()


def test_web_status_not_running(runner):
    """web status should indicate when not running."""
    with patch("swarm.server.webctl.web_is_running", return_value=None):
        result = runner.invoke(main, ["web", "status"])
        assert result.exit_code == 0
        assert "not running" in result.output


# --- launch ---


@patch("swarm.cli._require_tmux")
def test_launch_no_args_shows_available(mock_tmux, runner, sample_config_file):
    """launch with no args and no default_group shows available groups."""
    result = runner.invoke(main, ["launch", "-c", sample_config_file])
    assert result.exit_code == 0
    assert "Groups:" in result.output
    assert "backend" in result.output


@patch("swarm.worker.manager.launch_hive", new_callable=AsyncMock)
@patch("swarm.cli._require_tmux")
def test_launch_all(mock_tmux, mock_launch, runner, sample_config_file):
    """launch -a should launch all workers."""
    result = runner.invoke(main, ["launch", "-a", "-c", sample_config_file])
    assert result.exit_code == 0
    assert "Hive launched" in result.output
    assert "2 workers" in result.output


@patch("swarm.worker.manager.launch_hive", new_callable=AsyncMock)
@patch("swarm.cli._require_tmux")
def test_launch_by_group_name(mock_tmux, mock_launch, runner, sample_config_file):
    """launch <group> should launch that group."""
    result = runner.invoke(main, ["launch", "backend", "-c", sample_config_file])
    assert result.exit_code == 0
    assert "1 workers" in result.output


@patch("swarm.worker.manager.launch_hive", new_callable=AsyncMock)
@patch("swarm.cli._require_tmux")
def test_launch_by_worker_name(mock_tmux, mock_launch, runner, sample_config_file):
    """launch <worker> should launch that single worker."""
    result = runner.invoke(main, ["launch", "web", "-c", sample_config_file])
    assert result.exit_code == 0
    assert "1 workers" in result.output


@patch("swarm.cli._require_tmux")
def test_launch_unknown_target(mock_tmux, runner, sample_config_file):
    """launch <unknown> should show available groups."""
    result = runner.invoke(main, ["launch", "nonexistent", "-c", sample_config_file])
    assert result.exit_code == 0
    assert "Unknown group or worker" in result.output


@patch("swarm.cli._require_tmux")
def test_launch_config_errors(mock_tmux, runner, tmp_path):
    """launch should fail if config has validation errors (duplicate worker name)."""
    config = tmp_path / "swarm.yaml"
    config.write_text(f"""
session_name: test
workers:
  - name: api
    path: {tmp_path}
  - name: api
    path: {tmp_path}
""")
    result = runner.invoke(main, ["launch", "-a", "-c", str(config)])
    assert result.exit_code != 0
    assert "Config error" in result.output


# --- launch numeric targets ---


@patch("swarm.worker.manager.launch_hive", new_callable=AsyncMock)
@patch("swarm.cli._require_tmux")
def test_launch_by_group_number(mock_tmux, mock_launch, runner, sample_config_file):
    """launch 1 should launch group at index 1 (backend)."""
    result = runner.invoke(main, ["launch", "1", "-c", sample_config_file])
    assert result.exit_code == 0
    assert "Hive launched" in result.output
    assert "1 workers" in result.output


@patch("swarm.worker.manager.launch_hive", new_callable=AsyncMock)
@patch("swarm.cli._require_tmux")
def test_launch_by_worker_number(mock_tmux, mock_launch, runner, sample_config_file):
    """launch 2 should launch worker at index 2 (first worker after 1 group)."""
    result = runner.invoke(main, ["launch", "2", "-c", sample_config_file])
    assert result.exit_code == 0
    assert "Hive launched" in result.output
    assert "1 workers" in result.output


@patch("swarm.cli._require_tmux")
def test_launch_number_out_of_range(mock_tmux, runner, sample_config_file):
    """launch with out-of-range number should show available groups."""
    result = runner.invoke(main, ["launch", "99", "-c", sample_config_file])
    assert result.exit_code == 0
    assert "Unknown group or worker" in result.output


# --- launch with default_group ---


@patch("swarm.worker.manager.launch_hive", new_callable=AsyncMock)
@patch("swarm.cli._require_tmux")
def test_launch_default_group(mock_tmux, mock_launch, runner, tmp_path):
    """launch with no args should use default_group if set."""
    api_dir = tmp_path / "api"
    api_dir.mkdir()
    config = tmp_path / "swarm.yaml"
    config.write_text(f"""
session_name: test
default_group: backend
workers:
  - name: api
    path: {api_dir}
groups:
  - name: backend
    workers: [api]
""")
    result = runner.invoke(main, ["launch", "-c", str(config)])
    assert result.exit_code == 0
    assert "Hive launched" in result.output


@patch("swarm.cli._require_tmux")
def test_launch_default_group_not_found(mock_tmux, runner, tmp_path):
    """launch with missing default_group should fail validation."""
    api_dir = tmp_path / "api"
    api_dir.mkdir()
    config = tmp_path / "swarm.yaml"
    config.write_text(f"""
session_name: test
default_group: nonexistent
workers:
  - name: api
    path: {api_dir}
groups:
  - name: backend
    workers: [api]
""")
    result = runner.invoke(main, ["launch", "-c", str(config)])
    assert result.exit_code != 0
    assert "Config error" in result.output


# --- serve ---


@patch("swarm.server.daemon.run_daemon", new_callable=AsyncMock)
def test_serve_invokes_run_daemon(mock_run, runner, sample_config_file):
    """serve should call run_daemon with config."""
    result = runner.invoke(main, ["serve", "-c", sample_config_file])
    assert result.exit_code == 0


@patch("swarm.server.daemon.run_daemon", new_callable=AsyncMock)
def test_serve_with_session_override(mock_run, runner, sample_config_file):
    """serve --session should override the session name."""
    result = runner.invoke(main, ["serve", "-c", sample_config_file, "-s", "custom"])
    assert result.exit_code == 0


# --- daemon ---


@patch("swarm.server.daemon.run_daemon", new_callable=AsyncMock)
def test_daemon_command(mock_run, runner, sample_config_file):
    """daemon subcommand should call run_daemon."""
    result = runner.invoke(main, ["daemon", "-c", sample_config_file])
    assert result.exit_code == 0


@patch("swarm.server.daemon.run_daemon", new_callable=AsyncMock)
def test_daemon_with_session(mock_run, runner, sample_config_file):
    """daemon --session should override session name."""
    result = runner.invoke(main, ["daemon", "-c", sample_config_file, "-s", "mysession"])
    assert result.exit_code == 0


# --- log level ---


def test_log_level_option(runner, tmp_path, monkeypatch):
    """--log-level should be accepted without error."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    result = runner.invoke(main, ["--log-level", "DEBUG", "validate"])
    # Validation may fail (no config), but --log-level should be accepted
    assert "Error: Invalid value" not in result.output


# --- check-states ---


def test_check_states_all_match(runner, monkeypatch):
    """check-states should show checkmarks when stored and fresh states agree."""
    monkeypatch.setattr("swarm.cli.load_config", lambda p=None: _make_config())
    mock_workers = [
        Worker(name="api", path="/tmp/api", pane_id="%0", state=WorkerState.RESTING),
        Worker(name="web", path="/tmp/web", pane_id="%1", state=WorkerState.BUZZING),
    ]
    with (
        patch("swarm.tmux.hive.find_swarm_session", new_callable=AsyncMock, return_value="test"),
        patch(
            "swarm.tmux.hive.discover_workers",
            new_callable=AsyncMock,
            return_value=mock_workers,
        ),
        patch(
            "swarm.tmux.cell.get_pane_command",
            new_callable=AsyncMock,
            side_effect=["claude", "claude"],
        ),
        patch(
            "swarm.tmux.cell.capture_pane",
            new_callable=AsyncMock,
            side_effect=[
                "idle prompt\n> Try something\n? for shortcuts",
                "working...\nesc to interrupt",
            ],
        ),
    ):
        result = runner.invoke(main, ["check-states"])
        assert result.exit_code == 0
        assert "\u2713" in result.output
        assert "\u2717" not in result.output


def test_check_states_mismatch(runner, monkeypatch):
    """check-states should show cross and pane content for mismatches."""
    monkeypatch.setattr("swarm.cli.load_config", lambda p=None: _make_config())
    mock_workers = [
        Worker(name="web", path="/tmp/web", pane_id="%1", state=WorkerState.BUZZING),
    ]
    with (
        patch("swarm.tmux.hive.find_swarm_session", new_callable=AsyncMock, return_value="test"),
        patch(
            "swarm.tmux.hive.discover_workers",
            new_callable=AsyncMock,
            return_value=mock_workers,
        ),
        patch(
            "swarm.tmux.cell.get_pane_command",
            new_callable=AsyncMock,
            return_value="claude",
        ),
        patch(
            "swarm.tmux.cell.capture_pane",
            new_callable=AsyncMock,
            return_value='idle prompt\n> Try "how does foo work"\n? for shortcuts',
        ),
    ):
        result = runner.invoke(main, ["check-states"])
        assert result.exit_code == 0
        assert "\u2717" in result.output
        assert "stored=BUZZING" in result.output
        assert "fresh=RESTING" in result.output
        assert "Last 5 lines:" in result.output
        assert "| " in result.output


# --- helpers ---


def _make_config():
    return HiveConfig(session_name="nonexistent")
