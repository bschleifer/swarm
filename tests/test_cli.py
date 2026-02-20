"""Tests for cli.py — Click CLI commands."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from click.testing import CliRunner

from swarm.cli import _resolve_target, main
from swarm.config import GroupConfig, HiveConfig, WorkerConfig


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
    """init --skip-hooks --skip-config still runs system checks."""
    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/tmux")
    monkeypatch.setattr("swarm.service.is_wsl", lambda: False)

    import subprocess

    mock_result = MagicMock()
    mock_result.stdout = "tmux 3.4"
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: mock_result)
    result = runner.invoke(main, ["init", "--skip-hooks", "--skip-config"])
    assert result.exit_code == 0
    assert "Skipping hooks" in result.output
    assert "Skipping swarm.yaml" in result.output
    assert "System readiness" in result.output


def test_init_writes_api_password(runner, monkeypatch, tmp_path):
    """init should prompt for API password and write it to config."""
    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/tmux")
    monkeypatch.setattr("swarm.service.is_wsl", lambda: False)

    import subprocess

    mock_result = MagicMock()
    mock_result.stdout = "tmux 3.4"
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: mock_result)

    # Create a fake project dir with a git repo
    project_dir = tmp_path / "projects" / "myapp"
    project_dir.mkdir(parents=True)
    (project_dir / ".git").mkdir()

    out_path = str(tmp_path / "swarm.yaml")

    # Input: "a" for all workers, then "mySecret" for password
    result = runner.invoke(
        main,
        ["init", "--skip-hooks", "-d", str(tmp_path / "projects"), "-o", out_path],
        input="a\nmySecret\n",
    )
    assert result.exit_code == 0

    import yaml

    data = yaml.safe_load(Path(out_path).read_text())
    assert data["api_password"] == "mySecret"


def test_init_skips_api_password_when_empty(runner, monkeypatch, tmp_path):
    """init should omit api_password when user presses Enter (empty)."""
    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/tmux")
    monkeypatch.setattr("swarm.service.is_wsl", lambda: False)

    import subprocess

    mock_result = MagicMock()
    mock_result.stdout = "tmux 3.4"
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: mock_result)

    project_dir = tmp_path / "projects" / "myapp"
    project_dir.mkdir(parents=True)
    (project_dir / ".git").mkdir()

    out_path = str(tmp_path / "swarm.yaml")

    # Input: "a" for all workers, then empty for no password
    result = runner.invoke(
        main,
        ["init", "--skip-hooks", "-d", str(tmp_path / "projects"), "-o", out_path],
        input="a\n\n",
    )
    assert result.exit_code == 0

    import yaml

    data = yaml.safe_load(Path(out_path).read_text())
    assert "api_password" not in data


def test_init_backs_up_existing_config(runner, monkeypatch, tmp_path):
    """init should back up existing config before overwriting."""
    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/tmux")
    monkeypatch.setattr("swarm.service.is_wsl", lambda: False)

    import subprocess

    mock_result = MagicMock()
    mock_result.stdout = "tmux 3.4"
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: mock_result)

    project_dir = tmp_path / "projects" / "myapp"
    project_dir.mkdir(parents=True)
    (project_dir / ".git").mkdir()

    out_path = tmp_path / "swarm.yaml"
    out_path.write_text("# old config\nworkers:\n  - name: old\n    path: /old\n")

    # Input: "f" for fresh, "a" for all workers, then empty password
    args = [
        "init",
        "--skip-hooks",
        "-d",
        str(tmp_path / "projects"),
        "-o",
        str(out_path),
    ]
    result = runner.invoke(main, args, input="f\na\n\n")
    assert result.exit_code == 0
    assert "Backed up" in result.output

    # Backup file should exist
    backup = tmp_path / "swarm.yaml.bak"
    assert backup.exists()
    assert "old config" in backup.read_text()

    # New config should have the new worker
    import yaml

    data = yaml.safe_load(out_path.read_text())
    assert data["workers"][0]["name"] == "myapp"


def test_init_ports_settings_from_existing_config(runner, monkeypatch, tmp_path):
    """init should port settings from existing config when user chooses to."""
    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/tmux")
    monkeypatch.setattr("swarm.service.is_wsl", lambda: False)

    import subprocess

    mock_result = MagicMock()
    mock_result.stdout = "tmux 3.4"
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: mock_result)

    project_dir = tmp_path / "projects" / "myapp"
    project_dir.mkdir(parents=True)
    (project_dir / ".git").mkdir()

    # Existing config with custom settings
    out_path = tmp_path / "swarm.yaml"
    import yaml

    old_config = {
        "session_name": "swarm",
        "projects_dir": str(tmp_path / "projects"),
        "api_password": "oldSecret",
        "port": 8080,
        "panes_per_window": 6,
        "queen": {"cooldown": 120, "enabled": True, "min_confidence": 0.9},
        "drones": {"escalation_threshold": 90, "poll_interval": 15},
        "notifications": {"desktop": False, "terminal_bell": False},
        "workers": [
            {"name": "old-worker", "path": "/old/path"},
        ],
        "groups": [{"name": "all", "workers": ["old-worker"]}],
    }
    out_path.write_text(yaml.dump(old_config))

    # Input: "p" to port settings, "a" for all workers
    args = [
        "init",
        "--skip-hooks",
        "-d",
        str(tmp_path / "projects"),
        "-o",
        str(out_path),
    ]
    result = runner.invoke(main, args, input="p\na\n")
    assert result.exit_code == 0

    data = yaml.safe_load(out_path.read_text())
    # New workers from scan
    assert data["workers"][0]["name"] == "myapp"
    # Ported settings from old config
    assert data["api_password"] == "oldSecret"
    assert data["port"] == 8080
    assert data["panes_per_window"] == 6
    assert data["queen"]["cooldown"] == 120
    assert data["drones"]["escalation_threshold"] == 90
    assert data["notifications"]["desktop"] is False


def test_init_fresh_overwrites_existing_config(runner, monkeypatch, tmp_path):
    """init with 'f' (fresh) should discard old settings."""
    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/tmux")
    monkeypatch.setattr("swarm.service.is_wsl", lambda: False)

    import subprocess

    mock_result = MagicMock()
    mock_result.stdout = "tmux 3.4"
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: mock_result)

    project_dir = tmp_path / "projects" / "myapp"
    project_dir.mkdir(parents=True)
    (project_dir / ".git").mkdir()

    out_path = tmp_path / "swarm.yaml"
    import yaml

    old_config = {
        "session_name": "swarm",
        "api_password": "oldSecret",
        "port": 8080,
        "workers": [{"name": "old", "path": "/old"}],
        "groups": [{"name": "all", "workers": ["old"]}],
    }
    out_path.write_text(yaml.dump(old_config))

    # Input: "f" for fresh, "a" for all workers, empty password
    args = [
        "init",
        "--skip-hooks",
        "-d",
        str(tmp_path / "projects"),
        "-o",
        str(out_path),
    ]
    result = runner.invoke(main, args, input="f\na\n\n")
    assert result.exit_code == 0

    data = yaml.safe_load(out_path.read_text())
    assert data["workers"][0]["name"] == "myapp"
    # Old settings should NOT be ported
    assert "api_password" not in data
    assert data.get("port") is None or "port" not in data


def test_init_keep_existing_config(runner, monkeypatch, tmp_path):
    """init with 'k' (keep) should skip config generation entirely."""
    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/tmux")
    monkeypatch.setattr("swarm.service.is_wsl", lambda: False)

    import subprocess

    mock_result = MagicMock()
    mock_result.stdout = "tmux 3.4"
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: mock_result)

    project_dir = tmp_path / "projects" / "myapp"
    project_dir.mkdir(parents=True)
    (project_dir / ".git").mkdir()

    out_path = tmp_path / "swarm.yaml"
    original_content = "# my custom config\nworkers: []\n"
    out_path.write_text(original_content)

    args = [
        "init",
        "--skip-hooks",
        "-d",
        str(tmp_path / "projects"),
        "-o",
        str(out_path),
    ]
    result = runner.invoke(main, args, input="k\n")
    assert result.exit_code == 0
    assert "Keeping existing" in result.output
    # Config should be unchanged
    assert out_path.read_text() == original_content


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


def test_status_no_daemon(runner, monkeypatch):
    """status should handle no running daemon."""
    monkeypatch.setattr("swarm.cli.load_config", lambda p=None: _make_config())

    mock_get = AsyncMock(side_effect=ConnectionError("daemon not running"))
    with patch("swarm.cli._api_get", mock_get):
        result = runner.invoke(main, ["status"])
        # The status command raises SystemExit(1) on connection error
        assert result.exit_code != 0
        assert "Cannot reach daemon" in result.output


def test_status_with_workers(runner, monkeypatch):
    """status should display worker states from the daemon API."""
    monkeypatch.setattr("swarm.cli.load_config", lambda p=None: _make_config())

    mock_response = {
        "workers": [
            {"name": "api", "state": "resting", "state_duration": 10.0, "revive_count": 0},
        ]
    }

    mock_get = AsyncMock(return_value=mock_response)
    with patch("swarm.cli._api_get", mock_get):
        result = runner.invoke(main, ["status"])
        assert result.exit_code == 0
        assert "api" in result.output
        assert "resting" in result.output


def test_status_no_workers(runner, monkeypatch):
    """status should report when no workers are registered."""
    monkeypatch.setattr("swarm.cli.load_config", lambda p=None: _make_config())

    mock_get = AsyncMock(return_value={"workers": []})
    with patch("swarm.cli._api_get", mock_get):
        result = runner.invoke(main, ["status"])
        assert result.exit_code == 0
        assert "No workers" in result.output


# --- send ---


def test_send_to_worker(runner, monkeypatch):
    """send should deliver message to matching worker via daemon API."""
    monkeypatch.setattr("swarm.cli.load_config", lambda p=None: _make_config())

    mock_get = AsyncMock(return_value={"workers": [{"name": "api"}]})
    mock_post = AsyncMock(return_value={"ok": True})

    with patch("swarm.cli._api_get", mock_get), patch("swarm.cli._api_post", mock_post):
        result = runner.invoke(main, ["send", "api", "hello world"])
        assert result.exit_code == 0
        assert "Sent to api" in result.output
        assert "1 worker(s)" in result.output


def test_send_to_all(runner, monkeypatch):
    """send all should deliver to every worker via daemon API."""
    monkeypatch.setattr("swarm.cli.load_config", lambda p=None: _make_config())

    mock_get = AsyncMock(return_value={"workers": [{"name": "api"}, {"name": "web"}]})
    mock_post = AsyncMock(return_value={"ok": True})

    with patch("swarm.cli._api_get", mock_get), patch("swarm.cli._api_post", mock_post):
        result = runner.invoke(main, ["send", "all", "deploy"])
        assert result.exit_code == 0
        assert "2 worker(s)" in result.output


def test_send_to_group(runner, monkeypatch, sample_config):
    """send to a group name should deliver to all group members via daemon API."""
    monkeypatch.setattr("swarm.cli.load_config", lambda p=None: sample_config)

    mock_get = AsyncMock(return_value={"workers": [{"name": "api"}, {"name": "web"}]})
    mock_post = AsyncMock(return_value={"ok": True})

    with patch("swarm.cli._api_get", mock_get), patch("swarm.cli._api_post", mock_post):
        result = runner.invoke(main, ["send", "backend", "deploy"])
        assert result.exit_code == 0
        assert "Sent to" in result.output
        assert "1 worker(s)" in result.output


def test_send_no_matching_worker(runner, monkeypatch):
    """send to unknown worker should report no match."""
    monkeypatch.setattr("swarm.cli.load_config", lambda p=None: _make_config())

    mock_get = AsyncMock(return_value={"workers": [{"name": "api"}]})

    with patch("swarm.cli._api_get", mock_get):
        result = runner.invoke(main, ["send", "nonexistent", "hello"])
        assert result.exit_code == 0
        assert "No matching" in result.output


def test_send_no_daemon(runner, monkeypatch):
    """send with no running daemon should report error."""
    monkeypatch.setattr("swarm.cli.load_config", lambda p=None: _make_config())

    mock_get = AsyncMock(side_effect=ConnectionError("daemon not running"))

    with patch("swarm.cli._api_get", mock_get):
        result = runner.invoke(main, ["send", "api", "hello"])
        # The send command raises SystemExit(1) on connection error
        assert result.exit_code != 0
        assert "Cannot reach daemon" in result.output


# --- kill ---


def test_kill_worker(runner, monkeypatch):
    """kill should kill the named worker via daemon API."""
    monkeypatch.setattr("swarm.cli.load_config", lambda p=None: _make_config())

    mock_post = AsyncMock(return_value={"ok": True})
    with patch("swarm.cli._api_post", mock_post):
        result = runner.invoke(main, ["kill", "api"])
        assert result.exit_code == 0
        assert "Killed worker: api" in result.output


def test_kill_worker_not_found(runner, monkeypatch):
    """kill should report error for unknown worker."""
    monkeypatch.setattr("swarm.cli.load_config", lambda p=None: _make_config())

    mock_post = AsyncMock(side_effect=Exception("Worker not found"))
    with patch("swarm.cli._api_post", mock_post):
        result = runner.invoke(main, ["kill", "nonexistent"])
        # The kill command raises SystemExit(1) on failure
        assert result.exit_code != 0
        assert "Failed to kill" in result.output


def test_kill_no_daemon(runner, monkeypatch):
    """kill with no running daemon should report error."""
    monkeypatch.setattr("swarm.cli.load_config", lambda p=None: _make_config())

    mock_post = AsyncMock(side_effect=ConnectionError("daemon not running"))
    with patch("swarm.cli._api_post", mock_post):
        result = runner.invoke(main, ["kill", "api"])
        # The kill command raises SystemExit(1) on failure
        assert result.exit_code != 0
        assert "Failed to kill" in result.output


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


def test_launch_no_args_shows_available(runner, sample_config_file):
    """launch with no args and no default_group shows available groups."""
    result = runner.invoke(main, ["launch", "-c", sample_config_file])
    assert result.exit_code == 0
    assert "Groups:" in result.output
    assert "backend" in result.output


def test_launch_all(runner, sample_config_file):
    """launch -a should launch all workers."""
    mock_post = AsyncMock(return_value={"launched": ["api", "web"]})
    with patch("swarm.cli._api_post", mock_post):
        result = runner.invoke(main, ["launch", "-a", "-c", sample_config_file])
    assert result.exit_code == 0
    assert "Launched 2 worker(s)" in result.output


def test_launch_by_group_name(runner, sample_config_file):
    """launch <group> should launch that group."""
    mock_post = AsyncMock(return_value={"launched": ["api"]})
    with patch("swarm.cli._api_post", mock_post):
        result = runner.invoke(main, ["launch", "backend", "-c", sample_config_file])
    assert result.exit_code == 0
    assert "Launched 1 worker(s)" in result.output


def test_launch_by_worker_name(runner, sample_config_file):
    """launch <worker> should launch that single worker."""
    mock_post = AsyncMock(return_value={"launched": ["web"]})
    with patch("swarm.cli._api_post", mock_post):
        result = runner.invoke(main, ["launch", "web", "-c", sample_config_file])
    assert result.exit_code == 0
    assert "Launched 1 worker(s)" in result.output


def test_launch_unknown_target(runner, sample_config_file):
    """launch <unknown> should show available groups."""
    result = runner.invoke(main, ["launch", "nonexistent", "-c", sample_config_file])
    assert result.exit_code == 0
    assert "Unknown group or worker" in result.output


def test_launch_config_errors(runner, tmp_path):
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


def test_launch_by_group_number(runner, sample_config_file):
    """launch 1 should launch group at index 1 (backend)."""
    mock_post = AsyncMock(return_value={"launched": ["api"]})
    with patch("swarm.cli._api_post", mock_post):
        result = runner.invoke(main, ["launch", "1", "-c", sample_config_file])
    assert result.exit_code == 0
    assert "Launched 1 worker(s)" in result.output


def test_launch_by_worker_number(runner, sample_config_file):
    """launch 2 should launch worker at index 2 (first worker after 1 group)."""
    mock_post = AsyncMock(return_value={"launched": ["api"]})
    with patch("swarm.cli._api_post", mock_post):
        result = runner.invoke(main, ["launch", "2", "-c", sample_config_file])
    assert result.exit_code == 0
    assert "Launched 1 worker(s)" in result.output


def test_launch_number_out_of_range(runner, sample_config_file):
    """launch with out-of-range number should show available groups."""
    result = runner.invoke(main, ["launch", "99", "-c", sample_config_file])
    assert result.exit_code == 0
    assert "Unknown group or worker" in result.output


# --- launch with default_group ---


def test_launch_default_group(runner, tmp_path):
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
    mock_post = AsyncMock(return_value={"launched": ["api"]})
    with patch("swarm.cli._api_post", mock_post):
        result = runner.invoke(main, ["launch", "-c", str(config)])
    assert result.exit_code == 0
    assert "Launched 1 worker(s)" in result.output


def test_launch_default_group_not_found(runner, tmp_path):
    """launch with missing default_group should show error."""
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
    # default_group 'nonexistent' causes config validation error
    assert result.exit_code != 0
    assert "Config error" in result.output or "Unknown" in result.output


# --- serve ---


@patch("swarm.server.daemon.run_daemon", new_callable=AsyncMock)
def test_serve_invokes_run_daemon(mock_run, runner, sample_config_file):
    """serve should call run_daemon with config."""
    result = runner.invoke(main, ["serve", "-c", sample_config_file])
    assert result.exit_code == 0


# --- daemon ---


@patch("swarm.server.daemon.run_daemon", new_callable=AsyncMock)
def test_daemon_command(mock_run, runner, sample_config_file):
    """daemon subcommand should call run_daemon."""
    result = runner.invoke(main, ["daemon", "-c", sample_config_file])
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


def test_check_states_shows_workers(runner, monkeypatch):
    """check-states should show worker states from the daemon API."""
    monkeypatch.setattr("swarm.cli.load_config", lambda p=None: _make_config())

    mock_response = {
        "workers": [
            {"name": "api", "state": "resting", "state_duration": 10.5},
            {"name": "web", "state": "buzzing", "state_duration": 3.2},
        ]
    }

    mock_get = AsyncMock(return_value=mock_response)
    with patch("swarm.cli._api_get", mock_get):
        result = runner.invoke(main, ["check-states"])
        assert result.exit_code == 0
        assert "api" in result.output
        assert "resting" in result.output
        assert "10.5s" in result.output
        assert "web" in result.output
        assert "buzzing" in result.output
        assert "3.2s" in result.output


def test_check_states_no_daemon(runner, monkeypatch):
    """check-states should report error when daemon is not running."""
    monkeypatch.setattr("swarm.cli.load_config", lambda p=None: _make_config())

    mock_get = AsyncMock(side_effect=ConnectionError("daemon not running"))
    with patch("swarm.cli._api_get", mock_get):
        result = runner.invoke(main, ["check-states"])
        assert result.exit_code != 0
        assert "Cannot reach daemon" in result.output


def test_check_states_no_workers(runner, monkeypatch):
    """check-states should report when no workers are registered."""
    monkeypatch.setattr("swarm.cli.load_config", lambda p=None: _make_config())

    mock_get = AsyncMock(return_value={"workers": []})
    with patch("swarm.cli._api_get", mock_get):
        result = runner.invoke(main, ["check-states"])
        assert result.exit_code == 0
        assert "No workers" in result.output


# --- helpers ---


def _make_config():
    return HiveConfig(session_name="nonexistent")
