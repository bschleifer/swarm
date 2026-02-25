"""Tests for git conflict detection (swarm.git.conflicts)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from swarm.git.conflicts import detect_conflicts, get_changed_files


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


def _make_worktree(repo: Path, name: str) -> Path:
    """Create a worktree and return its path."""
    wt = repo / ".swarm" / "worktrees" / name
    wt.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "worktree", "add", "-b", f"swarm/{name}", str(wt)],
        cwd=str(repo),
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "test@test.com"],
        cwd=str(wt),
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=str(wt),
        check=True,
        capture_output=True,
    )
    return wt


class TestGetChangedFiles:
    @pytest.mark.asyncio
    async def test_no_changes(self, tmp_path: Path) -> None:
        _init_repo(tmp_path)
        result = await get_changed_files(tmp_path)
        assert result == set()

    @pytest.mark.asyncio
    async def test_modified_file(self, tmp_path: Path) -> None:
        _init_repo(tmp_path)
        (tmp_path / "README.md").write_text("changed")
        result = await get_changed_files(tmp_path)
        assert "README.md" in result

    @pytest.mark.asyncio
    async def test_untracked_file(self, tmp_path: Path) -> None:
        _init_repo(tmp_path)
        (tmp_path / "new.txt").write_text("new")
        result = await get_changed_files(tmp_path)
        assert "new.txt" in result

    @pytest.mark.asyncio
    async def test_non_repo(self, tmp_path: Path) -> None:
        # Non-repo directory should return empty set (git commands fail)
        result = await get_changed_files(tmp_path)
        assert result == set()


class TestDetectConflicts:
    @pytest.mark.asyncio
    async def test_empty_input(self) -> None:
        result = await detect_conflicts({})
        assert result == []

    @pytest.mark.asyncio
    async def test_no_overlap(self, tmp_path: Path) -> None:
        _init_repo(tmp_path)
        wt_a = _make_worktree(tmp_path, "a")
        wt_b = _make_worktree(tmp_path, "b")

        # Different files in each worktree
        (wt_a / "file_a.txt").write_text("a")
        (wt_b / "file_b.txt").write_text("b")

        result = await detect_conflicts({"a": wt_a, "b": wt_b})
        assert result == []

    @pytest.mark.asyncio
    async def test_overlap_detected(self, tmp_path: Path) -> None:
        _init_repo(tmp_path)
        wt_a = _make_worktree(tmp_path, "a")
        wt_b = _make_worktree(tmp_path, "b")

        # Same file in both worktrees
        (wt_a / "shared.txt").write_text("from a")
        (wt_b / "shared.txt").write_text("from b")

        result = await detect_conflicts({"a": wt_a, "b": wt_b})
        assert len(result) == 1
        assert result[0].file_path == "shared.txt"
        assert sorted(result[0].workers) == ["a", "b"]
