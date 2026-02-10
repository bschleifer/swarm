"""SwarmDaemon — long-running backend service."""

from __future__ import annotations

import asyncio
import json
import time
from typing import Set

from aiohttp import web

from pathlib import Path

from swarm.config import HiveConfig, WorkerConfig, load_config, save_config
from swarm.drones.log import DroneLog
from swarm.drones.pilot import DronePilot
from swarm.events import EventEmitter
from swarm.logging import get_logger
from swarm.notify.bus import NotificationBus
from swarm.notify.desktop import desktop_backend
from swarm.notify.terminal import terminal_bell_backend
from swarm.queen.queen import Queen
from swarm.tasks.board import TaskBoard
from swarm.tasks.store import FileTaskStore
from swarm.tasks.task import SwarmTask, TaskPriority, TaskStatus
from swarm.tmux.hive import discover_workers, find_swarm_session
from swarm.worker.worker import Worker, WorkerState

_log = get_logger("server.daemon")


# --- Exception classes ---


class SwarmOperationError(Exception):
    """Base for daemon operation errors."""


class WorkerNotFoundError(SwarmOperationError):
    """Referenced worker does not exist."""


class TaskOperationError(SwarmOperationError):
    """Task op failed (not found, wrong state)."""


class SwarmDaemon(EventEmitter):
    """Long-running backend service for the swarm."""

    def __init__(self, config: HiveConfig) -> None:
        self.__init_emitter__()
        self.config = config
        self.workers: list[Worker] = []
        self._worker_lock = asyncio.Lock()
        # Persistence: tasks and drone log survive restarts
        task_store = FileTaskStore()
        drone_log_path = Path.home() / ".swarm" / "drone.jsonl"
        self.drone_log = DroneLog(log_file=drone_log_path)
        self.task_board = TaskBoard(store=task_store)
        self.queen = Queen(config=config.queen, session_name=config.session_name)
        self.notification_bus = self._build_notification_bus(config)
        self.pilot: DronePilot | None = None
        self.ws_clients: Set[web.WebSocketResponse] = set()
        self.start_time = time.time()
        self._config_mtime: float = 0.0
        self._mtime_task: asyncio.Task | None = None
        self._wire_task_board()

    def _wire_task_board(self) -> None:
        """Wire task_board.on_change to auto-broadcast to WS clients."""
        self.task_board.on_change(self._on_task_board_changed)

    def _on_task_board_changed(self) -> None:
        self._broadcast_ws({"type": "tasks_changed"})

    def _build_notification_bus(self, config: HiveConfig) -> NotificationBus:
        bus = NotificationBus(debounce_seconds=config.notifications.debounce_seconds)
        if config.notifications.terminal_bell:
            bus.add_backend(terminal_bell_backend)
        if config.notifications.desktop:
            bus.add_backend(desktop_backend)
        return bus

    def init_pilot(self, *, enabled: bool = True) -> DronePilot:
        """Create, wire, and start the drone pilot. Returns the pilot instance."""
        self.pilot = DronePilot(
            self.workers,
            self.drone_log,
            self.config.watch_interval,
            session_name=self.config.session_name,
            drone_config=self.config.drones,
            task_board=self.task_board,
            queen=self.queen,
        )
        self.pilot.on_escalate(self._on_escalation)
        self.pilot.on_workers_changed(self._on_workers_changed)
        self.pilot.on_task_assigned(self._on_task_assigned)
        self.pilot.on_state_changed(self._on_state_changed)
        self.drone_log.on_entry(self._on_drone_entry)

        self.pilot.start()
        self.pilot.enabled = enabled
        _log.info("pilot initialized (enabled=%s)", enabled)
        return self.pilot

    async def start(self) -> None:
        """Discover workers and start the pilot loop."""
        # Auto-discover session if the configured one doesn't have workers
        await self.discover()

        if not self.workers:
            session = self.config.session_name
            _log.info("no workers in session '%s', auto-discovering...", session)
            found = await find_swarm_session()
            if found and found != session:
                _log.info("found swarm session: '%s'", found)
                self.config.session_name = found
                await self.discover()

        if not self.workers:
            _log.warning("no workers found for session '%s'", session)
            return

        _log.info("found %d workers", len(self.workers))
        self.init_pilot(enabled=True)
        _log.info("daemon started — drone pilot active")

        # Start config file mtime watcher
        if self.config.source_path:
            sp = Path(self.config.source_path)
            if sp.exists():
                self._config_mtime = sp.stat().st_mtime
        self._mtime_task = asyncio.create_task(self._watch_config_mtime())

    def _on_escalation(self, worker: Worker, reason: str) -> None:
        self.notification_bus.emit_escalation(worker.name, reason)
        self._broadcast_ws(
            {
                "type": "escalation",
                "worker": worker.name,
                "reason": reason,
            }
        )
        self.emit("escalation", worker, reason)

    def _on_workers_changed(self) -> None:
        self._broadcast_ws(
            {
                "type": "workers_changed",
                "workers": [{"name": w.name, "state": w.state.value} for w in self.workers],
            }
        )
        self.emit("workers_changed")

    def _on_task_assigned(self, worker: Worker, task) -> None:
        self.notification_bus.emit_task_assigned(worker.name, task.title)
        self._broadcast_ws(
            {
                "type": "task_assigned",
                "worker": worker.name,
                "task": {"id": task.id, "title": task.title},
            }
        )
        self.emit("task_assigned", worker, task)

    def _on_state_changed(self, worker: Worker) -> None:
        """Called when any worker changes state — push to WS clients."""
        self._broadcast_ws(
            {
                "type": "state",
                "workers": [
                    {
                        "name": w.name,
                        "state": w.state.value,
                        "state_duration": round(w.state_duration, 1),
                    }
                    for w in self.workers
                ],
            }
        )

    def _on_drone_entry(self, entry) -> None:
        self._broadcast_ws(
            {
                "type": "drones",
                "action": entry.action.value,
                "worker": entry.worker_name,
                "detail": entry.detail,
            }
        )

    def _hot_apply_config(self) -> None:
        """Apply config changes to pilot, queen, and notification bus."""
        if self.pilot:
            self.pilot.drone_config = self.config.drones
            self.pilot._base_interval = self.config.drones.poll_interval
            self.pilot._max_interval = self.config.drones.max_idle_interval
            self.pilot.interval = self.config.drones.poll_interval

        self.queen.config = self.config.queen
        self.notification_bus = self._build_notification_bus(self.config)

    async def reload_config(self, new_config: HiveConfig) -> None:
        """Hot-reload configuration. Updates pilot, queen, and notifies WS clients."""
        self.config = new_config
        self._hot_apply_config()

        # Update mtime tracker
        if new_config.source_path:
            sp = Path(new_config.source_path)
            if sp.exists():
                self._config_mtime = sp.stat().st_mtime

        self._broadcast_ws({"type": "config_changed"})
        _log.info("config hot-reloaded")

    async def _watch_config_mtime(self) -> None:
        """Poll config file mtime every 30s and notify WS clients if changed."""
        try:
            while True:
                await asyncio.sleep(30)
                if not self.config.source_path:
                    continue
                try:
                    sp = Path(self.config.source_path)
                    if sp.exists():
                        mtime = sp.stat().st_mtime
                        if mtime > self._config_mtime:
                            self._config_mtime = mtime
                            self._broadcast_ws({"type": "config_file_changed"})
                            _log.info("config file changed on disk")
                except Exception:
                    _log.debug("mtime check failed", exc_info=True)
        except asyncio.CancelledError:
            return

    def _broadcast_ws(self, data: dict) -> None:
        """Send a message to all connected WebSocket clients."""
        if not self.ws_clients:
            return
        payload = json.dumps(data)
        dead: list[web.WebSocketResponse] = []
        for ws in self.ws_clients:
            try:
                asyncio.ensure_future(ws.send_str(payload))
            except Exception:
                _log.debug("WebSocket send failed, marking client as dead")
                dead.append(ws)
        for ws in dead:
            self.ws_clients.discard(ws)

    async def stop(self) -> None:
        if self.pilot:
            self.pilot.stop()
        if self._mtime_task:
            self._mtime_task.cancel()
        # Close all WebSocket connections so runner.cleanup() doesn't hang
        for ws in list(self.ws_clients):
            try:
                await ws.close()
            except Exception:
                pass
        self.ws_clients.clear()
        _log.info("daemon stopped")

    # --- Lookup helper ---

    def get_worker(self, name: str) -> Worker | None:
        """Find a worker by name."""
        return next((w for w in self.workers if w.name == name), None)

    def _require_worker(self, name: str) -> Worker:
        """Get a worker by name or raise WorkerNotFoundError."""
        worker = self.get_worker(name)
        if not worker:
            raise WorkerNotFoundError(f"Worker '{name}' not found")
        return worker

    # --- Per-worker tmux operations ---

    async def send_to_worker(self, name: str, message: str) -> None:
        """Send text to a worker's tmux pane."""
        from swarm.tmux.cell import send_keys

        worker = self._require_worker(name)
        await send_keys(worker.pane_id, message)

    async def continue_worker(self, name: str) -> None:
        """Send Enter to a worker's tmux pane."""
        from swarm.tmux.cell import send_enter

        worker = self._require_worker(name)
        await send_enter(worker.pane_id)

    async def interrupt_worker(self, name: str) -> None:
        """Send Ctrl-C to a worker's tmux pane."""
        from swarm.tmux.cell import send_interrupt

        worker = self._require_worker(name)
        await send_interrupt(worker.pane_id)

    async def escape_worker(self, name: str) -> None:
        """Send Escape to a worker's tmux pane."""
        from swarm.tmux.cell import send_escape

        worker = self._require_worker(name)
        await send_escape(worker.pane_id)

    async def capture_worker_output(self, name: str, lines: int = 80) -> str:
        """Capture a worker's tmux pane content."""
        from swarm.tmux.cell import capture_pane

        worker = self._require_worker(name)
        return await capture_pane(worker.pane_id, lines=lines)

    async def discover(self) -> list[Worker]:
        """Discover workers in the configured tmux session. Updates self.workers."""
        self.workers = await discover_workers(self.config.session_name)
        return self.workers

    async def poll_once(self) -> bool:
        """Run one pilot poll cycle. Returns True if any action was taken."""
        if not self.pilot:
            return False
        return await self.pilot.poll_once()

    # --- Operation methods ---

    async def launch_workers(self, worker_configs: list[WorkerConfig]) -> list[Worker]:
        """Launch workers in tmux. Extends self.workers and updates pilot."""
        from swarm.worker.manager import launch_hive

        launched = await launch_hive(
            self.config.session_name,
            worker_configs,
            self.config.panes_per_window,
        )
        async with self._worker_lock:
            self.workers.extend(launched)
        if self.pilot:
            self.pilot.workers = self.workers
        else:
            self.init_pilot(enabled=True)
        self._broadcast_ws({"type": "workers_changed"})
        return launched

    async def spawn_worker(self, worker_config: WorkerConfig) -> Worker:
        """Spawn a single worker into the running session."""
        if any(w.name.lower() == worker_config.name.lower() for w in self.workers):
            raise SwarmOperationError(f"Worker '{worker_config.name}' already running")

        from swarm.worker.manager import add_worker_live

        async with self._worker_lock:
            worker = await add_worker_live(
                self.config.session_name,
                worker_config,
                self.workers,
                self.config.panes_per_window,
            )
        if self.pilot:
            self.pilot.workers = self.workers
        self._broadcast_ws({"type": "workers_changed"})
        return worker

    async def kill_worker(self, name: str) -> None:
        """Kill a worker: mark STUNG, unassign tasks, broadcast."""
        from swarm.worker.manager import kill_worker as _kill_worker

        worker = self._require_worker(name)

        async with self._worker_lock:
            await _kill_worker(worker)
            worker.state = WorkerState.STUNG
        self.task_board.unassign_worker(worker.name)
        self._broadcast_ws(
            {
                "type": "workers_changed",
                "workers": [{"name": w.name, "state": w.state.value} for w in self.workers],
            }
        )

    async def revive_worker(self, name: str) -> None:
        """Revive a STUNG worker: always passes session_name and calls record_revive."""
        from swarm.worker.manager import revive_worker as _revive_worker

        worker = self._require_worker(name)
        if worker.state != WorkerState.STUNG:
            raise SwarmOperationError(f"Worker '{name}' is {worker.state.value}, not STUNG")

        await _revive_worker(worker, session_name=self.config.session_name)
        worker.state = WorkerState.BUZZING
        worker.record_revive()
        self._broadcast_ws({"type": "workers_changed"})

    async def kill_session(self) -> None:
        """Kill the entire tmux session: stop pilot, unassign all, kill tmux, clear state."""
        from swarm.tmux.hive import kill_session as _kill_session

        if self.pilot:
            self.pilot.stop()

        for w in list(self.workers):
            self.task_board.unassign_worker(w.name)

        try:
            await _kill_session(self.config.session_name)
        except Exception:
            _log.warning("kill_session failed (session may already be gone)", exc_info=True)

        async with self._worker_lock:
            self.workers.clear()
        self.drone_log.clear()
        self._broadcast_ws({"type": "workers_changed"})

    def create_task(
        self,
        title: str,
        description: str = "",
        priority: TaskPriority = TaskPriority.NORMAL,
        tags: list[str] | None = None,
        depends_on: list[str] | None = None,
    ) -> SwarmTask:
        """Create a task. Broadcast happens via task_board.on_change."""
        return self.task_board.create(
            title=title,
            description=description,
            priority=priority,
            tags=tags,
            depends_on=depends_on,
        )

    def assign_task(self, task_id: str, worker_name: str) -> bool:
        """Assign a task to a worker. Validates both exist."""
        self._require_worker(worker_name)

        task = self.task_board.get(task_id)
        if not task:
            raise TaskOperationError(f"Task '{task_id}' not found")
        if not task.is_available:
            raise TaskOperationError(f"Task '{task_id}' is not available ({task.status.value})")

        return self.task_board.assign(task_id, worker_name)

    def complete_task(self, task_id: str) -> bool:
        """Complete a task. Raises if not found or wrong state."""
        task = self.task_board.get(task_id)
        if not task:
            raise TaskOperationError(f"Task '{task_id}' not found")
        if task.status not in (TaskStatus.ASSIGNED, TaskStatus.IN_PROGRESS):
            raise TaskOperationError(f"Task '{task_id}' cannot be completed ({task.status.value})")
        return self.task_board.complete(task_id)

    def fail_task(self, task_id: str) -> bool:
        """Fail a task. Raises if not found."""
        task = self.task_board.get(task_id)
        if not task:
            raise TaskOperationError(f"Task '{task_id}' not found")
        return self.task_board.fail(task_id)

    def remove_task(self, task_id: str) -> bool:
        """Remove a task. Raises if not found."""
        if not self.task_board.remove(task_id):
            raise TaskOperationError(f"Task '{task_id}' not found")
        return True

    def edit_task(
        self,
        task_id: str,
        title: str | None = None,
        description: str | None = None,
        priority: TaskPriority | None = None,
        tags: list[str] | None = None,
        attachments: list[str] | None = None,
    ) -> bool:
        """Edit a task. Raises if not found."""
        task = self.task_board.get(task_id)
        if not task:
            raise TaskOperationError(f"Task '{task_id}' not found")
        return self.task_board.update(
            task_id,
            title=title,
            description=description,
            priority=priority,
            tags=tags,
            attachments=attachments,
        )

    def save_attachment(self, filename: str, data: bytes) -> str:
        """Save an uploaded file to ~/.swarm/uploads/ and return the absolute path."""
        import hashlib

        uploads_dir = Path.home() / ".swarm" / "uploads"
        uploads_dir.mkdir(parents=True, exist_ok=True)
        # SHA-256 prefix for uniqueness
        digest = hashlib.sha256(data).hexdigest()[:12]
        safe_name = Path(filename).name  # strip directory components
        dest = uploads_dir / f"{digest}_{safe_name}"
        dest.write_bytes(data)
        return str(dest)

    def toggle_drones(self) -> bool:
        """Toggle drone pilot. Returns new enabled state."""
        if not self.pilot:
            return False
        new_state = self.pilot.toggle()
        self._broadcast_ws({"type": "drones_toggled", "enabled": new_state})
        return new_state

    def check_config_file(self) -> bool:
        """Check if config file changed on disk; reload if so. Returns True if reloaded."""
        if not self.config.source_path:
            return False
        try:
            current_mtime = Path(self.config.source_path).stat().st_mtime
        except OSError:
            return False
        if current_mtime <= self._config_mtime:
            return False
        self._config_mtime = current_mtime
        try:
            new_config = load_config(self.config.source_path)
        except Exception:
            _log.warning("failed to reload config from disk", exc_info=True)
            return False

        # Hot-apply fields that don't require worker lifecycle changes
        self.config.groups = new_config.groups
        self.config.drones = new_config.drones
        self.config.queen = new_config.queen
        self.config.notifications = new_config.notifications
        self.config.workers = new_config.workers
        self.config.api_password = new_config.api_password

        self._hot_apply_config()

        _log.info("config reloaded from disk (external change detected)")
        return True

    async def continue_all(self) -> int:
        """Send Enter to all RESTING workers. Returns count of workers continued."""
        from swarm.tmux.cell import send_enter

        count = 0
        for w in list(self.workers):
            if w.state == WorkerState.RESTING:
                try:
                    await send_enter(w.pane_id)
                    count += 1
                except Exception:
                    _log.debug("failed to send enter to %s", w.name)
        return count

    async def send_all(self, message: str) -> int:
        """Send a message to all workers. Returns count sent."""
        from swarm.tmux.cell import send_keys

        count = 0
        for w in list(self.workers):
            try:
                await send_keys(w.pane_id, message)
                count += 1
            except Exception:
                _log.debug("failed to send to %s", w.name)
        return count

    async def send_group(self, group_name: str, message: str) -> int:
        """Send a message to all workers in a group. Returns count sent."""
        from swarm.tmux.cell import send_keys

        group_workers = self.config.get_group(group_name)
        group_names = {w.name.lower() for w in group_workers}

        count = 0
        for w in list(self.workers):
            if w.name.lower() in group_names:
                try:
                    await send_keys(w.pane_id, message)
                    count += 1
                except Exception:
                    _log.debug("failed to send to %s", w.name)
        return count

    async def gather_hive_context(self) -> str:
        """Capture all worker panes and build hive context string for the Queen."""
        from swarm.queen.context import build_hive_context
        from swarm.tmux.cell import capture_pane

        worker_outputs: dict[str, str] = {}
        for w in list(self.workers):
            try:
                worker_outputs[w.name] = await capture_pane(w.pane_id, lines=20)
            except Exception:
                _log.debug("failed to capture pane for %s in queen flow", w.name)
        return build_hive_context(
            list(self.workers),
            worker_outputs=worker_outputs,
            drone_log=self.drone_log,
            task_board=self.task_board,
        )

    async def analyze_worker(self, worker_name: str) -> dict:
        """Run Queen analysis on a specific worker. Returns Queen's analysis dict."""
        from swarm.tmux.cell import capture_pane

        worker = self._require_worker(worker_name)
        content = await capture_pane(worker.pane_id)
        hive_ctx = await self.gather_hive_context()
        return await self.queen.analyze_worker(worker.name, content, hive_context=hive_ctx)

    async def coordinate_hive(self) -> dict:
        """Run Queen coordination across the entire hive. Returns coordination dict."""
        hive_ctx = await self.gather_hive_context()
        return await self.queen.coordinate_hive(hive_ctx)

    def save_config(self) -> None:
        """Save config to disk and update mtime to prevent self-triggered reload."""
        save_config(self.config)
        if self.config.source_path:
            try:
                self._config_mtime = Path(self.config.source_path).stat().st_mtime
            except OSError:
                pass


