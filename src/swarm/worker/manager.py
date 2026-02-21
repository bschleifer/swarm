"""Worker lifecycle management: spawn, kill, revive workers via ProcessPool."""

from __future__ import annotations

from swarm.config import WorkerConfig
from swarm.logging import get_logger
from swarm.providers import LLMProvider, get_provider
from swarm.pty.pool import ProcessPool
from swarm.pty.process import ProcessError
from swarm.worker.worker import Worker, WorkerState

_log = get_logger("worker.manager")


async def launch_workers(
    pool: ProcessPool,
    worker_configs: list[WorkerConfig],
    stagger_seconds: float = 2.0,
    provider: LLMProvider | None = None,
) -> list[Worker]:
    """Spawn all workers via the pool and return Worker objects."""
    prov = provider or get_provider()
    workers_to_spawn = [(wc.name, str(wc.resolved_path)) for wc in worker_configs]
    procs = await pool.spawn_batch(
        workers_to_spawn,
        command=prov.worker_command(),
        stagger_seconds=stagger_seconds,
    )

    launched: list[Worker] = []
    for wc, proc in zip(worker_configs, procs):
        worker = Worker(
            name=wc.name,
            path=str(wc.resolved_path),
            process=proc,
        )
        launched.append(worker)

    return launched


async def revive_worker(
    worker: Worker,
    pool: ProcessPool,
    provider: LLMProvider | None = None,
) -> None:
    """Revive a stung (exited) worker by respawning via the pool."""
    prov = provider or get_provider()
    try:
        new_proc = await pool.revive(worker.name, cwd=worker.path, command=prov.worker_command())
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
    provider: LLMProvider | None = None,
) -> Worker:
    """Add a new worker to a running swarm.

    Spawns a new process via the pool. When *auto_start* is ``True``,
    launches the provider's interactive command immediately.
    """
    prov = provider or get_provider()
    path = str(worker_config.resolved_path)
    command = prov.worker_command() if auto_start else ["bash"]
    proc = await pool.spawn(worker_config.name, path, command=command)

    initial_state = WorkerState.BUZZING if auto_start else WorkerState.RESTING
    worker = Worker(
        name=worker_config.name,
        path=path,
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
