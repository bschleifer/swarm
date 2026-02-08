"""SwarmDaemon — long-running backend service."""

from __future__ import annotations

import asyncio
import json
import time
from typing import Set

from aiohttp import web

from pathlib import Path

from swarm.buzz.log import BuzzLog
from swarm.buzz.pilot import BuzzPilot
from swarm.config import HiveConfig
from swarm.logging import get_logger
from swarm.notify.bus import NotificationBus
from swarm.notify.desktop import desktop_backend
from swarm.notify.terminal import terminal_bell_backend
from swarm.queen.queen import Queen
from swarm.tasks.board import TaskBoard
from swarm.tasks.store import FileTaskStore
from swarm.tmux.hive import discover_workers, find_swarm_session
from swarm.worker.worker import Worker

_log = get_logger("server.daemon")


class SwarmDaemon:
    """Long-running backend service for the swarm."""

    def __init__(self, config: HiveConfig) -> None:
        self.config = config
        self.workers: list[Worker] = []
        self._worker_lock = asyncio.Lock()
        # Persistence: tasks and buzz log survive restarts
        task_store = FileTaskStore()
        buzz_log_path = Path.home() / ".swarm" / "buzz.jsonl"
        self.buzz_log = BuzzLog(log_file=buzz_log_path)
        self.task_board = TaskBoard(store=task_store)
        self.queen = Queen(config=config.queen, session_name=config.session_name)
        self.notification_bus = self._build_notification_bus(config)
        self.pilot: BuzzPilot | None = None
        self.ws_clients: Set[web.WebSocketResponse] = set()
        self.start_time = time.time()
        self._config_mtime: float = 0.0
        self._mtime_task: asyncio.Task | None = None

    def _build_notification_bus(self, config: HiveConfig) -> NotificationBus:
        bus = NotificationBus(debounce_seconds=config.notifications.debounce_seconds)
        if config.notifications.terminal_bell:
            bus.add_backend(terminal_bell_backend)
        if config.notifications.desktop:
            bus.add_backend(desktop_backend)
        return bus

    async def start(self) -> None:
        """Discover workers and start the pilot loop."""
        # Auto-discover session if the configured one doesn't have workers
        session = self.config.session_name
        self.workers = await discover_workers(session)

        if not self.workers:
            _log.info("no workers in session '%s', auto-discovering...", session)
            found = await find_swarm_session()
            if found and found != session:
                _log.info("found swarm session: '%s'", found)
                self.config.session_name = found
                session = found
                self.workers = await discover_workers(session)

        if not self.workers:
            _log.warning("no workers found for session '%s'", session)
            return

        _log.info("found %d workers", len(self.workers))

        self.pilot = BuzzPilot(
            self.workers,
            self.buzz_log,
            self.config.watch_interval,
            session_name=self.config.session_name,
            buzz_config=self.config.buzz,
            task_board=self.task_board,
            queen=self.queen,
        )
        self.pilot.on_escalate(self._on_escalation)
        self.pilot.on_workers_changed(self._on_workers_changed)
        self.pilot.on_task_assigned(self._on_task_assigned)
        self.pilot.on_state_changed(self._on_state_changed)
        self.buzz_log.on_entry(self._on_buzz_entry)

        self.pilot.start()
        _log.info("daemon started — buzz pilot active")

        # Start config file mtime watcher
        if self.config.source_path:
            sp = Path(self.config.source_path)
            if sp.exists():
                self._config_mtime = sp.stat().st_mtime
        self._mtime_task = asyncio.create_task(self._watch_config_mtime())

    def _on_escalation(self, worker: Worker, reason: str) -> None:
        self.notification_bus.emit_escalation(worker.name, reason)
        self._broadcast_ws({
            "type": "escalation",
            "worker": worker.name,
            "reason": reason,
        })

    def _on_workers_changed(self) -> None:
        self._broadcast_ws({
            "type": "workers_changed",
            "workers": [
                {"name": w.name, "state": w.state.value}
                for w in self.workers
            ],
        })

    def _on_task_assigned(self, worker: Worker, task) -> None:
        self.notification_bus.emit_task_assigned(worker.name, task.title)
        self._broadcast_ws({
            "type": "task_assigned",
            "worker": worker.name,
            "task": {"id": task.id, "title": task.title},
        })

    def _on_state_changed(self, worker: Worker) -> None:
        """Called when any worker changes state — push to WS clients."""
        self._broadcast_ws({
            "type": "state",
            "workers": [
                {"name": w.name, "state": w.state.value, "state_duration": round(w.state_duration, 1)}
                for w in self.workers
            ],
        })

    def _on_buzz_entry(self, entry) -> None:
        self._broadcast_ws({
            "type": "buzz",
            "action": entry.action.value,
            "worker": entry.worker_name,
            "detail": entry.detail,
        })

    async def reload_config(self, new_config: HiveConfig) -> None:
        """Hot-reload configuration. Updates pilot, queen, and notifies WS clients."""
        self.config = new_config

        # Update pilot settings
        if self.pilot:
            self.pilot.buzz_config = new_config.buzz
            self.pilot._base_interval = new_config.buzz.poll_interval
            self.pilot._max_interval = new_config.buzz.max_idle_interval
            self.pilot.interval = new_config.buzz.poll_interval

        # Update queen
        self.queen.config = new_config.queen

        # Rebuild notification bus
        self.notification_bus = self._build_notification_bus(new_config)

        # Update mtime tracker
        if new_config.source_path:
            sp = Path(new_config.source_path)
            if sp.exists():
                self._config_mtime = sp.stat().st_mtime

        self._broadcast_ws({"type": "config_changed"})
        _log.info("config hot-reloaded")

    async def _watch_config_mtime(self) -> None:
        """Poll config file mtime every 30s and notify WS clients if changed."""
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

    def stop(self) -> None:
        if self.pilot:
            self.pilot.stop()
        if self._mtime_task:
            self._mtime_task.cancel()
        _log.info("daemon stopped")


async def run_daemon(config: HiveConfig, host: str = "localhost", port: int = 8081) -> None:
    """Start the daemon with HTTP server."""
    from swarm.server.api import create_app

    daemon = SwarmDaemon(config)
    await daemon.start()

    app = create_app(daemon)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()

    _log.info("API server listening on http://%s:%d", host, port)
    print(f"Swarm daemon running — http://{host}:{port}")
    print(f"Workers: {len(daemon.workers)}")
    print(f"API: http://{host}:{port}/api/health")
    print(f"WebSocket: ws://{host}:{port}/ws")

    try:
        while True:
            await asyncio.sleep(3600)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        daemon.stop()
        await runner.cleanup()
