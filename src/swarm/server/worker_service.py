"""WorkerService — worker CRUD, tmux operations, and lifecycle management."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from swarm.drones.log import DroneAction, LogCategory
from swarm.logging import get_logger
from swarm.tmux.cell import (
    PaneGoneError,
    TmuxError,
    capture_pane,
    get_pane_command,
    send_enter,
    send_escape,
    send_interrupt,
    send_keys,
)
from swarm.tmux.hive import discover_workers
from swarm.worker.worker import Worker, WorkerState

if TYPE_CHECKING:
    from swarm.config import WorkerConfig
    from swarm.server.daemon import SwarmDaemon

_log = get_logger("server.worker_service")


class WorkerService:
    """Manages worker CRUD, tmux operations, and lifecycle."""

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

    # --- Tmux operations ---

    async def send_to_worker(self, name: str, message: str, *, _log_operator: bool = True) -> None:
        """Send text to a worker's tmux pane."""
        worker = self.require_worker(name)
        if self._daemon.pilot:
            self._daemon.pilot.wake_worker(name)
        await send_keys(worker.pane_id, message)
        if _log_operator:
            self._daemon.drone_log.add(
                DroneAction.OPERATOR, name, "sent message", category=LogCategory.OPERATOR
            )

    async def prep_for_task(self, pane_id: str) -> None:
        """Send /get-latest and /clear before a new task assignment."""
        from swarm.worker.state import classify_pane_content

        async def _wait_for_idle(timeout_polls: int = 120) -> bool:
            for _ in range(timeout_polls):
                await asyncio.sleep(0.5)
                cmd = await get_pane_command(pane_id)
                content = await capture_pane(pane_id)
                state = classify_pane_content(cmd, content)
                if state == WorkerState.RESTING:
                    return True
            return False

        if not await _wait_for_idle():
            _log.warning("prep: worker pane %s never became idle — skipping prep", pane_id)
            return

        await send_keys(pane_id, "/get-latest")
        if not await _wait_for_idle():
            _log.warning("prep: /get-latest timed out for pane %s", pane_id)
            return

        await send_keys(pane_id, "/clear")
        if not await _wait_for_idle():
            _log.warning("prep: /clear timed out for pane %s", pane_id)
            return

    async def continue_worker(self, name: str) -> None:
        """Send Enter to a worker's tmux pane."""
        worker = self.require_worker(name)
        if self._daemon.pilot:
            self._daemon.pilot.wake_worker(name)
        await send_enter(worker.pane_id)
        self._daemon.drone_log.add(
            DroneAction.OPERATOR, name, "continued (manual)", category=LogCategory.OPERATOR
        )

    async def interrupt_worker(self, name: str) -> None:
        """Send Ctrl-C to a worker's tmux pane."""
        worker = self.require_worker(name)
        if self._daemon.pilot:
            self._daemon.pilot.wake_worker(name)
        await send_interrupt(worker.pane_id)
        self._daemon.drone_log.add(
            DroneAction.OPERATOR, name, "interrupted (Ctrl-C)", category=LogCategory.OPERATOR
        )

    async def escape_worker(self, name: str) -> None:
        """Send Escape to a worker's tmux pane."""
        worker = self.require_worker(name)
        if self._daemon.pilot:
            self._daemon.pilot.wake_worker(name)
        await send_escape(worker.pane_id)
        self._daemon.drone_log.add(
            DroneAction.OPERATOR, name, "sent Escape", category=LogCategory.OPERATOR
        )

    async def capture_output(self, name: str, lines: int = 80) -> str:
        """Capture a worker's tmux pane content."""
        worker = self.require_worker(name)
        return await capture_pane(worker.pane_id, lines=lines)

    async def safe_capture_output(self, name: str, lines: int = 80) -> str:
        """Capture pane content, returning a fallback string on failure."""
        from swarm.server.daemon import WorkerNotFoundError

        try:
            return await self.capture_output(name, lines=lines)
        except (OSError, asyncio.TimeoutError, WorkerNotFoundError, TmuxError, PaneGoneError):
            return "(pane unavailable)"

    async def discover(self) -> list[Worker]:
        """Discover workers in the configured tmux session. Updates daemon.workers."""
        d = self._daemon
        d.workers = await discover_workers(d.config.session_name)
        return d.workers

    # --- Lifecycle ---

    async def launch(self, worker_configs: list[WorkerConfig]) -> list[Worker]:
        """Launch workers in tmux. Extends workers and updates pilot."""
        d = self._daemon
        if d.workers:
            from swarm.worker.manager import add_worker_live

            launched = []
            for wc in worker_configs:
                worker = await add_worker_live(
                    d.config.session_name,
                    wc,
                    [],
                    d.config.panes_per_window,
                    auto_start=True,
                )
                launched.append(worker)
            async with d._worker_lock:
                d.workers.extend(launched)
        else:
            from swarm.worker.manager import launch_hive

            launched = await launch_hive(
                d.config.session_name,
                worker_configs,
                d.config.panes_per_window,
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
                d.config.session_name,
                worker_config,
                d.workers,
                d.config.panes_per_window,
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
            await _kill_worker(worker)
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

        await _revive_worker(worker, session_name=d.config.session_name)
        worker.state = WorkerState.BUZZING
        worker.record_revive()
        d.drone_log.add(
            DroneAction.OPERATOR, name, "revived (manual)", category=LogCategory.OPERATOR
        )
        d.broadcast_ws({"type": "workers_changed"})

    async def kill_session(self, *, all_sessions: bool = False) -> None:
        """Kill the tmux session (or all sessions if all_sessions=True)."""
        from swarm.tmux.hive import kill_all_sessions, kill_session as _kill_session

        d = self._daemon
        if d.pilot:
            d.pilot.stop()

        for w in list(d.workers):
            d.task_board.unassign_worker(w.name)

        try:
            if all_sessions:
                await kill_all_sessions()
            else:
                await _kill_session(d.config.session_name)
        except OSError:
            _log.warning("kill_session failed (session may already be gone)", exc_info=True)

        async with d._worker_lock:
            d.workers.clear()
        d.drone_log.clear()
        d.broadcast_ws({"type": "workers_changed"})

    # --- Bulk operations ---

    async def _send_to_workers(
        self,
        workers: list[Worker],
        action: Callable[[str], Awaitable[None]],
        log_actor: str,
        log_detail: str,
    ) -> int:
        """Send an action to a list of workers. Returns count of successes."""
        count = 0
        for w in workers:
            try:
                await action(w.pane_id)
                count += 1
            except (OSError, asyncio.TimeoutError, TmuxError, PaneGoneError):
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
        """Send Enter to all RESTING/WAITING workers."""
        d = self._daemon
        targets = [w for w in d.workers if w.state in (WorkerState.RESTING, WorkerState.WAITING)]
        return await self._send_to_workers(
            targets, send_enter, "all", "continued {count} worker(s)"
        )

    async def send_all(self, message: str) -> int:
        """Send a message to all workers."""
        d = self._daemon
        preview = message[:80] + ("\u2026" if len(message) > 80 else "")
        return await self._send_to_workers(
            list(d.workers),
            lambda pane_id: send_keys(pane_id, message),
            "all",
            f'broadcast to {{count}} worker(s): "{preview}"',
        )

    async def send_group(self, group_name: str, message: str) -> int:
        """Send a message to all workers in a group."""
        d = self._daemon
        group_workers = d.config.get_group(group_name)
        group_names = {w.name.lower() for w in group_workers}
        targets = [w for w in d.workers if w.name.lower() in group_names]
        preview = message[:80] + ("\u2026" if len(message) > 80 else "")
        return await self._send_to_workers(
            targets,
            lambda pane_id: send_keys(pane_id, message),
            group_name,
            f'group send to {{count}} worker(s): "{preview}"',
        )
