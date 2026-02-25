"""Conflict detection — track file overlap between workers."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

from swarm.logging import get_logger

_log = get_logger("git.conflicts")


@dataclass
class FileConflict:
    """A file being modified by multiple workers simultaneously."""

    file_path: str
    workers: list[str]


async def get_changed_files(worktree: Path) -> set[str]:
    """Return files changed (modified + untracked) in a worktree."""
    if not worktree.is_dir():
        return set()

    proc = await asyncio.create_subprocess_exec(
        "git",
        "diff",
        "--name-only",
        "HEAD",
        cwd=str(worktree),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await proc.communicate()
    changed = {f for f in stdout.decode(errors="replace").strip().splitlines() if f}

    # Also include untracked files
    proc2 = await asyncio.create_subprocess_exec(
        "git",
        "ls-files",
        "--others",
        "--exclude-standard",
        cwd=str(worktree),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout2, _ = await proc2.communicate()
    untracked = {f for f in stdout2.decode(errors="replace").strip().splitlines() if f}

    return changed | untracked


async def detect_conflicts(
    worktrees: dict[str, Path],
) -> list[FileConflict]:
    """Detect files modified by multiple workers concurrently.

    Args:
        worktrees: Mapping of worker name to worktree path.

    Returns:
        List of FileConflict for files touched by 2+ workers.
    """
    if not worktrees:
        return []

    # Gather changed files concurrently
    tasks = {name: asyncio.create_task(get_changed_files(path)) for name, path in worktrees.items()}
    results: dict[str, set[str]] = {}
    for name, task in tasks.items():
        try:
            results[name] = await task
        except Exception:
            _log.debug("failed to get changed files for %s", name)
            results[name] = set()

    # Invert: file → list of workers
    file_workers: dict[str, list[str]] = {}
    for name, files in results.items():
        for f in files:
            file_workers.setdefault(f, []).append(name)

    # Filter to overlaps (2+ workers)
    return [
        FileConflict(file_path=f, workers=sorted(workers))
        for f, workers in sorted(file_workers.items())
        if len(workers) >= 2
    ]
