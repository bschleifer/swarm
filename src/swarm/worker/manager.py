"""Worker lifecycle management: spawn, kill, revive workers via ProcessPool."""

from __future__ import annotations

import asyncio

from swarm.config import WorkerConfig
from swarm.logging import get_logger
from swarm.providers import get_provider
from swarm.pty.pool import ProcessPool
from swarm.pty.process import ProcessError
from swarm.worker.worker import Worker, WorkerState

_log = get_logger("worker.manager")


def _resolve_provider_name(wc: WorkerConfig, default: str) -> str:
    """Return the provider name for a worker config, falling back to default."""
    return wc.provider or default


async def _resolve_worktree(
    wc: WorkerConfig,
) -> tuple[str, str, str]:
    """Resolve worktree isolation for a worker config.

    Returns ``(spawn_path, repo_path, worktree_branch)`` where
    *repo_path* and *worktree_branch* are empty strings when isolation
    is not enabled.
    """
    if wc.isolation != "worktree":
        return str(wc.resolved_path), "", ""

    from swarm.git.worktree import (
        create_worktree,
        is_git_repo,
        worktree_branch,
    )

    if not await is_git_repo(wc.resolved_path):
        _log.warning(
            "isolation=worktree but %s is not a git repo",
            wc.resolved_path,
        )
        return str(wc.resolved_path), "", ""

    wt_path = await create_worktree(wc.resolved_path, wc.name)
    return str(wt_path), str(wc.resolved_path), worktree_branch(wc.name)


async def _cleanup_worktree(repo_path: str, worker_name: str) -> None:
    """Remove a worktree after a failed spawn. Best-effort — logs on failure."""
    try:
        from pathlib import Path

        from swarm.git.worktree import remove_worktree

        await remove_worktree(Path(repo_path), worker_name)
    except Exception:
        _log.debug("worktree cleanup failed for %s", worker_name, exc_info=True)


async def launch_workers(
    pool: ProcessPool,
    worker_configs: list[WorkerConfig],
    stagger_seconds: float = 2.0,
    default_provider: str = "claude",
) -> list[Worker]:
    """Spawn all workers via the pool and return Worker objects.

    Each worker gets its own provider-specific command based on its
    ``provider`` config (or the *default_provider* fallback).
    """
    launched: list[Worker] = []
    for i, wc in enumerate(worker_configs):
        prov_name = _resolve_provider_name(wc, default_provider)
        prov = get_provider(prov_name)
        spawn_path, repo_path, wt_branch = await _resolve_worktree(wc)
        try:
            proc = await pool.spawn(
                wc.name, spawn_path, command=prov.worker_command(), shell_wrap=True
            )
        except (ProcessError, OSError) as exc:
            _log.error("spawn failed for worker '%s': %s", wc.name, exc)
            if repo_path:
                await _cleanup_worktree(repo_path, wc.name)
            # Kill already-launched workers to avoid orphans
            for w in launched:
                try:
                    await pool.kill(w.name)
                except (ProcessError, OSError):
                    _log.debug("cleanup kill failed for %s", w.name)
            raise
        worker = Worker(
            name=wc.name,
            path=spawn_path,
            provider_name=prov_name,
            process=proc,
            repo_path=repo_path,
            worktree_branch=wt_branch,
        )
        launched.append(worker)
        if i < len(worker_configs) - 1 and stagger_seconds > 0:
            await asyncio.sleep(stagger_seconds)

    return launched


async def revive_worker(
    worker: Worker,
    pool: ProcessPool,
) -> None:
    """Revive a stung (exited) worker by respawning via the pool."""
    prov = get_provider(worker.provider_name)
    try:
        new_proc = await pool.revive(
            worker.name, cwd=worker.path, command=prov.worker_command(), shell_wrap=True
        )
        if new_proc:
            worker.process = new_proc
            worker.record_revive()
            worker.update_state(WorkerState.BUZZING)
            _log.info("revived %s (pid=%d)", worker.name, new_proc.pid)
        else:
            _log.warning("cannot revive %s — not found in pool", worker.name)
    except ProcessError as e:
        _log.warning("revive failed for %s: %s", worker.name, e)


async def add_worker_live(
    pool: ProcessPool,
    worker_config: WorkerConfig,
    workers: list[Worker],
    auto_start: bool = False,
    default_provider: str = "claude",
) -> Worker:
    """Add a new worker to a running swarm.

    Spawns a new process via the pool. When *auto_start* is ``True``,
    launches the provider's interactive command immediately.
    """
    prov_name = _resolve_provider_name(worker_config, default_provider)
    prov = get_provider(prov_name)
    spawn_path, repo_path, wt_branch = await _resolve_worktree(worker_config)
    command = prov.worker_command(resume=False) if auto_start else ["bash"]
    try:
        proc = await pool.spawn(
            worker_config.name,
            spawn_path,
            command=command,
            shell_wrap=auto_start,
        )
    except (ProcessError, OSError):
        if repo_path:
            await _cleanup_worktree(repo_path, worker_config.name)
        raise

    initial_state = WorkerState.BUZZING if auto_start else WorkerState.RESTING
    worker = Worker(
        name=worker_config.name,
        path=spawn_path,
        provider_name=prov_name,
        process=proc,
        state=initial_state,
        repo_path=repo_path,
        worktree_branch=wt_branch,
    )
    workers.append(worker)
    _log.info(
        "live-added worker %s at %s (pid=%d)",
        worker_config.name,
        spawn_path,
        proc.pid,
    )
    return worker


async def kill_worker(worker: Worker, pool: ProcessPool) -> None:
    """Kill a specific worker."""
    try:
        await pool.kill(worker.name)
    except ProcessError:
        _log.debug("kill failed for %s (process may be gone)", worker.name)