async def run_daemon(config: HiveConfig, host: str = "localhost", port: int = 9090) -> None:
    """Start the daemon with HTTP server."""
    import signal

    from swarm.server.api import create_app

    daemon = SwarmDaemon(config)
    await daemon.start()

    app = create_app(daemon)

    # Graceful shutdown via signal — avoids KeyboardInterrupt race with aiohttp
    shutdown = asyncio.Event()
    app["shutdown_event"] = shutdown

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()

    _print_banner(daemon, host, port)

    # Wire runtime event logging to console
    if daemon.pilot:
        daemon.pilot.on_state_changed(
            lambda w: console_log(f'Worker "{w.name}" state -> {w.state.value}')
        )
        daemon.pilot.on_task_assigned(
            lambda w, t: console_log(f'Task "{t.title}" assigned -> {w.name}')
        )
        daemon.pilot.on_workers_changed(lambda: console_log("Workers changed (add/remove)"))
        daemon.pilot.on_hive_empty(lambda: console_log("All workers gone", level="warn"))
        daemon.pilot.on_hive_complete(lambda: console_log("Hive complete — all tasks done"))

    daemon.task_board.on_change(lambda: console_log("Task board updated"))

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, shutdown.set)

    await shutdown.wait()
    print("\nShutting down...", flush=True)

    await daemon.stop()
    await runner.cleanup()


