"""Tests for testing/project.py — TestProjectManager."""

import pytest

from swarm.testing.project import TestProjectManager


class TestTestProjectManager:
    def test_setup_creates_project(self):
        mgr = TestProjectManager()
        try:
            project_dir = mgr.setup()
            assert project_dir.exists()
            assert (project_dir / "pyproject.toml").exists()
            assert (project_dir / "tasks.yaml").exists()
            assert (project_dir / "src" / "calculator.py").exists()
            assert (project_dir / ".git").exists()
        finally:
            mgr.cleanup()

    def test_load_tasks(self):
        mgr = TestProjectManager()
        try:
            mgr.setup()
            tasks = mgr.load_tasks()
            assert len(tasks) >= 3
            assert all("title" in t for t in tasks)
            assert any("bug" in t.get("task_type", "") for t in tasks)
        finally:
            mgr.cleanup()

    def test_load_tasks_before_setup_raises(self):
        mgr = TestProjectManager()
        with pytest.raises(RuntimeError, match="setup"):
            mgr.load_tasks()

    def test_cleanup_removes_dir(self):
        mgr = TestProjectManager()
        project_dir = mgr.setup()
        parent = project_dir.parent
        assert parent.exists()
        mgr.cleanup()
        assert not parent.exists()
        assert mgr.project_dir is None

    def test_cleanup_idempotent(self):
        mgr = TestProjectManager()
        mgr.setup()
        mgr.cleanup()
        mgr.cleanup()  # should not raise

    def test_fixture_not_found(self, tmp_path):
        mgr = TestProjectManager(fixture_dir=tmp_path / "nonexistent")
        with pytest.raises(FileNotFoundError, match="not found"):
            mgr.setup()

    def test_git_commit_exists(self):
        import subprocess

        mgr = TestProjectManager()
        try:
            project_dir = mgr.setup()
            result = subprocess.run(
                ["git", "log", "--oneline", "-1"],
                cwd=project_dir,
                capture_output=True,
                text=True,
            )
            assert result.returncode == 0
            assert "Initial commit" in result.stdout
        finally:
            mgr.cleanup()
