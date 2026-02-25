"""Tests for git worktree isolation (swarm.git.worktree)."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from swarm.git.worktree import (
    create_worktree,
    get_current_branch,
    is_git_repo,
    list_worktrees,
    merge_worktree,
    remove_worktree,
    worktree_branch,
    worktree_path,
)


def _init_repo(path: Path) -> None:
    """Create a minimal git repo with an initial commit."""
    subprocess.run(["git", "init"], cwd=str(path), check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@test.com"],
        cwd=str(path),
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=str(path),
        check=True,
        capture_output=True,
    )
    (path / "README.md").write_text("init")
    subprocess.run(
        ["git", "add", "README.md"],
        cwd=str(path),
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "commit", "-m", "initial"],
        cwd=str(path),
        check=True,
        capture_output=True,
    )


class TestIsGitRepo:
    @pytest.mark.asyncio
    async def test_git_repo(self, tmp_path: Path) -> None:
        _init_repo(tmp_path)
        assert await is_git_repo(tmp_path)

    @pytest.mark.asyncio
    async def test_non_repo(self, tmp_path: Path) -> None:
        assert not await is_git_repo(tmp_path)


class TestWorktreePure:
    def test_worktree_path(self, tmp_path: Path) -> None:
        result = worktree_path(tmp_path, "api")
        assert result == tmp_path / ".swarm" / "worktrees" / "api"

    def test_worktree_branch(self) -> None:
        assert worktree_branch("api") == "swarm/api"


class TestCreateWorktree:
    @pytest.mark.asyncio
    async def test_creates_at_expected_path(self, tmp_path: Path) -> None:
        _init_repo(tmp_path)
        result = await create_worktree(tmp_path, "worker1")
        expected = tmp_path / ".swarm" / "worktrees" / "worker1"
        assert result == expected
        assert expected.is_dir()

    @pytest.mark.asyncio
    async def test_idempotent(self, tmp_path: Path) -> None:
        _init_repo(tmp_path)
        first = await create_worktree(tmp_path, "w1")
        second = await create_worktree(tmp_path, "w1")
        assert first == second

    @pytest.mark.asyncio
    async def test_correct_branch(self, tmp_path: Path) -> None:
        _init_repo(tmp_path)
        wt = await create_worktree(tmp_path, "api")
        branch = await get_current_branch(wt)
        assert branch == "swarm/api"


class TestRemoveWorktree:
    @pytest.mark.asyncio
    async def test_removes_successfully(self, tmp_path: Path) -> None:
        _init_repo(tmp_path)
        wt = await create_worktree(tmp_path, "w1")
        assert wt.is_dir()
        removed = await remove_worktree(tmp_path, "w1")
        assert removed is True
        assert not wt.exists()

    @pytest.mark.asyncio
    async def test_noop_for_nonexistent(self, tmp_path: Path) -> None:
        _init_repo(tmp_path)
        removed = await remove_worktree(tmp_path, "nope")
        assert removed is False


class TestListWorktrees:
    @pytest.mark.asyncio
    async def test_lists_swarm_worktrees(self, tmp_path: Path) -> None:
        _init_repo(tmp_path)
        await create_worktree(tmp_path, "api")
        await create_worktree(tmp_path, "web")
        infos = await list_worktrees(tmp_path)
        names = {i.worker_name for i in infos}
        assert names == {"api", "web"}
        for info in infos:
            assert info.branch.startswith("swarm/")


class TestMergeWorktree:
    @pytest.mark.asyncio
    async def test_clean_merge(self, tmp_path: Path) -> None:
        _init_repo(tmp_path)
        wt = await create_worktree(tmp_path, "feat")

        # Make a change in the worktree
        (wt / "new_file.txt").write_text("hello")
        subprocess.run(
            ["git", "add", "new_file.txt"],
            cwd=str(wt),
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "commit", "-m", "add new_file"],
            cwd=str(wt),
            check=True,
            capture_output=True,
        )

        result = await merge_worktree(tmp_path, "feat")
        assert result.success is True
        assert not result.conflicts

    @pytest.mark.asyncio
    async def test_conflict_detected_and_aborted(self, tmp_path: Path) -> None:
        _init_repo(tmp_path)
        wt = await create_worktree(tmp_path, "feat")

        # Change the same file in both branches
        (wt / "README.md").write_text("worktree change")
        subprocess.run(
            ["git", "add", "README.md"],
            cwd=str(wt),
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "commit", "-m", "worktree edit"],
            cwd=str(wt),
            check=True,
            capture_output=True,
        )

        (tmp_path / "README.md").write_text("main change")
        subprocess.run(
            ["git", "add", "README.md"],
            cwd=str(tmp_path),
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "commit", "-m", "main edit"],
            cwd=str(tmp_path),
            check=True,
            capture_output=True,
        )

        result = await merge_worktree(tmp_path, "feat")
        assert result.success is False
        assert "README.md" in result.conflicts

    @pytest.mark.asyncio
    async def test_nonexistent_branch(self, tmp_path: Path) -> None:
        _init_repo(tmp_path)
        result = await merge_worktree(tmp_path, "nope")
        assert result.success is False


class TestLaunchWithWorktree:
    """Verify launch_workers uses worktree path when isolation=worktree."""

    @pytest.mark.asyncio
    async def test_launch_uses_worktree_path(self, tmp_path: Path) -> None:
        from swarm.config import WorkerConfig

        wc = WorkerConfig(
            name="api",
            path=str(tmp_path),
            isolation="worktree",
        )

        fake_wt = tmp_path / ".swarm" / "worktrees" / "api"
        fake_wt.mkdir(parents=True)

        with (
            patch(
                "swarm.git.worktree.is_git_repo",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch(
                "swarm.git.worktree.create_worktree",
                new_callable=AsyncMock,
                return_value=fake_wt,
            ),
            patch(
                "swarm.git.worktree.worktree_branch",
                return_value="swarm/api",
            ),
        ):
            from swarm.worker.manager import _resolve_worktree

            spawn_path, repo_path, branch = await _resolve_worktree(wc)
            assert spawn_path == str(fake_wt)
            assert repo_path == str(tmp_path)
            assert branch == "swarm/api"

    @pytest.mark.asyncio
    async def test_launch_no_isolation(self, tmp_path: Path) -> None:
        from swarm.config import WorkerConfig

        wc = WorkerConfig(name="api", path=str(tmp_path))

        from swarm.worker.manager import _resolve_worktree

        spawn_path, repo_path, branch = await _resolve_worktree(wc)
        assert spawn_path == str(tmp_path)
        assert repo_path == ""
        assert branch == ""
