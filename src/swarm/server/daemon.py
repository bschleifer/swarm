"""SwarmDaemon — long-running backend service."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from aiohttp import web

from pathlib import Path

if TYPE_CHECKING:
    from swarm.auth.graph import GraphTokenManager as GraphManager

from swarm.config import HiveConfig, WorkerConfig, load_config, save_config
from swarm.drones.log import DroneAction, DroneLog, LogCategory, SystemAction, SystemEntry
from swarm.drones.pilot import DronePilot
from swarm.events import EventEmitter
from swarm.logging import get_logger
from swarm.notify.bus import NotificationBus
from swarm.server.analyzer import QueenAnalyzer
from swarm.server.email_service import EmailService
from swarm.server.proposals import ProposalManager
from swarm.server.task_manager import TaskManager
from swarm.notify.desktop import desktop_backend
from swarm.notify.terminal import terminal_bell_backend
from swarm.queen.queen import Queen
from swarm.tasks.board import TaskBoard
from swarm.tasks.history import TaskAction, TaskHistory
from swarm.tasks.proposal import (
    AssignmentProposal,
    ProposalStatus,
    ProposalStore,
)
from swarm.tasks.store import FileTaskStore
from swarm.tasks.task import (
    SwarmTask,
    TaskPriority,
    TaskStatus,
    TaskType,
)
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
        # Persistence: tasks and system log survive restarts
        task_store = FileTaskStore()
        system_log_path = Path.home() / ".swarm" / "system.jsonl"
        self.drone_log = DroneLog(log_file=system_log_path)
        self.task_board = TaskBoard(store=task_store)
        self.task_history = TaskHistory()
        self.queen = Queen(config=config.queen, session_name=config.session_name)
        self.proposal_store = ProposalStore()
        self.proposals = ProposalManager(self.proposal_store, self)
        self.analyzer = QueenAnalyzer(self.queen, self)
        self.notification_bus = self._build_notification_bus(config)
        # Apply workflow skill overrides from config
        if config.workflows:
            from swarm.tasks.workflows import apply_config_overrides

            apply_config_overrides(config.workflows)
        self.pilot: DronePilot | None = None
        self.ws_clients: set[web.WebSocketResponse] = set()
        self.terminal_ws_clients: set[web.WebSocketResponse] = set()
        # In-flight Queen analysis tracking lives on self.analyzer
        self.start_time = time.time()
        self._config_mtime: float = 0.0
        self._mtime_task: asyncio.Task | None = None
        # Microsoft Graph OAuth
        self.graph_mgr = self._build_graph_manager(config)
        self._graph_auth_pending: dict[str, str] = {}  # state → code_verifier
        # Email service (attachments, draft replies, email processing)
        self.email = EmailService(
            drone_log=self.drone_log,
            queen=self.queen,
            graph_mgr=self.graph_mgr,
            broadcast_ws=self.broadcast_ws,
        )
        # Task lifecycle manager (create, edit, status transitions)
        self.tasks = TaskManager(
            task_board=self.task_board,
            task_history=self.task_history,
            drone_log=self.drone_log,
        )
        self._wire_task_board()

    def _wire_task_board(self) -> None:
        """Wire task_board.on_change to auto-broadcast to WS clients."""
        self.task_board.on_change(self._on_task_board_changed)

    def _on_task_board_changed(self) -> None:
        self.broadcast_ws({"type": "tasks_changed"})
        self._expire_stale_proposals()

    def _build_notification_bus(self, config: HiveConfig) -> NotificationBus:
        bus = NotificationBus(debounce_seconds=config.notifications.debounce_seconds)
        if config.notifications.terminal_bell:
            bus.add_backend(terminal_bell_backend)
        if config.notifications.desktop:
            bus.add_backend(desktop_backend)
        return bus

    @staticmethod
    def _build_graph_manager(config: HiveConfig) -> GraphManager | None:
        """Build a GraphTokenManager if Graph client_id is configured."""
        if not config.graph_client_id:
            return None
        from swarm.auth.graph import GraphTokenManager

        return GraphTokenManager(config.graph_client_id, config.graph_tenant_id, port=config.port)

    def _worker_descriptions(self) -> dict[str, str]:
        """Build a name→description map from config workers."""
        return {w.name: w.description for w in self.config.workers if w.description}

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
            worker_descriptions=self._worker_descriptions(),
        )
        self.pilot.on_escalate(self._on_escalation)
        self.pilot.on_workers_changed(self._on_workers_changed)
        self.pilot.on_task_assigned(self._on_task_assigned)
        self.pilot.on_state_changed(self._on_state_changed)
        self.pilot.on_proposal(self.queue_proposal)
        self.pilot.on_task_done(self._on_task_done)
        self.pilot._pending_proposals_check = lambda: bool(self.proposal_store.pending)
        self.drone_log.on_entry(self._on_drone_entry)

        self.tasks._pilot = self.pilot
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
        self.init_pilot(enabled=self.config.drones.enabled)
        _log.info(
            "daemon started — drone pilot %s",
            "active" if self.config.drones.enabled else "disabled",
        )

        # Start config file mtime watcher
        if self.config.source_path:
            sp = Path(self.config.source_path)
            if sp.exists():
                self._config_mtime = sp.stat().st_mtime
        self._mtime_task = asyncio.create_task(self._watch_config_mtime())

    def _on_escalation(self, worker: Worker, reason: str) -> None:
        # Skip if there's already a pending escalation proposal for this worker
        if self.proposal_store.has_pending_escalation(worker.name):
            _log.debug("skipping escalation for %s — pending proposal exists", worker.name)
            return

        # Skip if Queen is already analyzing this worker
        if self.analyzer.has_inflight_escalation(worker.name):
            _log.debug("skipping escalation for %s — analysis already in flight", worker.name)
            return

        self.notification_bus.emit_escalation(worker.name, reason)
        self.broadcast_ws(
            {
                "type": "escalation",
                "worker": worker.name,
                "reason": reason,
            }
        )
        self.emit("escalation", worker, reason)

        # Trigger Queen analysis if enabled
        if self.queen.enabled and self.queen.can_call:
            try:
                asyncio.get_running_loop()
                self.analyzer.track_escalation(worker.name)
                asyncio.ensure_future(self.analyzer.analyze_escalation(worker, reason))
            except RuntimeError:
                self.analyzer.clear_escalation(worker.name)

    def _on_task_done(self, worker: Worker, task: SwarmTask, resolution: str = "") -> None:
        """Handle a task that appears complete — create a proposal for user approval."""
        # Guard: worker must still be idle — if it resumed working, skip
        if worker.state == WorkerState.BUZZING:
            _log.info(
                "Ignoring task_done for '%s': worker %s is BUZZING",
                task.title,
                worker.name,
            )
            return

        # Skip if already pending
        if self.proposal_store.has_pending_completion(worker.name, task.id):
            return

        if resolution:
            # Queen coordination already provided a resolution — create proposal directly
            proposal = AssignmentProposal.completion(
                worker_name=worker.name,
                task_id=task.id,
                task_title=task.title,
                assessment=resolution,
                reasoning=f"Worker {worker.name} idle for {worker.state_duration:.0f}s",
            )
            self.queue_proposal(proposal)
        elif self.queen.enabled and self.queen.can_call:
            key = f"{worker.name}:{task.id}"
            if self.analyzer.has_inflight_completion(key):
                _log.debug("skipping completion analysis for %s — already in flight", key)
                return
            try:
                asyncio.get_running_loop()
                self.analyzer.track_completion(key)
                asyncio.ensure_future(self.analyzer.analyze_completion(worker, task))
            except RuntimeError:
                self.analyzer.clear_completion(key)
        else:
            # Queen unavailable — skip proposal (no way to assess completion)
            _log.info(
                "Queen unavailable — cannot assess completion for task '%s' on %s",
                task.title,
                worker.name,
            )

    def _worker_task_map(self) -> dict[str, str]:
        """Return {worker_name: task_title} for all assigned/in-progress tasks."""
        result: dict[str, str] = {}
        for t in self.task_board.active_tasks:
            if t.assigned_worker:
                result[t.assigned_worker] = t.title
        return result

    def _on_workers_changed(self) -> None:
        task_map = self._worker_task_map()
        self.broadcast_ws(
            {
                "type": "workers_changed",
                "workers": [{"name": w.name, "state": w.state.value} for w in self.workers],
                "worker_tasks": task_map,
            }
        )
        self._expire_stale_proposals()
        self.emit("workers_changed")

    def _on_task_assigned(self, worker: Worker, task: SwarmTask) -> None:
        self.notification_bus.emit_task_assigned(worker.name, task.title)
        self.broadcast_ws(
            {
                "type": "task_assigned",
                "worker": worker.name,
                "task": {"id": task.id, "title": task.title},
            }
        )
        self.emit("task_assigned", worker, task)

    def _on_state_changed(self, worker: Worker) -> None:
        """Called when any worker changes state — push to WS clients."""
        # When a worker resumes working, expire stale escalation AND completion
        # proposals — the worker is no longer idle so the proposals are outdated.
        if worker.state == WorkerState.BUZZING:
            # Clear in-flight analysis tracking
            self.analyzer.clear_worker_inflight(worker.name)
            pending = self.proposal_store.pending_for_worker(worker.name)
            stale = [p for p in pending if p.proposal_type in ("escalation", "completion")]
            if stale:
                for p in stale:
                    p.status = ProposalStatus.EXPIRED
                self.proposal_store.clear_resolved()
                self._broadcast_proposals()

        # Log STUNG transitions to system log
        if worker.state == WorkerState.STUNG:
            self.drone_log.add(
                SystemAction.WORKER_STUNG,
                worker.name,
                "worker exited",
                category=LogCategory.WORKER,
                is_notification=True,
            )

        self.broadcast_ws(
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

    def _on_drone_entry(self, entry: SystemEntry) -> None:
        # Emit legacy "drones" type for backward compat
        self.broadcast_ws(
            {
                "type": "drones",
                "action": entry.action.value,
                "worker": entry.worker_name,
                "detail": entry.detail,
            }
        )
        # Emit new "system_log" type with category/notification info
        self.broadcast_ws(
            {
                "type": "system_log",
                "action": entry.action.value,
                "worker": entry.worker_name,
                "detail": entry.detail,
                "category": entry.category.value,
                "is_notification": entry.is_notification,
            }
        )

    def queue_proposal(self, proposal: AssignmentProposal) -> None:
        """Accept a new Queen proposal for user review."""
        self.proposals.on_proposal(proposal)

    def _expire_stale_proposals(self) -> None:
        self.proposals.expire_stale()

    def proposal_dict(self, proposal: AssignmentProposal) -> dict[str, Any]:
        """Serialize a proposal to a dict for API/WebSocket responses."""
        return self.proposals.proposal_dict(proposal)

    def _broadcast_proposals(self) -> None:
        self.proposals.broadcast()

    def _hot_apply_config(self) -> None:
        """Apply config changes to pilot, queen, and notification bus."""
        if self.pilot:
            self.pilot.drone_config = self.config.drones
            self.pilot.enabled = self.config.drones.enabled
            self.pilot._base_interval = self.config.drones.poll_interval
            self.pilot._max_interval = self.config.drones.max_idle_interval
            self.pilot.interval = self.config.drones.poll_interval
            self.pilot.worker_descriptions = self._worker_descriptions()

        self.queen.enabled = self.config.queen.enabled
        self.queen.cooldown = self.config.queen.cooldown
        self.queen.system_prompt = self.config.queen.system_prompt
        self.queen.min_confidence = self.config.queen.min_confidence
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

        self.broadcast_ws({"type": "config_changed"})
        self.drone_log.add(
            SystemAction.CONFIG_CHANGED,
            "system",
            "config reloaded",
            category=LogCategory.SYSTEM,
        )
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
                            self.broadcast_ws({"type": "config_file_changed"})
                            _log.info("config file changed on disk")
                except OSError:
                    _log.debug("mtime check failed", exc_info=True)
        except asyncio.CancelledError:
            return

    def broadcast_ws(self, data: dict[str, Any]) -> None:
        """Send a message to all connected WebSocket clients."""
        if not self.ws_clients:
            return
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return  # No running event loop (CLI/test context)
        payload = json.dumps(data)
        dead: list[web.WebSocketResponse] = []
        for ws in list(self.ws_clients):
            if ws.closed:
                dead.append(ws)
                continue
            asyncio.ensure_future(self._safe_ws_send(ws, payload, dead))
        for ws in dead:
            self.ws_clients.discard(ws)

    @staticmethod
    async def _safe_ws_send(
        ws: web.WebSocketResponse, payload: str, dead: list[web.WebSocketResponse]
    ) -> None:
        """Send a WS message, catching exceptions and discarding dead clients."""
        try:
            await ws.send_str(payload)
        except Exception:  # broad catch: WS errors are unpredictable
            _log.debug("WebSocket send failed, marking client as dead")
            dead.append(ws)

    async def stop(self) -> None:
        if self.pilot:
            self.pilot.stop()
        if self._mtime_task:
            self._mtime_task.cancel()
        # Close all WebSocket connections so runner.cleanup() doesn't hang
        for ws in list(self.ws_clients):
            try:
                await ws.close()
            except Exception:  # broad catch: cleanup must not raise
                pass
        self.ws_clients.clear()
        # Close terminal WebSocket connections too
        for ws in list(self.terminal_ws_clients):
            try:
                await ws.close()
            except Exception:  # broad catch: cleanup must not raise
                pass
        self.terminal_ws_clients.clear()
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

    def _require_task(
        self, task_id: str, allowed_statuses: set[TaskStatus] | None = None
    ) -> SwarmTask:
        """Delegate to TaskManager."""
        return self.tasks.require_task(task_id, allowed_statuses)

    # --- Per-worker tmux operations ---

    async def send_to_worker(self, name: str, message: str, *, _log_operator: bool = True) -> None:
        """Send text to a worker's tmux pane."""
        worker = self._require_worker(name)
        await send_keys(worker.pane_id, message)
        if _log_operator:
            self.drone_log.add(DroneAction.OPERATOR, name, "sent message")

    async def _prep_worker_for_task(self, pane_id: str) -> None:
        """Send /get-latest and /clear before a new task assignment.

        Ensures the worker has the latest code and a fresh context window.
        Waits for the worker to be idle BEFORE sending any commands — never
        injects text into a BUZZING (actively working) pane.
        """
        from swarm.worker.state import classify_pane_content

        async def _wait_for_idle(timeout_polls: int = 120) -> bool:
            """Poll until worker returns to RESTING (max ~60s).

            Only RESTING counts as truly idle.  WAITING means the worker
            is showing an approval prompt mid-command (e.g. git tool
            approvals during /get-latest) — injecting text at that point
            would corrupt the prompt instead of running as a command.

            Returns True if idle was reached, False on timeout.
            """
            for _ in range(timeout_polls):
                await asyncio.sleep(0.5)
                cmd = await get_pane_command(pane_id)
                content = await capture_pane(pane_id)
                state = classify_pane_content(cmd, content)
                if state == WorkerState.RESTING:
                    return True
            return False

        # Wait for the worker to be idle BEFORE sending anything
        if not await _wait_for_idle():
            _log.warning("prep: worker pane %s never became idle — skipping prep", pane_id)
            return

        # Pull latest code
        await send_keys(pane_id, "/get-latest")
        if not await _wait_for_idle():
            _log.warning("prep: /get-latest timed out for pane %s", pane_id)
            return

        # Clear context window
        await send_keys(pane_id, "/clear")
        if not await _wait_for_idle():
            _log.warning("prep: /clear timed out for pane %s", pane_id)
            return

    async def continue_worker(self, name: str) -> None:
        """Send Enter to a worker's tmux pane."""
        worker = self._require_worker(name)
        await send_enter(worker.pane_id)
        self.drone_log.add(DroneAction.OPERATOR, name, "continued (manual)")

    async def interrupt_worker(self, name: str) -> None:
        """Send Ctrl-C to a worker's tmux pane."""
        worker = self._require_worker(name)
        await send_interrupt(worker.pane_id)
        self.drone_log.add(DroneAction.OPERATOR, name, "interrupted (Ctrl-C)")

    async def escape_worker(self, name: str) -> None:
        """Send Escape to a worker's tmux pane."""
        worker = self._require_worker(name)
        await send_escape(worker.pane_id)
        self.drone_log.add(DroneAction.OPERATOR, name, "sent Escape")

    async def capture_worker_output(self, name: str, lines: int = 80) -> str:
        """Capture a worker's tmux pane content."""
        worker = self._require_worker(name)
        return await capture_pane(worker.pane_id, lines=lines)

    async def safe_capture_output(self, name: str, lines: int = 80) -> str:
        """Capture pane content, returning a fallback string on failure."""
        try:
            return await self.capture_worker_output(name, lines=lines)
        except (OSError, asyncio.TimeoutError, WorkerNotFoundError, TmuxError, PaneGoneError):
            return "(pane unavailable)"

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
        if self.workers:
            # Session already running — add workers to existing session
            from swarm.worker.manager import add_worker_live

            launched = []
            for wc in worker_configs:
                worker = await add_worker_live(
                    self.config.session_name,
                    wc,
                    [],  # don't let add_worker_live append — we manage the list
                    self.config.panes_per_window,
                    auto_start=True,
                )
                launched.append(worker)
            async with self._worker_lock:
                self.workers.extend(launched)
        else:
            # Fresh session — create from scratch
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
            self.init_pilot(enabled=self.config.drones.enabled)
        self.broadcast_ws({"type": "workers_changed"})
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
        self.broadcast_ws({"type": "workers_changed"})
        return worker

    async def kill_worker(self, name: str) -> None:
        """Kill a worker: mark STUNG, unassign tasks, broadcast."""
        from swarm.worker.manager import kill_worker as _kill_worker

        worker = self._require_worker(name)

        async with self._worker_lock:
            await _kill_worker(worker)
            worker.state = WorkerState.STUNG
        self.task_board.unassign_worker(worker.name)
        self.drone_log.add(DroneAction.OPERATOR, name, "killed")
        self.broadcast_ws(
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
        self.drone_log.add(DroneAction.OPERATOR, name, "revived (manual)")
        self.broadcast_ws({"type": "workers_changed"})

    async def kill_session(self) -> None:
        """Kill the entire tmux session: stop pilot, unassign all, kill tmux, clear state."""
        from swarm.tmux.hive import kill_session as _kill_session

        if self.pilot:
            self.pilot.stop()

        for w in list(self.workers):
            self.task_board.unassign_worker(w.name)

        try:
            await _kill_session(self.config.session_name)
        except OSError:
            _log.warning("kill_session failed (session may already be gone)", exc_info=True)

        async with self._worker_lock:
            self.workers.clear()
        self.drone_log.clear()
        self.broadcast_ws({"type": "workers_changed"})

    def create_task(
        self,
        title: str,
        description: str = "",
        priority: TaskPriority = TaskPriority.NORMAL,
        task_type: TaskType = TaskType.CHORE,
        tags: list[str] | None = None,
        depends_on: list[str] | None = None,
        attachments: list[str] | None = None,
        source_email_id: str = "",
        actor: str = "user",
    ) -> SwarmTask:
        """Delegate to TaskManager."""
        return self.tasks.create_task(
            title=title,
            description=description,
            priority=priority,
            task_type=task_type,
            tags=tags,
            depends_on=depends_on,
            attachments=attachments,
            source_email_id=source_email_id,
            actor=actor,
        )

    async def assign_task(
        self,
        task_id: str,
        worker_name: str,
        actor: str = "user",
        message: str | None = None,
    ) -> bool:
        """Assign a task to a worker and send task info to its tmux pane.

        If *message* is provided (e.g. from a Queen proposal), it is sent
        instead of the auto-generated task message.
        """
        self._require_worker(worker_name)

        task = self.task_board.get(task_id)
        if not task:
            raise TaskOperationError(f"Task '{task_id}' not found")
        if not task.is_available:
            raise TaskOperationError(f"Task '{task_id}' is not available ({task.status.value})")

        result = self.task_board.assign(task_id, worker_name)
        if result:
            self.task_history.append(task_id, TaskAction.ASSIGNED, actor=actor, detail=worker_name)
            self.drone_log.add(
                SystemAction.TASK_ASSIGNED,
                worker_name,
                task.title,
                category=LogCategory.TASK,
            )
            if actor == "user":
                self.drone_log.add(
                    DroneAction.OPERATOR, worker_name, f"task assigned: {task.title}"
                )
            # Always use the standard task message (includes skill command).
            # If the Queen provided a custom message, append it as context.
            from swarm.server.messages import build_task_message

            msg = build_task_message(task)
            if message:
                msg = f"{msg}\n\nQueen context: {message}"
            try:
                # Prep the worker: pull latest code and clear context window
                pane_id = self._require_worker(worker_name).pane_id
                await self._prep_worker_for_task(pane_id)
                await self.send_to_worker(worker_name, msg, _log_operator=False)
                # Long messages trigger Claude Code's paste-confirmation prompt
                # ("[Pasted text … +N lines]"). Send a second Enter after a short
                # delay to accept the paste and submit the message.
                if "\n" in msg or len(msg) > 200:
                    await asyncio.sleep(0.3)
                    await send_enter(pane_id)
            except (OSError, asyncio.TimeoutError, TmuxError, PaneGoneError):
                _log.warning("failed to send task message to %s", worker_name, exc_info=True)
                # Undo assignment — worker was /clear'd but never got the task
                self.task_board.unassign(task_id)
                self.task_history.append(
                    task_id,
                    TaskAction.UNASSIGNED,
                    actor="system",
                    detail=f"send failed to {worker_name} — returned to pending",
                )
                self.broadcast_ws(
                    {
                        "type": "task_send_failed",
                        "worker": worker_name,
                        "task_title": task.title,
                    }
                )
                self.drone_log.add(
                    DroneAction.OPERATOR,
                    worker_name,
                    f"task send FAILED: {task.title} — returned to pending",
                )
                self.drone_log.add(
                    SystemAction.TASK_SEND_FAILED,
                    worker_name,
                    task.title,
                    category=LogCategory.TASK,
                    is_notification=True,
                )
        return result

    def complete_task(
        self, task_id: str, actor: str = "user", resolution: str = "", send_reply: bool = False
    ) -> bool:
        """Complete a task. Raises if not found or wrong state.

        When *send_reply* is True and the task originated from an email,
        draft and send a reply via the Graph API.
        """
        task = self._require_task(task_id, {TaskStatus.ASSIGNED, TaskStatus.IN_PROGRESS})

        # Capture email info before completing (status changes on complete)
        source_email_id = task.source_email_id
        task_title = task.title
        task_type = task.task_type.value

        result = self.task_board.complete(task_id, resolution=resolution)
        if result:
            self.task_history.append(task_id, TaskAction.COMPLETED, actor=actor, detail=resolution)
            self.drone_log.add(
                SystemAction.TASK_COMPLETED,
                task.assigned_worker or actor,
                task_title,
                category=LogCategory.TASK,
            )
            # Reply to source email only when explicitly requested (opt-in)
            if send_reply and source_email_id and self.graph_mgr and resolution:
                try:
                    asyncio.get_running_loop()
                    asyncio.ensure_future(
                        self._send_completion_reply(
                            source_email_id, task_title, task_type, resolution, task_id
                        )
                    )
                except RuntimeError:
                    pass  # No running event loop (test/CLI context)
        return result

    async def _send_completion_reply(
        self,
        message_id: str,
        task_title: str,
        task_type: str,
        resolution: str,
        task_id: str = "",
    ) -> None:
        """Delegate to EmailService."""
        await self.email.send_completion_reply(
            message_id, task_title, task_type, resolution, task_id
        )

    async def retry_draft_reply(self, task_id: str) -> None:
        """Retry drafting an email reply for an already-completed task."""
        task = self._require_task(task_id)
        if not task.source_email_id:
            raise TaskOperationError("Task has no source email")
        if not task.resolution:
            raise TaskOperationError("Task has no resolution text")
        if not self.graph_mgr:
            raise TaskOperationError("Microsoft Graph not configured")

        await self.email.send_completion_reply(
            task.source_email_id, task.title, task.task_type.value, task.resolution, task_id
        )

    def unassign_task(self, task_id: str, actor: str = "user") -> bool:
        """Delegate to TaskManager."""
        return self.tasks.unassign_task(task_id, actor)

    def reopen_task(self, task_id: str, actor: str = "user") -> bool:
        """Delegate to TaskManager."""
        return self.tasks.reopen_task(task_id, actor)

    def fail_task(self, task_id: str, actor: str = "user") -> bool:
        """Delegate to TaskManager."""
        return self.tasks.fail_task(task_id, actor)

    def remove_task(self, task_id: str, actor: str = "user") -> bool:
        """Delegate to TaskManager."""
        return self.tasks.remove_task(task_id, actor)

    def edit_task(
        self,
        task_id: str,
        title: str | None = None,
        description: str | None = None,
        priority: TaskPriority | None = None,
        task_type: TaskType | None = None,
        tags: list[str] | None = None,
        attachments: list[str] | None = None,
        depends_on: list[str] | None = None,
        actor: str = "user",
    ) -> bool:
        """Delegate to TaskManager."""
        return self.tasks.edit_task(
            task_id,
            title=title,
            description=description,
            priority=priority,
            task_type=task_type,
            tags=tags,
            attachments=attachments,
            depends_on=depends_on,
            actor=actor,
        )

    async def approve_proposal(self, proposal_id: str, draft_response: bool = False) -> bool:
        """Approve a Queen proposal — delegates to ProposalManager."""
        return await self.proposals.approve(proposal_id, draft_response=draft_response)

    def reject_proposal(self, proposal_id: str) -> bool:
        """Reject a Queen proposal — delegates to ProposalManager."""
        return self.proposals.reject(proposal_id)

    def reject_all_proposals(self) -> int:
        """Reject all pending proposals — delegates to ProposalManager."""
        return self.proposals.reject_all()

    def save_attachment(self, filename: str, data: bytes) -> str:
        """Delegate to EmailService."""
        return self.email.save_attachment(filename, data)

    async def create_task_smart(
        self,
        title: str = "",
        description: str = "",
        priority: TaskPriority = TaskPriority.NORMAL,
        task_type: TaskType | None = None,
        tags: list[str] | None = None,
        depends_on: list[str] | None = None,
        attachments: list[str] | None = None,
        source_email_id: str = "",
        actor: str = "user",
    ) -> SwarmTask:
        """Delegate to TaskManager."""
        return await self.tasks.create_task_smart(
            title=title,
            description=description,
            priority=priority,
            task_type=task_type,
            tags=tags,
            depends_on=depends_on,
            attachments=attachments,
            source_email_id=source_email_id,
            actor=actor,
        )

    async def fetch_and_save_image(self, url: str) -> str:
        """Delegate to EmailService."""
        return await self.email.fetch_and_save_image(url)

    async def process_email_data(
        self,
        subject: str,
        body_content: str,
        body_type: str,
        attachment_dicts: list[dict[str, Any]],
        effective_id: str,
        *,
        graph_token: str = "",
    ) -> dict[str, Any]:
        """Delegate to EmailService."""
        return await self.email.process_email_data(
            subject,
            body_content,
            body_type,
            attachment_dicts,
            effective_id,
            graph_token=graph_token,
        )

    def toggle_drones(self) -> bool:
        """Toggle drone pilot and persist to config. Returns new enabled state."""
        if not self.pilot:
            return False
        new_state = self.pilot.toggle()
        self.config.drones.enabled = new_state
        self.save_config()
        self.broadcast_ws({"type": "drones_toggled", "enabled": new_state})
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
        except (OSError, ValueError, KeyError):
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
            self.drone_log.add(DroneAction.OPERATOR, log_actor, log_detail.format(count=count))
        return count

    async def continue_all(self) -> int:
        """Send Enter to all RESTING/WAITING workers. Returns count of workers continued."""
        targets = [w for w in self.workers if w.state in (WorkerState.RESTING, WorkerState.WAITING)]
        return await self._send_to_workers(
            targets, send_enter, "all", "continued {count} worker(s)"
        )

    async def send_all(self, message: str) -> int:
        """Send a message to all workers. Returns count sent."""
        preview = message[:80] + ("…" if len(message) > 80 else "")
        return await self._send_to_workers(
            list(self.workers),
            lambda pane_id: send_keys(pane_id, message),
            "all",
            f'broadcast to {{count}} worker(s): "{preview}"',
        )

    async def send_group(self, group_name: str, message: str) -> int:
        """Send a message to all workers in a group. Returns count sent."""
        group_workers = self.config.get_group(group_name)
        group_names = {w.name.lower() for w in group_workers}
        targets = [w for w in self.workers if w.name.lower() in group_names]
        preview = message[:80] + ("…" if len(message) > 80 else "")
        return await self._send_to_workers(
            targets,
            lambda pane_id: send_keys(pane_id, message),
            group_name,
            f'group send to {{count}} worker(s): "{preview}"',
        )

    async def gather_hive_context(self) -> str:
        """Delegate to QueenAnalyzer."""
        return await self.analyzer.gather_context()

    async def analyze_worker(self, worker_name: str, *, force: bool = False) -> dict[str, Any]:
        """Delegate to QueenAnalyzer."""
        return await self.analyzer.analyze_worker(worker_name, force=force)

    async def coordinate_hive(self, *, force: bool = False) -> dict[str, Any]:
        """Delegate to QueenAnalyzer."""
        return await self.analyzer.coordinate(force=force)

    @staticmethod
    def _parse_approval_rules(rules_raw: Any) -> list[Any]:
        """Parse and validate approval rules from a config update."""
        import re as _re

        from swarm.config import DroneApprovalRule

        if not isinstance(rules_raw, list):
            raise ValueError("drones.approval_rules must be a list")
        parsed = []
        for i, r in enumerate(rules_raw):
            if not isinstance(r, dict):
                raise ValueError(f"drones.approval_rules[{i}] must be an object")
            pattern = r.get("pattern", "")
            action = r.get("action", "approve")
            if action not in ("approve", "escalate"):
                raise ValueError(
                    f"drones.approval_rules[{i}].action must be 'approve' or 'escalate'"
                )
            try:
                _re.compile(pattern)
            except _re.error as exc:
                raise ValueError(
                    f"drones.approval_rules[{i}].pattern: invalid regex: {exc}"
                ) from exc
            parsed.append(DroneApprovalRule(pattern=pattern, action=action))
        return parsed

    def _apply_drones_config(self, bz: dict[str, Any]) -> None:
        """Validate and apply drones section of a config update."""
        cfg = self.config.drones
        for key in (
            "enabled",
            "escalation_threshold",
            "poll_interval",
            "auto_approve_yn",
            "max_revive_attempts",
            "max_poll_failures",
            "max_idle_interval",
            "auto_stop_on_complete",
        ):
            if key in bz:
                val = bz[key]
                if key in ("enabled", "auto_approve_yn", "auto_stop_on_complete"):
                    if not isinstance(val, bool):
                        raise ValueError(f"drones.{key} must be boolean")
                else:
                    if not isinstance(val, (int, float)):
                        raise ValueError(f"drones.{key} must be a number")
                    if val < 0:
                        raise ValueError(f"drones.{key} must be >= 0")
                setattr(cfg, key, val)
        if "approval_rules" in bz:
            self.config.drones.approval_rules = self._parse_approval_rules(bz["approval_rules"])

    def _apply_queen_config(self, qn: dict[str, Any]) -> None:
        """Validate and apply queen section of a config update."""
        cfg = self.config.queen
        if "cooldown" in qn:
            if not isinstance(qn["cooldown"], (int, float)) or qn["cooldown"] < 0:
                raise ValueError("queen.cooldown must be a non-negative number")
            cfg.cooldown = qn["cooldown"]
        if "enabled" in qn:
            if not isinstance(qn["enabled"], bool):
                raise ValueError("queen.enabled must be boolean")
            cfg.enabled = qn["enabled"]
        if "system_prompt" in qn:
            if not isinstance(qn["system_prompt"], str):
                raise ValueError("queen.system_prompt must be a string")
            cfg.system_prompt = qn["system_prompt"]
        if "min_confidence" in qn:
            val = qn["min_confidence"]
            if not isinstance(val, (int, float)) or not (0.0 <= val <= 1.0):
                raise ValueError("queen.min_confidence must be a number between 0.0 and 1.0")
            cfg.min_confidence = float(val)

    def _apply_notifications_config(self, nt: dict[str, Any]) -> None:
        """Validate and apply notifications section of a config update."""
        cfg = self.config.notifications
        for key in ("terminal_bell", "desktop"):
            if key in nt:
                if not isinstance(nt[key], bool):
                    raise ValueError(f"notifications.{key} must be boolean")
                setattr(cfg, key, nt[key])
        if "debounce_seconds" in nt:
            if not isinstance(nt["debounce_seconds"], (int, float)) or nt["debounce_seconds"] < 0:
                raise ValueError("notifications.debounce_seconds must be >= 0")
            cfg.debounce_seconds = nt["debounce_seconds"]

    def _apply_workflows_config(self, wf: Any) -> None:
        """Validate and apply workflows section of a config update."""
        if not isinstance(wf, dict):
            raise ValueError("workflows must be an object")
        valid_types = {"bug", "feature", "verify", "chore"}
        cleaned: dict[str, str] = {}
        for k, v in wf.items():
            if k not in valid_types:
                raise ValueError(f"workflows key '{k}' is not a valid task type")
            if not isinstance(v, str):
                raise ValueError(f"workflows.{k} must be a string")
            cleaned[k] = v.strip()
        self.config.workflows = cleaned
        from swarm.tasks.workflows import apply_config_overrides

        apply_config_overrides(cleaned)

    def _apply_default_group(self, dg: Any) -> None:
        """Validate and apply default_group setting."""
        if not isinstance(dg, str):
            raise ValueError("default_group must be a string")
        if dg:
            group_names = {g.name.lower() for g in self.config.groups}
            if dg.lower() not in group_names:
                raise ValueError(f"default_group '{dg}' does not match any defined group")
        self.config.default_group = dg

    def _apply_scalar_config(self, body: dict[str, Any]) -> None:
        """Apply workers, default_group, scalars, and graph settings."""
        if "workers" in body and isinstance(body["workers"], dict):
            for wname, desc in body["workers"].items():
                wc = self.config.get_worker(wname)
                if wc and isinstance(desc, str):
                    wc.description = desc
        if "default_group" in body:
            self._apply_default_group(body["default_group"])
        for key in ("session_name", "projects_dir", "log_level"):
            if key in body:
                setattr(self.config, key, body[key])
        for key, attr in (
            ("graph_client_id", "graph_client_id"),
            ("graph_tenant_id", "graph_tenant_id"),
        ):
            if key in body and isinstance(body[key], str):
                val = body[key].strip() or ("common" if key == "graph_tenant_id" else "")
                setattr(self.config, attr, val)
        if "tool_buttons" in body and isinstance(body["tool_buttons"], list):
            from swarm.config import ToolButtonConfig

            self.config.tool_buttons = [
                ToolButtonConfig(label=b["label"], command=b.get("command", ""))
                for b in body["tool_buttons"]
                if isinstance(b, dict) and b.get("label")
            ]

    async def apply_config_update(self, body: dict[str, Any]) -> None:
        """Apply a partial config update from the API. Raises ValueError on invalid input."""
        if "drones" in body:
            self._apply_drones_config(body["drones"])
        if "queen" in body:
            self._apply_queen_config(body["queen"])
        if "notifications" in body:
            self._apply_notifications_config(body["notifications"])
        self._apply_scalar_config(body)
        if "workflows" in body:
            self._apply_workflows_config(body["workflows"])

        # Rebuild graph manager if client_id changed
        self.graph_mgr = self._build_graph_manager(self.config)
        self.email._graph_mgr = self.graph_mgr

        # Hot-reload and save
        await self.reload_config(self.config)
        self.save_config()

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