def _print_banner(daemon: SwarmDaemon, host: str, port: int) -> None:
    """Print NestJS-style structured startup banner."""
    import importlib.metadata

    try:
        version = importlib.metadata.version("swarm-ai")
    except importlib.metadata.PackageNotFoundError:
        version = "dev"

    Y = "\033[33m"  # yellow/honey
    C = "\033[36m"  # cyan
    D = "\033[2m"  # dim
    B = "\033[1m"  # bold
    R = "\033[0m"  # reset

    n_workers = len(daemon.workers)
    drones_enabled = daemon.pilot.enabled if daemon.pilot else False
    interval = daemon.config.drones.poll_interval
    queen_model = getattr(daemon.config.queen, "model", "sonnet")
    task_summary = daemon.task_board.summary()

    print(f"\n{Y}{B}Swarm WUI v{version}{R}", flush=True)
    print(f"  {D}\u251c\u2500{R} Dashboard:  {C}http://{host}:{port}{R}", flush=True)
    print(f"  {D}\u251c\u2500{R} API:        {C}http://{host}:{port}/api/health{R}", flush=True)
    print(f"  {D}\u251c\u2500{R} WebSocket:  {C}ws://{host}:{port}/ws{R}", flush=True)
    print(f"  {D}\u251c\u2500{R} Workers:    {Y}{n_workers}{R} discovered", flush=True)
    drones_str = f"enabled (interval {interval}s)" if drones_enabled else "disabled"
    print(f"  {D}\u251c\u2500{R} Drones:     {drones_str}", flush=True)
    print(f"  {D}\u251c\u2500{R} Queen:      ready (model: {queen_model})", flush=True)
    print(f"  {D}\u2514\u2500{R} Tasks:      {task_summary}", flush=True)
    print(flush=True)


def console_log(msg: str, level: str = "info") -> None:
    """Print a timestamped runtime event to the console."""
    from datetime import datetime

    ts = datetime.now().strftime("%H:%M:%S")
    if level == "warn":
        prefix = "\033[33m\u26a0\033[0m"
    elif level == "error":
        prefix = "\033[31m\u2717\033[0m"
    else:
        prefix = " "
    print(f"[{ts}] {prefix} {msg}", flush=True)
