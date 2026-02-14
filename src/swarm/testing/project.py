"""TestProjectManager — set up and tear down synthetic test projects."""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import yaml

from swarm.logging import get_logger

_log = get_logger("testing.project")

_FIXTURE_DIR = (
    Path(__file__).resolve().parent.parent.parent.parent / "tests" / "fixtures" / "test-project"
)


class TestProjectManager:
    """Manages synthetic test project lifecycle: copy, git init, load tasks, cleanup."""

    def __init__(self, fixture_dir: Path | None = None) -> None:
        self._fixture_dir = fixture_dir or _FIXTURE_DIR
        self._tmp_dir: Path | None = None

    @property
    def project_dir(self) -> Path | None:
        return self._tmp_dir

    def setup(self) -> Path:
        """Copy the fixture project to a temp dir and git-init it.

        Returns the path to the temp project directory.
        """
        if not self._fixture_dir.is_dir():
            raise FileNotFoundError(f"Test fixture directory not found: {self._fixture_dir}")

        self._tmp_dir = Path(tempfile.mkdtemp(prefix="swarm-test-"))
        project_dir = self._tmp_dir / "test-project"
        shutil.copytree(self._fixture_dir, project_dir)

        # Initialize git repo so Claude Code workers can operate normally
        subprocess.run(
            ["git", "init"],
            cwd=project_dir,
            capture_output=True,
            check=True,
        )
        subprocess.run(
            ["git", "add", "."],
            cwd=project_dir,
            capture_output=True,
            check=True,
        )
        subprocess.run(
            ["git", "commit", "-m", "Initial commit for test run"],
            cwd=project_dir,
            capture_output=True,
            check=True,
            env={
                "GIT_AUTHOR_NAME": "Swarm Test",
                "GIT_AUTHOR_EMAIL": "test@swarm.local",
                "GIT_COMMITTER_NAME": "Swarm Test",
                "GIT_COMMITTER_EMAIL": "test@swarm.local",
                **dict(__import__("os").environ),
            },
        )

        _log.info("test project set up at %s", project_dir)
        return project_dir

    def load_tasks(self) -> list[dict[str, Any]]:
        """Load tasks from the fixture's tasks.yaml.

        Returns a list of task dicts with keys: title, description, priority, task_type, tags.
        """
        if not self._tmp_dir:
            raise RuntimeError("setup() must be called before load_tasks()")

        tasks_file = self._tmp_dir / "test-project" / "tasks.yaml"
        if not tasks_file.exists():
            _log.warning("no tasks.yaml found in test project")
            return []

        data = yaml.safe_load(tasks_file.read_text()) or {}
        tasks = data.get("tasks", [])
        return [t for t in tasks if isinstance(t, dict)]

    def cleanup(self) -> None:
        """Remove the temporary project directory."""
        if self._tmp_dir and self._tmp_dir.exists():
            shutil.rmtree(self._tmp_dir, ignore_errors=True)
            _log.info("cleaned up test project at %s", self._tmp_dir)
            self._tmp_dir = None
