"""Git worktree management for per-worker isolation."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

from swarm.logging import get_logger

_log = get_logger("git.worktree")

_SWARM_DIR = ".swarm"
_WORKTREE_DIR = "worktrees"
_BRANCH_PREFIX = "swarm/"


@dataclass
class WorktreeInfo:
    """Parsed info from ``git worktree list --porcelain``."""

    path: Path
    branch: str
    worker_name: str


@dataclass
class MergeResult:
    """Result of merging a worktree branch back to the main branch."""

    success: bool
    message: str
    conflicts: list[str]


async def _run_git(
    *args: str,
    cwd: Path,
    check: bool = True,
) -> tuple[int, str, str]:
    """Run a git command asynchronously. Returns (returncode, stdout, stderr)."""
    proc = await asyncio.create_subprocess_exec(
        "git",
        *args,
        cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout_b, stderr_b = await proc.communicate()
    stdout = stdout_b.decode(errors="replace").strip()
    stderr = stderr_b.decode(errors="replace").strip()
    if check and proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed (rc={proc.returncode}): {stderr}")
    return proc.returncode, stdout, stderr


async def is_git_repo(path: Path) -> bool:
    """Check if *path* is inside a git repository."""
    rc, _, _ = await _run_git("rev-parse", "--is-inside-work-tree", cwd=path, check=False)
    return rc == 0


async def get_repo_root(path: Path) -> Path | None:
    """Return the repository root for *path*, or None."""
    rc, stdout, _ = await _run_git("rev-parse", "--show-toplevel", cwd=path, check=False)
    if rc != 0:
        return None
    return Path(stdout)


def worktree_path(repo: Path, name: str) -> Path:
    """Pure: compute the worktree directory for a worker name."""
    return repo / _SWARM_DIR / _WORKTREE_DIR / name


def worktree_branch(name: str) -> str:
    """Pure: compute the branch name for a worker."""
    return f"{_BRANCH_PREFIX}{name}"


async def create_worktree(repo: Path, name: str) -> Path:
    """Create a git worktree for *name*. Idempotent — returns path if exists."""
    wt = worktree_path(repo, name)
    branch = worktree_branch(name)

    if wt.exists():
        _log.debug("worktree already exists: %s", wt)
        return wt

    wt.parent.mkdir(parents=True, exist_ok=True)

    # Check if branch already exists
    rc, _, _ = await _run_git("rev-parse", "--verify", branch, cwd=repo, check=False)
    if rc == 0:
        # Branch exists — create worktree using existing branch
        await _run_git("worktree", "add", str(wt), branch, cwd=repo)
    else:
        # New branch from HEAD
        await _run_git("worktree", "add", "-b", branch, str(wt), cwd=repo)

    _log.info("created worktree %s on branch %s", wt, branch)
    return wt


async def remove_worktree(repo: Path, name: str) -> bool:
    """Remove a worktree and its branch. Returns True if removed."""
    wt = worktree_path(repo, name)
    branch = worktree_branch(name)

    if not wt.exists():
        _log.debug("worktree does not exist: %s", wt)
        return False

    rc, _, stderr = await _run_git("worktree", "remove", "--force", str(wt), cwd=repo, check=False)
    if rc != 0:
        _log.warning("worktree remove failed: %s", stderr)
        return False

    # Clean up the branch
    await _run_git("branch", "-D", branch, cwd=repo, check=False)
    _log.info("removed worktree %s and branch %s", wt, branch)
    return True


async def list_worktrees(repo: Path) -> list[WorktreeInfo]:
    """List swarm-managed worktrees in the repo."""
    _, stdout, _ = await _run_git("worktree", "list", "--porcelain", cwd=repo)
    result: list[WorktreeInfo] = []
    current_path: Path | None = None
    current_branch = ""

    for line in stdout.splitlines():
        if line.startswith("worktree "):
            current_path = Path(line[len("worktree ") :])
            current_branch = ""
        elif line.startswith("branch "):
            ref = line[len("branch ") :]
            # refs/heads/swarm/worker-name → swarm/worker-name
            if ref.startswith("refs/heads/"):
                current_branch = ref[len("refs/heads/") :]
        elif line == "" and current_path is not None:
            if current_branch.startswith(_BRANCH_PREFIX):
                worker_name = current_branch[len(_BRANCH_PREFIX) :]
                result.append(
                    WorktreeInfo(
                        path=current_path,
                        branch=current_branch,
                        worker_name=worker_name,
                    )
                )
            current_path = None
            current_branch = ""

    # Handle last entry (no trailing blank line)
    if current_path is not None and current_branch.startswith(_BRANCH_PREFIX):
        worker_name = current_branch[len(_BRANCH_PREFIX) :]
        result.append(
            WorktreeInfo(
                path=current_path,
                branch=current_branch,
                worker_name=worker_name,
            )
        )

    return result


async def get_current_branch(path: Path) -> str | None:
    """Get the current branch name at *path*."""
    rc, stdout, _ = await _run_git("rev-parse", "--abbrev-ref", "HEAD", cwd=path, check=False)
    if rc != 0:
        return None
    return stdout


async def merge_worktree(repo: Path, name: str) -> MergeResult:
    """Merge a worktree branch back to the current branch. Abort on conflict."""
    branch = worktree_branch(name)

    # Verify branch exists
    rc, _, _ = await _run_git("rev-parse", "--verify", branch, cwd=repo, check=False)
    if rc != 0:
        return MergeResult(
            success=False,
            message=f"Branch {branch} does not exist",
            conflicts=[],
        )

    rc, _stdout, _stderr = await _run_git(
        "merge",
        "--no-ff",
        branch,
        "-m",
        f"Merge {branch}",
        cwd=repo,
        check=False,
    )
    if rc == 0:
        return MergeResult(success=True, message=f"Merged {branch} successfully", conflicts=[])

    # Conflict — gather conflicted files and abort
    _, diff_out, _ = await _run_git("diff", "--name-only", "--diff-filter=U", cwd=repo, check=False)
    conflicts = [f for f in diff_out.splitlines() if f]

    await _run_git("merge", "--abort", cwd=repo, check=False)

    return MergeResult(
        success=False,
        message=f"Merge conflicts in {len(conflicts)} file(s) — aborted",
        conflicts=conflicts,
    )
