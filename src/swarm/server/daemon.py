"""SwarmDaemon — long-running backend service."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from typing import TYPE_CHECKING, Any

from aiohttp import web

from pathlib import Path

if TYPE_CHECKING:
    from swarm.auth.graph import GraphTokenManager as GraphManager

from swarm.config import HiveConfig, WorkerConfig
from swarm.server.config_manager import ConfigManager
from swarm.server.worker_service import WorkerService
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
from swarm.queen.queue import QueenCallQueue
from swarm.queen.queen import Queen
from swarm.tasks.board import TaskBoard
from swarm.tunnel import TunnelManager, TunnelState
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
from swarm.pty.process import ProcessError
from swarm.worker.worker import Worker, WorkerState

_log = get_logger("server.daemon")


def _log_task_exception(task: asyncio.Task[object]) -> None:
    """Log unhandled exceptions from fire-and-forget tasks."""
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        _log.error("fire-and-forget task failed: %s", exc, exc_info=exc)


# --- Exception classes ---


class SwarmOperationError(Exception):
    """Base for daemon operation errors."""


class WorkerNotFoundError(SwarmOperationError):
    """Referenced worker does not exist."""


class TaskOperationError(SwarmOperationError):
    """Task op failed (not found, wrong state)."""


class SwarmDaemon(EventEmitter):
    """Long-running backend service for the swarm."""

    def __init__(self, config: HiveConfig, *, task_store: FileTaskStore | None = None) -> None:
        self.__init_emitter__()
        self.config = config
        self.workers: list[Worker] = []
        self.pool = None  # ProcessPool — set externally for PTY-based workers
        self._worker_lock = asyncio.Lock()
        # Persistence: tasks and system log survive restarts
        task_store = task_store or FileTaskStore()
        system_log_path = Path.home() / ".swarm" / "system.jsonl"
        self.drone_log = DroneLog(log_file=system_log_path)
        self.task_board = TaskBoard(store=task_store)
        self.task_history = TaskHistory()
        self.queen = Queen(config=config.queen, session_name=config.session_name)
        self.queen_queue = QueenCallQueue(
            max_concurrent=2,
            on_status_change=self._on_queen_queue_status_change,
            get_worker_state=self._get_worker_state,
        )
        self.proposal_store = ProposalStore()
        self.proposals = ProposalManager(self.proposal_store, self)
        self.analyzer = QueenAnalyzer(self.queen, self, self.queen_queue)
        self.notification_bus = self._build_notification_bus(config)
        # Apply workflow skill overrides from config
        if config.workflows:
            from swarm.tasks.workflows import apply_config_overrides

            apply_config_overrides(config.workflows)
        self.pilot: DronePilot | None = None
        self.ws_clients: set[web.WebSocketResponse] = set()
        self.terminal_ws_clients: set[web.WebSocketResponse] = set()
        self._heartbeat_task: asyncio.Task | None = None
        self._usage_task: asyncio.Task | None = None
        self._heartbeat_snapshot: dict[str, str] = {}
        # In-flight Queen analysis tracking lives on self.analyzer
        self.start_time = time.time()
        self._config_mtime: float = 0.0
        self._mtime_task: asyncio.Task | None = None
        self._bg_tasks: set[asyncio.Task[object]] = set()
        # Debounced state broadcasts: coalesce multiple state_changed events
        # within a single poll tick into one WebSocket broadcast.
        self._state_dirty: bool = False
        self._state_debounce_handle: asyncio.TimerHandle | None = None
        self._state_debounce_delay: float = 0.3  # 300ms
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
        self.config_mgr = ConfigManager(self)
        self.worker_svc = WorkerService(self)
        self.tunnel = TunnelManager(
            port=config.port,
            on_state_change=self._on_tunnel_state_change,
        )
        # Update detection
        self._update_result: object | None = None  # UpdateResult when checked
        self._update_task: asyncio.Task | None = None
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

    def _get_worker_state(self, name: str) -> str | None:
        """Return a worker's current state value, or None if not found."""
        w = self.get_worker(name)
        return w.state.value if w else None

    def _on_queen_queue_status_change(self, status: dict) -> None:
        """Broadcast queen queue status changes to WS clients."""
        self.broadcast_ws({"type": "queen_queue", **status})

    def _worker_descriptions(self) -> dict[str, str]:
        """Build a name→description map from config workers."""
        return {w.name: w.description for w in self.config.workers if w.description}

    def init_pilot(self, *, enabled: bool = True) -> DronePilot:
        """Create, wire, and start the drone pilot. Returns the pilot instance."""
        self.pilot = DronePilot(
            self.workers,
            self.drone_log,
            self.config.watch_interval,
            pool=self.pool,
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
        self.pilot.set_pending_proposals_check(lambda: bool(self.proposal_store.pending))
        self.pilot.set_pending_proposals_for_worker(
            lambda name: bool(self.proposal_store.pending_for_worker(name))
        )
        self.drone_log.on_entry(self._on_drone_entry)

        self.tasks._pilot = self.pilot
        self.pilot.start()
        self.pilot.enabled = enabled
        _log.info("pilot initialized (enabled=%s)", enabled)
        return self.pilot

    def _init_test_mode(self) -> None:
        """Initialize test mode: TestRunLog, TestOperator, wire events."""
        import uuid

        from swarm.testing.config import TestConfig
        from swarm.testing.log import TestRunLog
        from swarm.testing.operator import TestOperator

        test_cfg = self.config.test if self.config.test.enabled else TestConfig(enabled=True)
        report_dir = Path(test_cfg.report_dir).expanduser()
        run_id = uuid.uuid4().hex[:8]

        self._test_log = TestRunLog(run_id, report_dir)
        self._test_operator = TestOperator(self, self._test_log, test_cfg)
        self._test_operator.start()

        # Override pilot idle threshold for faster test-mode completion detection
        if self.pilot:
            self.pilot._auto_complete_min_idle = test_cfg.auto_complete_min_idle

        # Wire pilot's drone_decision event to test log
        if self.pilot:
            self.pilot._emit_decisions = True
            self.pilot.on(
                "drone_decision",
                lambda w, content, d: self._test_log.record_drone_decision(
                    worker_name=w.name,
                    content=content,
                    decision=d.decision.value,
                    reason=d.reason,
                    rule_pattern=d.rule_pattern,
                    rule_index=d.rule_index,
                    source=d.source,
                ),
            )
            # Track state transitions for the test report
            _prev_states: dict[str, str] = {}

            def _log_state_change(w: Worker) -> None:
                old = _prev_states.get(w.name, "UNKNOWN")
                new = w.state.value
                if old != new:
                    self._test_log.record_state_change(w.name, old, new)
                    _prev_states[w.name] = new

            self.pilot.on_state_changed(_log_state_change)

        # Wire Queen analysis events to test log
        self.on(
            "queen_analysis",
            lambda wn, action, reasoning, conf: self._test_log.record_queen_analysis(
                worker_name=wn,
                action=action,
                reasoning=reasoning,
                confidence=conf,
            ),
        )

        # Wire hive_complete to report generation
        if self.pilot:
            self.pilot.on_hive_complete(self._on_test_complete)

        # Load test tasks into the task board
        if self.config.test.enabled:
            self._load_test_tasks()

        # Broadcast test mode status to dashboard
        self.broadcast_ws({"type": "test_mode", "enabled": True, "run_id": run_id})

        _log.info("test mode initialized (run_id=%s, log=%s)", run_id, self._test_log.log_path)

    def _load_test_tasks(self) -> None:
        """Load tasks from the test project's tasks.yaml into the task board."""
        # The fixture dir tasks.yaml is at the standard location
        fixture_tasks_file = (
            Path(__file__).resolve().parent.parent.parent.parent
            / "tests"
            / "fixtures"
            / "test-project"
            / "tasks.yaml"
        )
        if not fixture_tasks_file.exists():
            _log.warning("test tasks.yaml not found at %s", fixture_tasks_file)
            return

        import yaml

        data = yaml.safe_load(fixture_tasks_file.read_text()) or {}
        tasks = data.get("tasks", [])

        from swarm.tasks.task import TaskPriority, TaskType

        priority_map = {
            "low": TaskPriority.LOW,
            "normal": TaskPriority.NORMAL,
            "high": TaskPriority.HIGH,
            "urgent": TaskPriority.URGENT,
        }
        type_map = {
            "bug": TaskType.BUG,
            "feature": TaskType.FEATURE,
            "verify": TaskType.VERIFY,
            "chore": TaskType.CHORE,
        }

        # Remove stale test tasks from previous runs to prevent duplicates.
        # Tasks persist in ~/.swarm/tasks.json across runs, so without cleanup
        # each test run would add another copy of every fixture task.
        fixture_titles = {t["title"] for t in tasks if isinstance(t, dict) and t.get("title")}
        if fixture_titles:
            stale_ids = {
                task.id for task in self.task_board.all_tasks if task.title in fixture_titles
            }
            if stale_ids:
                removed = self.task_board.remove_tasks(stale_ids)
                _log.info("removed %d stale test tasks from previous runs", removed)

        for t in tasks:
            if not isinstance(t, dict) or not t.get("title"):
                continue
            self.create_task(
                title=t["title"],
                description=t.get("description", ""),
                priority=priority_map.get(t.get("priority", "normal"), TaskPriority.NORMAL),
                task_type=type_map.get(t.get("task_type", "chore"), TaskType.CHORE),
                tags=t.get("tags", []),
                actor="test-mode",
            )
        _log.info("loaded %d test tasks", len(tasks))

    def _on_test_complete(self) -> None:
        """Called when hive completes in test mode — trigger report generation."""
        if not hasattr(self, "_test_log"):
            return

        async def _generate() -> None:
            from swarm.testing.report import ReportGenerator

            gen = ReportGenerator(self._test_log, self._test_log.report_dir)
            try:
                report_path = await gen.generate()
                _log.info("test report written to %s", report_path)
                self.broadcast_ws({"type": "test_report_ready", "path": str(report_path)})
            except Exception:
                _log.error("test report generation failed", exc_info=True)

        task = asyncio.create_task(_generate())
        task.add_done_callback(_log_task_exception)
        self._track_task(task)

    async def start(self) -> None:
        """Discover workers and start the pilot loop."""
        await self.discover()

        if not self.workers:
            _log.warning("no workers found")
            return

        _log.info("found %d workers", len(self.workers))
        self.init_pilot(enabled=self.config.drones.enabled)
        _log.info(
            "daemon started — drone pilot %s",
            "active" if self.config.drones.enabled else "disabled",
        )

        # Start heartbeat loop for display_state dirty-checking
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

        # Start periodic usage refresh (every 60s)
        self._usage_task = asyncio.create_task(self._usage_refresh_loop())

        # Start background update check (5s delay for WS clients to connect)
        self._update_task = asyncio.create_task(self._check_for_updates())

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
            self.analyzer.start_escalation(worker, reason)

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
            self.analyzer.start_completion(worker, task)
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

    def _on_task_assigned(self, worker: Worker, task: SwarmTask, message: str = "") -> None:
        # When the pilot auto-approved, actually assign & send the message
        if task.is_available:
            try:
                asyncio.get_running_loop()
                task_ = asyncio.ensure_future(self._deliver_auto_assignment(worker, task, message))
                task_.add_done_callback(_log_task_exception)
                self._track_task(task_)
            except RuntimeError:
                pass  # No event loop (sync test context)
        self.notification_bus.emit_task_assigned(worker.name, task.title)
        self.broadcast_ws(
            {
                "type": "task_assigned",
                "worker": worker.name,
                "task": {"id": task.id, "title": task.title},
            }
        )
        self.emit("task_assigned", worker, task)

    async def _deliver_auto_assignment(self, worker: Worker, task: SwarmTask, message: str) -> None:
        """Deliver an auto-approved task assignment via the standard assign_task path."""
        try:
            await self.assign_task(task.id, worker.name, actor="queen", message=message)
        except Exception:
            _log.warning(
                "auto-assign delivery failed: %s → %s",
                worker.name,
                task.title,
                exc_info=True,
            )

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

        self._mark_state_dirty()

    def _mark_state_dirty(self) -> None:
        """Schedule a debounced state broadcast.

        Multiple state changes within ``_state_debounce_delay`` seconds are
        coalesced into a single WebSocket broadcast.
        """
        self._state_dirty = True
        if self._state_debounce_handle is not None:
            self._state_debounce_handle.cancel()
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # No event loop (test/CLI context) — flush immediately
            self._flush_state_broadcast()
            return
        self._state_debounce_handle = loop.call_later(
            self._state_debounce_delay, self._flush_state_broadcast
        )

    def _flush_state_broadcast(self) -> None:
        """Send the coalesced state broadcast if dirty."""
        if not self._state_dirty:
            return
        self._state_dirty = False
        self._state_debounce_handle = None
        self._broadcast_state()

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

    async def _usage_refresh_loop(self) -> None:
        """Periodically read worker JSONL sessions to update token usage."""
        from swarm.worker.usage import get_worker_usage

        try:
            while True:
                await asyncio.sleep(60)
                for worker in self.workers:
                    try:
                        worker.usage = get_worker_usage(worker.path, self.start_time)
                    except Exception:
                        _log.debug("usage refresh failed for %s", worker.name, exc_info=True)
        except asyncio.CancelledError:
            return

    async def _heartbeat_loop(self) -> None:
        """Periodically check if worker display_state changed and broadcast.

        Catches time-based transitions (e.g. RESTING→SLEEPING) that happen
        between poll cycles, ensuring WS clients stay in sync.

        Also acts as a watchdog: if the pilot's poll loop has died, restart it.
        First check runs after 2s (fast startup), then every 8s.
        """
        try:
            first = True
            while True:
                await asyncio.sleep(2 if first else 8)
                first = False

                # Watchdog: revive pilot loop if it died unexpectedly
                if self.pilot and self.pilot.needs_restart():
                    _log.warning("heartbeat: pilot loop was dead — restarting")
                    self.pilot.restart_loop()

                snapshot = {w.name: w.display_state.value for w in self.workers}
                if snapshot != self._heartbeat_snapshot:
                    self._heartbeat_snapshot = snapshot
                    self._broadcast_state()
        except asyncio.CancelledError:
            return

    async def _check_for_updates(self) -> None:
        """Background update check — runs once after a 5s startup delay."""
        try:
            await asyncio.sleep(5)
            from swarm.update import check_for_update, update_result_to_dict

            result = await check_for_update()
            self._update_result = result
            if result.available:
                self.broadcast_ws({"type": "update_available", **update_result_to_dict(result)})
        except asyncio.CancelledError:
            return
        except Exception:
            _log.debug("background update check failed", exc_info=True)

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

    def apply_config(self) -> None:
        """Apply current config to pilot, queen, and notification bus.

        Encapsulates internal attribute updates so external callers
        (e.g. ConfigManager) don't need to reach into daemon internals.
        """
        if self.pilot:
            self.pilot.drone_config = self.config.drones
            self.pilot.enabled = self.config.drones.enabled
            self.pilot.set_poll_intervals(
                self.config.drones.poll_interval,
                self.config.drones.max_idle_interval,
            )
            self.pilot.interval = self.config.drones.poll_interval
            self.pilot.worker_descriptions = self._worker_descriptions()

        self.queen.enabled = self.config.queen.enabled
        self.queen.cooldown = self.config.queen.cooldown
        self.queen.system_prompt = self.config.queen.system_prompt
        self.queen.min_confidence = self.config.queen.min_confidence
        self.notification_bus = self._build_notification_bus(self.config)

    async def reload_config(self, new_config: HiveConfig) -> None:
        """Hot-reload configuration. Updates pilot, queen, and notifies WS clients."""
        await self.config_mgr.reload(new_config)

    async def _watch_config_mtime(self) -> None:
        """Poll config file mtime every 30s and notify WS clients if changed."""
        await self.config_mgr.watch_mtime()

    def _on_tunnel_state_change(self, state: TunnelState, detail: str) -> None:
        """Broadcast tunnel state changes to all WS clients."""
        if state == TunnelState.RUNNING:
            self.broadcast_ws({"type": "tunnel_started", "url": detail})
        elif state == TunnelState.STOPPED:
            self.broadcast_ws({"type": "tunnel_stopped"})
        elif state == TunnelState.ERROR:
            self.broadcast_ws({"type": "tunnel_error", "error": detail})

    def _track_task(self, task: asyncio.Task[object]) -> None:
        """Register a fire-and-forget task for cancellation at shutdown."""
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)

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
            task = asyncio.ensure_future(self._safe_ws_send(ws, payload, dead))
            task.add_done_callback(_log_task_exception)
            self._track_task(task)
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

    def _broadcast_state(self) -> None:
        """Push current worker states to all WS clients."""
        self.broadcast_ws(
            {
                "type": "state",
                "workers": [
                    {
                        "name": w.name,
                        "state": w.display_state.value,
                        "state_duration": round(w.state_duration, 1),
                    }
                    for w in self.workers
                ],
            }
        )

    def _cancel_timers(self) -> None:
        """Cancel all background timer tasks."""
        if self.pilot:
            self.pilot.stop()
        for t in (
            self._heartbeat_task,
            self._usage_task,
            self._mtime_task,
            getattr(self, "_update_task", None),
        ):
            if t:
                t.cancel()
        if self._state_debounce_handle is not None:
            self._state_debounce_handle.cancel()
            self._state_debounce_handle = None
        for task in list(self._bg_tasks):
            task.cancel()
        self._bg_tasks.clear()

    async def stop(self) -> None:
        # Generate test report if a test run was active and no report was written.
        # Must run before cancelling bg tasks so the subprocess can finish.
        await self._generate_test_report_if_pending()

        self.queen_queue.cancel_all()
        self._cancel_timers()
        # Stop cloudflare tunnel if running
        if self.tunnel.is_running:
            await self.tunnel.stop()
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

    async def _generate_test_report_if_pending(self) -> None:
        """Generate a test report on shutdown if one wasn't produced during the run."""
        if not hasattr(self, "_test_log"):
            return
        try:
            from swarm.testing.report import ReportGenerator

            gen = ReportGenerator(self._test_log, self._test_log.report_dir)
            report_path = await gen.generate_if_pending()
            if report_path:
                _log.info("fallback test report written to %s", report_path)
        except Exception:
            _log.error("fallback test report generation failed", exc_info=True)

    # --- Lookup helper ---

    def get_worker(self, name: str) -> Worker | None:
        """Find a worker by name."""
        return self.worker_svc.get_worker(name)

    def _require_worker(self, name: str) -> Worker:
        """Get a worker by name or raise WorkerNotFoundError."""
        return self.worker_svc.require_worker(name)

    def _require_task(
        self, task_id: str, allowed_statuses: set[TaskStatus] | None = None
    ) -> SwarmTask:
        """Delegate to TaskManager."""
        return self.tasks.require_task(task_id, allowed_statuses)

    # --- Per-worker operations ---

    async def send_to_worker(self, name: str, message: str, *, _log_operator: bool = True) -> None:
        """Send text to a worker's process."""
        await self.worker_svc.send_to_worker(name, message, _log_operator=_log_operator)

    async def _prep_worker_for_task(self, worker_name: str) -> None:
        """Send /get-latest and /clear before a new task assignment."""
        await self.worker_svc.prep_for_task(worker_name)

    async def continue_worker(self, name: str) -> None:
        """Send Enter to a worker's process."""
        await self.worker_svc.continue_worker(name)

    async def interrupt_worker(self, name: str) -> None:
        """Send Ctrl-C to a worker's process."""
        await self.worker_svc.interrupt_worker(name)

    async def escape_worker(self, name: str) -> None:
        """Send Escape to a worker's process."""
        await self.worker_svc.escape_worker(name)

    async def capture_worker_output(self, name: str, lines: int = 80) -> str:
        """Read a worker's process output buffer."""
        return await self.worker_svc.capture_output(name, lines=lines)

    async def safe_capture_output(self, name: str, lines: int = 80) -> str:
        """Read process output, returning a fallback string on failure."""
        return await self.worker_svc.safe_capture_output(name, lines=lines)

    async def discover(self) -> list[Worker]:
        """Discover existing workers. Updates self.workers."""
        return await self.worker_svc.discover()

    async def poll_once(self) -> bool:
        """Run one pilot poll cycle. Returns True if any action was taken."""
        if not self.pilot:
            return False
        return await self.pilot.poll_once()

    # --- Operation methods ---

    async def launch_workers(self, worker_configs: list[WorkerConfig]) -> list[Worker]:
        """Launch workers. Extends self.workers and updates pilot."""
        return await self.worker_svc.launch(worker_configs)

    async def spawn_worker(self, worker_config: WorkerConfig) -> Worker:
        """Spawn a single worker into the running session."""
        return await self.worker_svc.spawn(worker_config)

    async def kill_worker(self, name: str) -> None:
        """Kill a worker: mark STUNG, unassign tasks, broadcast."""
        await self.worker_svc.kill(name)

    async def revive_worker(self, name: str) -> None:
        """Revive a STUNG worker."""
        await self.worker_svc.revive(name)

    async def kill_session(self, *, all_sessions: bool = False) -> None:
        """Kill all workers and clean up."""
        await self.worker_svc.kill_session(all_sessions=all_sessions)

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
        """Assign a task to a worker and send task info to its process.

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
            if self.pilot:
                self.pilot.wake_worker(worker_name)
            self.task_history.append(task_id, TaskAction.ASSIGNED, actor=actor, detail=worker_name)
            # Build task message before logging so we can include metadata
            from swarm.server.messages import build_task_message

            msg = build_task_message(task)
            if message:
                msg = f"{msg}\n\nQueen context: {message}"
            _log.info(
                "task message to %s (%d chars, queen_ctx=%s)",
                worker_name,
                len(msg),
                bool(message),
            )
            _log.debug("task message body: %s", msg[:500])
            self.drone_log.add(
                SystemAction.TASK_ASSIGNED,
                worker_name,
                task.title,
                category=LogCategory.TASK,
                metadata={
                    "task_id": task.id,
                    "msg_length": len(msg),
                    "has_queen_context": bool(message),
                },
            )
            if actor == "user":
                self.drone_log.add(
                    DroneAction.OPERATOR,
                    worker_name,
                    f"task assigned: {task.title}",
                    category=LogCategory.OPERATOR,
                )
            try:
                # Prep the worker: pull latest code and clear context window
                await self._prep_worker_for_task(worker_name)
                await self.send_to_worker(worker_name, msg, _log_operator=False)
                # Long messages trigger Claude Code's paste-confirmation prompt
                # ("[Pasted text … +N lines]"). Send a second Enter after a short
                # delay to accept the paste and submit the message.
                if "\n" in msg or len(msg) > 200:
                    worker = self._require_worker(worker_name)
                    await asyncio.sleep(0.3)
                    await worker.process.send_enter()
            except (ProcessError, OSError, asyncio.TimeoutError):
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
                    category=LogCategory.OPERATOR,
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
            # Signal pilot that a task was completed during this session
            # so hive_complete detection can distinguish fresh completions
            # from stale ones loaded from the persistent store.
            if self.pilot:
                self.pilot._saw_completion = True
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
                    task = asyncio.ensure_future(
                        self._send_completion_reply(
                            source_email_id, task_title, task_type, resolution, task_id
                        )
                    )
                    task.add_done_callback(_log_task_exception)
                    self._track_task(task)
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
        return self.config_mgr.toggle_drones()

    def check_config_file(self) -> bool:
        """Check if config file changed on disk; reload if so. Returns True if reloaded."""
        return self.config_mgr.check_file()

    async def continue_all(self) -> int:
        """Send Enter to all RESTING/WAITING workers. Returns count of workers continued."""
        return await self.worker_svc.continue_all()

    async def send_all(self, message: str) -> int:
        """Send a message to all workers. Returns count sent."""
        return await self.worker_svc.send_all(message)

    async def send_group(self, group_name: str, message: str) -> int:
        """Send a message to all workers in a group. Returns count sent."""
        return await self.worker_svc.send_group(group_name, message)

    async def gather_hive_context(self) -> str:
        """Delegate to QueenAnalyzer."""
        return await self.analyzer.gather_context()

    async def analyze_worker(self, worker_name: str, *, force: bool = False) -> dict[str, Any]:
        """Delegate to QueenAnalyzer."""
        return await self.analyzer.analyze_worker(worker_name, force=force)

    async def coordinate_hive(self, *, force: bool = False) -> dict[str, Any]:
        """Delegate to QueenAnalyzer."""
        return await self.analyzer.coordinate(force=force)

    async def apply_config_update(self, body: dict[str, Any]) -> None:
        """Apply a partial config update from the API. Raises ValueError on invalid input."""
        await self.config_mgr.apply_update(body)

    def save_config(self) -> None:
        """Save config to disk and update mtime to prevent self-triggered reload."""
        self.config_mgr.save()


_DAEMON_LOCK_PATH = Path.home() / ".swarm" / "daemon.lock"


def _acquire_daemon_lock() -> int:
    """Acquire an exclusive lock on the daemon lock file.

    Uses ``fcntl.flock()`` which is automatically released when the
    process exits (even on crash).  Returns the open file descriptor
    so it stays alive for the process lifetime.

    Raises ``SystemExit`` if another daemon already holds the lock.
    """
    import fcntl

    _DAEMON_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(_DAEMON_LOCK_PATH), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        raise SystemExit(
            "Another swarm daemon is already running. "
            "Stop it first or use 'swarm kill --all' to clean up."
        )
    # Write our PID for diagnostics
    os.ftruncate(fd, 0)
    os.lseek(fd, 0, os.SEEK_SET)
    os.write(fd, f"{os.getpid()}\n".encode())
    return fd


async def run_daemon(
    config: HiveConfig, host: str = "localhost", port: int = 9090, *, test_mode: bool = False
) -> None:
    """Start the daemon with HTTP server."""
    import signal

    from swarm.server.api import create_app

    # Singleton lock — prevents two daemons from running simultaneously
    # and causing revive wars via the shared pty-holder.
    # The fd must stay open for the process lifetime; stored on the daemon.
    _daemon_lock_fd = _acquire_daemon_lock()

    # Capture startup command for os.execv restart
    startup_argv = list(sys.argv)

    test_store = None
    if test_mode:
        test_store = FileTaskStore(path=Path.home() / ".swarm" / "test-tasks.json")
    daemon = SwarmDaemon(config, task_store=test_store)
    daemon._lock_fd = _daemon_lock_fd  # prevent GC / keep lock alive

    # Initialize the PTY process pool (starts holder sidecar if needed)
    from swarm.pty.pool import ProcessPool

    pool = ProcessPool()
    await pool.ensure_holder()
    daemon.pool = pool

    await daemon.start()

    # Initialize test mode components if enabled
    if test_mode:
        daemon._init_test_mode()

    app = create_app(daemon)

    # Graceful shutdown via signal — avoids KeyboardInterrupt race with aiohttp
    shutdown = asyncio.Event()
    app["shutdown_event"] = shutdown
    # Mutable container so the handler can set it without triggering
    # aiohttp's "changing state of started app" deprecation warning.
    app["restart_flag"] = {"requested": False}

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
            lambda w, t, m="": console_log(f'Task "{t.title}" assigned -> {w.name}')
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

    # If restart was requested (e.g. after update), replace process with new binary
    if app.get("restart_flag", {}).get("requested"):
        print("Restarting swarm...", flush=True)
        os.execv(startup_argv[0], startup_argv)


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
    # Check cache-only for update info (no network call during startup)
    from swarm.update import check_for_update_sync

    cached = check_for_update_sync()
    if cached and cached.available:
        print(
            f"  {D}\u251c\u2500{R} Tasks:      {task_summary}",
            flush=True,
        )
        print(
            f"  {D}\u2514\u2500{R} Update:     {Y}{cached.remote_version}{R} available"
            f" (current: {cached.current_version})",
            flush=True,
        )
    else:
        print(f"  {D}\u2514\u2500{R} Tasks:      {task_summary}", flush=True)
    print(flush=True)


async def run_test_daemon(
    config: HiveConfig, host: str = "0.0.0.0", port: int | None = None, timeout: int = 300
) -> Path | None:
    """Run the daemon in test mode with auto-shutdown on completion or timeout.

    Returns the report file path, or None if no report was generated.
    Raises TimeoutError if the timeout is reached.
    """
    import signal

    from swarm.server.api import create_app

    port = port or config.test.port

    # Isolate test tasks from the main task board so they don't leak.
    test_store = FileTaskStore(path=Path.home() / ".swarm" / "test-tasks.json")
    daemon = SwarmDaemon(config, task_store=test_store)

    from swarm.pty.pool import ProcessPool

    pool = ProcessPool()
    await pool.ensure_holder()
    daemon.pool = pool

    await daemon.start()
    daemon._init_test_mode()

    app = create_app(daemon)

    shutdown = asyncio.Event()
    app["shutdown_event"] = shutdown
    report_result: dict[str, Path | None] = {"path": None}

    # Intercept broadcast_ws to detect test_report_ready
    _orig_broadcast = daemon.broadcast_ws

    def _intercept_broadcast(data: dict[str, Any]) -> None:
        _orig_broadcast(data)
        if data.get("type") == "test_report_ready":
            report_result["path"] = Path(data["path"])
            shutdown.set()

    daemon.broadcast_ws = _intercept_broadcast  # type: ignore[assignment]

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()

    _print_test_banner(daemon, host, port, timeout)
    _wire_test_console(daemon)

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, shutdown.set)

    timed_out = False
    try:
        await asyncio.wait_for(shutdown.wait(), timeout=timeout)
    except asyncio.TimeoutError:
        timed_out = True
        console_log(f"Test timeout reached ({timeout}s)", level="warn")

    # If we timed out without a report, try to generate one as fallback
    if timed_out and report_result["path"] is None:
        await daemon._generate_test_report_if_pending()
        # Check if the fallback produced a report via the test_log
        if hasattr(daemon, "_test_log"):
            report_dir = Path(daemon._test_log.report_dir)
            # Find the most recent report
            reports = sorted(report_dir.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
            if reports:
                report_result["path"] = reports[0]

    print("\nShutting down test daemon...", flush=True)
    await daemon.stop()
    await runner.cleanup()

    if timed_out and report_result["path"] is None:
        raise TimeoutError(f"Test timed out after {timeout}s with no report")

    return report_result["path"]


def _wire_test_console(daemon: SwarmDaemon) -> None:
    """Wire pilot + daemon events to console_log with structured prefixes."""
    if daemon.pilot:
        daemon.pilot.on_state_changed(lambda w: console_log(f"[STATE] {w.name} -> {w.state.value}"))
        daemon.pilot.on_task_assigned(
            lambda w, t, m="": console_log(f'[TASK] "{t.title}" -> {w.name}')
        )
        daemon.pilot.on_workers_changed(lambda: console_log("[HIVE] Workers changed"))
        daemon.pilot.on_hive_empty(lambda: console_log("[HIVE] All workers gone", level="warn"))
        daemon.pilot.on_hive_complete(lambda: console_log("[HIVE] Complete — all tasks done"))

        # Drone decisions (skip NONE to reduce noise)
        if hasattr(daemon.pilot, "_emit_decisions"):
            daemon.pilot.on(
                "drone_decision",
                lambda w, content, d: (
                    console_log(f"[DRONE] {w.name}: {d.decision.value} — {d.reason}")
                    if d.decision.value != "NONE"
                    else None
                ),
            )

        daemon.pilot.on_escalate(
            lambda w, reason: console_log(f"[ESCALATE] {w.name}: {reason}", level="warn")
        )

    # Queen analysis events
    daemon.on(
        "queen_analysis",
        lambda wn, action, reasoning, conf: console_log(
            f"[QUEEN] {wn}: {action} (confidence={conf:.2f})"
        ),
    )

    daemon.task_board.on_change(lambda: console_log("[TASK] Board updated"))


def _print_test_banner(daemon: SwarmDaemon, host: str, port: int, timeout: int) -> None:
    """Print structured startup banner for test mode."""
    import importlib.metadata

    try:
        version = importlib.metadata.version("swarm-ai")
    except importlib.metadata.PackageNotFoundError:
        version = "dev"

    Y = "\033[33m"
    C = "\033[36m"
    D = "\033[2m"
    B = "\033[1m"
    R = "\033[0m"

    n_workers = len(daemon.workers)
    n_tasks = len(daemon.task_board.all_tasks)
    session = daemon.config.session_name

    print(f"\n{Y}{B}Swarm Test Runner v{version}{R}", flush=True)
    print(f"  {D}\u251c\u2500{R} Dashboard:  {C}http://{host}:{port}{R}", flush=True)
    print(f"  {D}\u251c\u2500{R} Workers:    {Y}{n_workers}{R} test worker(s)", flush=True)
    print(f"  {D}\u251c\u2500{R} Tasks:      {Y}{n_tasks}{R} loaded", flush=True)
    print(f"  {D}\u251c\u2500{R} Timeout:    {timeout}s", flush=True)
    print(f"  {D}\u251c\u2500{R} Session:    {session}", flush=True)
    print(f"  {D}\u2514\u2500{R} Port:       {port}", flush=True)
    print(flush=True)


_console_pipe_broken = False


def console_log(msg: str, level: str = "info") -> None:
    """Print a timestamped runtime event to the console.

    Silently stops logging after the first BrokenPipeError — the parent
    terminal is gone and further attempts would just flood the error log.
    """
    global _console_pipe_broken  # noqa: PLW0603
    if _console_pipe_broken:
        return

    from datetime import datetime

    ts = datetime.now().strftime("%H:%M:%S")
    if level == "warn":
        prefix = "\033[33m\u26a0\033[0m"
    elif level == "error":
        prefix = "\033[31m\u2717\033[0m"
    else:
        prefix = " "
    try:
        print(f"[{ts}] {prefix} {msg}", flush=True)
    except BrokenPipeError:
        _console_pipe_broken = True
