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
        proc = await pool.spawn(
            wc.name, str(wc.resolved_path), command=prov.worker_command(), shell_wrap=True
        )
        worker = Worker(
            name=wc.name,
            path=str(wc.resolved_path),
            provider_name=prov_name,
            process=proc,
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
    path = str(worker_config.resolved_path)
    command = prov.worker_command(resume=False) if auto_start else ["bash"]
    proc = await pool.spawn(worker_config.name, path, command=command, shell_wrap=auto_start)

    initial_state = WorkerState.BUZZING if auto_start else WorkerState.RESTING
    worker = Worker(
        name=worker_config.name,
        path=path,
        provider_name=prov_name,
        process=proc,
        state=initial_state,
    )
    workers.append(worker)
    _log.info("live-added worker %s at %s (pid=%d)", worker_config.name, path, proc.pid)
    return worker


async def kill_worker(worker: Worker, pool: ProcessPool) -> None:
    """Kill a specific worker."""
    try:
        await pool.kill(worker.name)
    except ProcessError:
        _log.debug("kill failed for %s (process may be gone)", worker.name)
