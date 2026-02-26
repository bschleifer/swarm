"""WorkerService — worker CRUD, I/O operations, and lifecycle management."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from swarm.drones.log import DroneAction, LogCategory
from swarm.logging import get_logger
from swarm.pty.process import ProcessError
from swarm.server.helpers import truncate_preview
from swarm.worker.worker import Worker, WorkerState

if TYPE_CHECKING:
    from swarm.config import WorkerConfig
    from swarm.server.daemon import SwarmDaemon

_log = get_logger("server.worker_service")


def _infer_provider_from_name(name: str) -> str:
    """Infer provider from worker name suffix (e.g., foo-codex)."""
    n = name.lower()
    for prov in ("codex", "gemini", "claude"):
        if n.endswith(f"-{prov}"):
            return prov
    return ""


class WorkerService:
    """Manages worker CRUD, process I/O, and lifecycle."""

    def __init__(self, daemon: SwarmDaemon) -> None:
        self._daemon = daemon

    def get_worker(self, name: str) -> Worker | None:
        """Find a worker by name."""
        return next((w for w in self._daemon.workers if w.name == name), None)

    def require_worker(self, name: str) -> Worker:
        """Get worker by name or raise WorkerNotFoundError."""
        from swarm.server.daemon import WorkerNotFoundError

        worker = self.get_worker(name)
        if not worker:
            raise WorkerNotFoundError(f"Worker '{name}' not found")
        return worker

    # --- Worker I/O operations ---

    async def send_to_worker(self, name: str, message: str, *, _log_operator: bool = True) -> None:
        """Send text to a worker's process."""
        worker = self.require_worker(name)
        if self._daemon.pilot:
            self._daemon.pilot.wake_worker(name)
        await worker.process.send_keys(message)
        if _log_operator:
            self._daemon.drone_log.add(
                DroneAction.OPERATOR, name, "sent message", category=LogCategory.OPERATOR
            )

    async def prep_for_task(self, worker_name: str, *, timeout: float = 30.0) -> None:
        """Send /get-latest and /clear before a new task assignment.

        Runs as a bounded async operation with a configurable timeout
        (default 30s) so it never blocks the HTTP handler indefinitely.
        """
        from swarm.providers import get_provider

        worker = self.require_worker(worker_name)
        provider = get_provider(worker.provider_name)

        async def _wait_for_idle(timeout_polls: int = 60) -> bool:
            for _ in range(timeout_polls):
                await asyncio.sleep(0.5)
                cmd = worker.process.get_child_foreground_command()
                content = worker.process.get_content(35)
                state = provider.classify_output(cmd, content)
                if state == WorkerState.RESTING:
                    return True
            return False

        try:
            await asyncio.wait_for(self._do_prep(worker, _wait_for_idle), timeout=timeout)
        except TimeoutError:
            _log.warning("prep: timed out after %.0fs for worker %s", timeout, worker_name)

    @staticmethod
    async def _do_prep(
        worker: Worker,
        wait_for_idle: Callable[[], Awaitable[bool]],
    ) -> None:
        """Internal prep sequence — separated for timeout wrapping."""
        if not await wait_for_idle():
            _log.warning("prep: worker %s never became idle — skipping prep", worker.name)
            return

        await worker.process.send_keys("/get-latest")
        if not await wait_for_idle():
            _log.warning("prep: /get-latest timed out for worker %s", worker.name)
            return

        await worker.process.send_keys("/clear")
        if not await wait_for_idle():
            _log.warning("prep: /clear timed out for worker %s", worker.name)
            return

    async def continue_worker(self, name: str) -> None:
        """Send Enter to a worker's process."""
        worker = self.require_worker(name)
        if self._daemon.pilot:
            self._daemon.pilot.wake_worker(name)
        await worker.process.send_enter()
        self._daemon.drone_log.add(
            DroneAction.OPERATOR, name, "continued (manual)", category=LogCategory.OPERATOR
        )

    async def interrupt_worker(self, name: str) -> None:
        """Send Ctrl-C to a worker's process."""
        worker = self.require_worker(name)
        if self._daemon.pilot:
            self._daemon.pilot.wake_worker(name)
        await worker.process.send_interrupt()
        self._daemon.drone_log.add(
            DroneAction.OPERATOR, name, "interrupted (Ctrl-C)", category=LogCategory.OPERATOR
        )

    async def escape_worker(self, name: str) -> None:
        """Send Escape to a worker's process."""
        worker = self.require_worker(name)
        if self._daemon.pilot:
            self._daemon.pilot.wake_worker(name)
        await worker.process.send_escape()
        self._daemon.drone_log.add(
            DroneAction.OPERATOR, name, "sent Escape", category=LogCategory.OPERATOR
        )

    async def redraw_worker(self, name: str) -> None:
        """Send SIGWINCH to force TUI redraw for a worker."""
        worker = self.require_worker(name)
        await worker.process.send_sigwinch()

    async def capture_output(self, name: str, lines: int = 80) -> str:
        """Read a worker's process output buffer."""
        worker = self.require_worker(name)
        return worker.process.get_content(lines)

    async def safe_capture_output(self, name: str, lines: int = 80) -> str:
        """Read process output, returning a fallback string on failure."""
        from swarm.server.daemon import WorkerNotFoundError

        try:
            return await self.capture_output(name, lines=lines)
        except (TimeoutError, ProcessError, OSError, WorkerNotFoundError):
            return "(output unavailable)"

    async def discover(self) -> list[Worker]:
        """Discover existing workers via the process pool. Updates daemon.workers."""
        d = self._daemon
        if d.pool:
            processes = await d.pool.discover()
            # Wrap WorkerProcess objects in Worker dataclasses.
            # Match against existing workers to preserve state; create new
            # Worker objects for any processes discovered for the first time.
            existing = {w.name: w for w in d.workers}
            workers: list[Worker] = []
            for proc in processes:
                if proc.name in existing:
                    w = existing[proc.name]
                    w.process = proc
                    wc = d.config.get_worker(proc.name)
                    if wc and wc.provider:
                        w.provider_name = wc.provider
                    elif not wc:
                        inferred = _infer_provider_from_name(proc.name)
                        if inferred:
                            w.provider_name = inferred
                else:
                    wc = d.config.get_worker(proc.name)
                    if wc:
                        prov_name = wc.provider or d.config.provider
                    else:
                        prov_name = _infer_provider_from_name(proc.name) or d.config.provider
                    w = Worker(
                        name=proc.name,
                        path=proc.cwd,
                        provider_name=prov_name,
                        process=proc,
                    )
                workers.append(w)
            d.workers = workers
        return d.workers

    # --- Lifecycle ---

    async def launch(self, worker_configs: list[WorkerConfig]) -> list[Worker]:
        """Launch workers via the process pool. Extends workers and updates pilot."""
        d = self._daemon
        default_prov = d.config.provider
        if d.workers:
            from swarm.worker.manager import add_worker_live

            launched = []
            for wc in worker_configs:
                worker = await add_worker_live(
                    d.pool,
                    wc,
                    [],
                    auto_start=True,
                    default_provider=default_prov,
                )
                launched.append(worker)
            async with d._worker_lock:
                d.workers.extend(launched)
        else:
            from swarm.worker.manager import launch_workers

            launched = await launch_workers(
                d.pool,
                worker_configs,
                default_provider=default_prov,
            )
            async with d._worker_lock:
                d.workers.extend(launched)

        if d.pilot:
            d.pilot.workers = d.workers
        else:
            d.init_pilot(enabled=d.config.drones.enabled)
        d.broadcast_ws({"type": "workers_changed"})
        return launched

    async def spawn(self, worker_config: WorkerConfig) -> Worker:
        """Spawn a single worker into the running session."""
        from swarm.server.daemon import SwarmOperationError
        from swarm.worker.manager import add_worker_live

        d = self._daemon
        if any(w.name.lower() == worker_config.name.lower() for w in d.workers):
            raise SwarmOperationError(f"Worker '{worker_config.name}' already running")

        async with d._worker_lock:
            worker = await add_worker_live(
                d.pool,
                worker_config,
                d.workers,
                auto_start=True,
                default_provider=d.config.provider,
            )
        if d.pilot:
            d.pilot.workers = d.workers
        d.broadcast_ws({"type": "workers_changed"})
        return worker

    async def kill(self, name: str) -> None:
        """Kill a worker: mark STUNG, unassign tasks, broadcast."""
        from swarm.worker.manager import kill_worker as _kill_worker

        d = self._daemon
        worker = self.require_worker(name)

        async with d._worker_lock:
            await _kill_worker(worker, d.pool)
            worker.state = WorkerState.STUNG
        d.task_board.unassign_worker(worker.name)
        d.drone_log.add(DroneAction.OPERATOR, name, "killed", category=LogCategory.OPERATOR)
        d.broadcast_ws(
            {
                "type": "workers_changed",
                "workers": [{"name": w.name, "state": w.state.value} for w in d.workers],
            }
        )

    async def revive(self, name: str) -> None:
        """Revive a STUNG worker."""
        from swarm.server.daemon import SwarmOperationError
        from swarm.worker.manager import revive_worker as _revive_worker

        d = self._daemon
        worker = self.require_worker(name)
        if worker.state != WorkerState.STUNG:
            raise SwarmOperationError(f"Worker '{name}' is {worker.state.value}, not STUNG")

        await _revive_worker(worker, d.pool)
        if not worker.process or not worker.process.is_alive:
            raise SwarmOperationError(f"Failed to revive worker '{name}'")
        worker.state = WorkerState.BUZZING
        worker.record_revive()
        d.drone_log.add(
            DroneAction.OPERATOR, name, "revived (manual)", category=LogCategory.OPERATOR
        )
        d.broadcast_ws({"type": "workers_changed"})

    async def merge_worker(self, name: str) -> dict[str, object]:
        """Merge a worker's worktree branch back to the main branch."""
        worker = self.require_worker(name)
        if not worker.repo_path:
            return {
                "success": False,
                "message": f"Worker '{name}' has no worktree",
                "conflicts": [],
            }
        from swarm.git.worktree import merge_worktree

        repo = __import__("pathlib").Path(worker.repo_path)
        result = await merge_worktree(repo, name)
        _log.info(
            "merge %s: success=%s message=%s",
            name,
            result.success,
            result.message,
        )
        return {
            "success": result.success,
            "message": result.message,
            "conflicts": result.conflicts,
        }

    async def kill_session(self, *, all_sessions: bool = False) -> None:
        """Kill all workers and clean up."""
        d = self._daemon
        if d.pilot:
            d.pilot.stop()

        for w in list(d.workers):
            d.task_board.unassign_worker(w.name)

        if d.pool:
            try:
                await d.pool.kill_all()
            except (ProcessError, OSError):
                _log.warning(
                    "kill_all failed (processes may already be gone)",
                    exc_info=True,
                )

        # Clean up worktrees for isolated workers
        for w in list(d.workers):
            if w.repo_path:
                try:
                    from pathlib import Path

                    from swarm.git.worktree import remove_worktree

                    await remove_worktree(Path(w.repo_path), w.name)
                except Exception:
                    _log.debug(
                        "worktree cleanup failed for %s",
                        w.name,
                        exc_info=True,
                    )

        async with d._worker_lock:
            d.workers.clear()
        d.drone_log.clear()
        d.broadcast_ws({"type": "workers_changed"})

    # --- Bulk operations ---

    async def _send_to_workers(
        self,
        workers: list[Worker],
        action: Callable[[Worker], Awaitable[None]],
        log_actor: str,
        log_detail: str,
    ) -> int:
        """Send an action to a list of workers. Returns count of successes."""
        count = 0
        for w in workers:
            try:
                await action(w)
                count += 1
            except (TimeoutError, ProcessError, OSError):
                _log.debug("failed to send to %s", w.name)
        if count:
            self._daemon.drone_log.add(
                DroneAction.OPERATOR,
                log_actor,
                log_detail.format(count=count),
                category=LogCategory.OPERATOR,
            )
        return count

    async def continue_all(self) -> int:
        """Send Enter to all RESTING/WAITING workers (skips user-active terminals)."""
        d = self._daemon
        targets = [
            w
            for w in d.workers
            if w.state in (WorkerState.RESTING, WorkerState.WAITING)
            and not (w.process and w.process.is_user_active)
        ]
        return await self._send_to_workers(
            targets, lambda w: w.process.send_enter(), "all", "continued {count} worker(s)"
        )

    async def send_all(self, message: str) -> int:
        """Send a message to all workers (skips user-active terminals)."""
        d = self._daemon
        preview = truncate_preview(message)
        targets = [w for w in d.workers if not (w.process and w.process.is_user_active)]
        return await self._send_to_workers(
            targets,
            lambda w: w.process.send_keys(message),
            "all",
            f'broadcast to {{count}} worker(s): "{preview}"',
        )

    async def send_group(self, group_name: str, message: str) -> int:
        """Send a message to all workers in a group."""
        d = self._daemon
        group_workers = d.config.get_group(group_name)
        group_names = {w.name.lower() for w in group_workers}
        targets = [w for w in d.workers if w.name.lower() in group_names]
        preview = truncate_preview(message)
        return await self._send_to_workers(
            targets,
            lambda w: w.process.send_keys(message),
            group_name,
            f'group send to {{count}} worker(s): "{preview}"',
        )
