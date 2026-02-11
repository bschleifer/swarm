"""SwarmDaemon — long-running backend service."""

from __future__ import annotations

import asyncio
import json
import time
from typing import Set

from aiohttp import web

from pathlib import Path

from swarm.config import HiveConfig, WorkerConfig, load_config, save_config
from swarm.drones.log import DroneAction, DroneLog
from swarm.drones.pilot import DronePilot
from swarm.events import EventEmitter
from swarm.logging import get_logger
from swarm.notify.bus import NotificationBus
from swarm.notify.desktop import desktop_backend
from swarm.notify.terminal import terminal_bell_backend
from swarm.queen.queen import Queen
from swarm.tasks.board import TaskBoard
from swarm.tasks.history import TaskAction, TaskHistory
from swarm.tasks.proposal import AssignmentProposal, ProposalStatus, ProposalStore
from swarm.tasks.store import FileTaskStore
from swarm.tasks.task import SwarmTask, TaskPriority, TaskStatus, TaskType
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
        self.task_history = TaskHistory()
        self.queen = Queen(config=config.queen, session_name=config.session_name)
        self.proposal_store = ProposalStore()
        self.notification_bus = self._build_notification_bus(config)
        # Apply workflow skill overrides from config
        if config.workflows:
            from swarm.tasks.workflows import apply_config_overrides

            apply_config_overrides(config.workflows)
        self.pilot: DronePilot | None = None
        self.ws_clients: Set[web.WebSocketResponse] = set()
        self.terminal_ws_clients: Set[web.WebSocketResponse] = set()
        self.start_time = time.time()
        self._config_mtime: float = 0.0
        self._mtime_task: asyncio.Task | None = None
        # Microsoft Graph OAuth
        self.graph_mgr = self._build_graph_manager(config)
        self._graph_auth_pending: dict[str, str] = {}  # state → code_verifier
        self._wire_task_board()

    def _wire_task_board(self) -> None:
        """Wire task_board.on_change to auto-broadcast to WS clients."""
        self.task_board.on_change(self._on_task_board_changed)

    def _on_task_board_changed(self) -> None:
        self._broadcast_ws({"type": "tasks_changed"})
        self._expire_stale_proposals()

    def _build_notification_bus(self, config: HiveConfig) -> NotificationBus:
        bus = NotificationBus(debounce_seconds=config.notifications.debounce_seconds)
        if config.notifications.terminal_bell:
            bus.add_backend(terminal_bell_backend)
        if config.notifications.desktop:
            bus.add_backend(desktop_backend)
        return bus

    @staticmethod
    def _build_graph_manager(config: HiveConfig):
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
        self.pilot.on_proposal(self._on_proposal)
        self.pilot._pending_proposals_check = lambda: bool(self.proposal_store.pending)
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

        # Trigger Queen analysis if enabled
        if self.queen.enabled and self.queen.can_call:
            try:
                asyncio.get_running_loop()
                asyncio.ensure_future(self._queen_analyze_escalation(worker, reason))
            except RuntimeError:
                pass  # No running event loop (test/CLI context)

    async def _queen_analyze_escalation(self, worker: Worker, reason: str) -> None:
        """Ask Queen to analyze an escalated worker and act or propose.

        High-confidence actions are executed immediately. Low-confidence
        actions (and plans) are surfaced to the user as proposals.
        """
        try:
            from swarm.tmux.cell import capture_pane

            content = await capture_pane(worker.pane_id)
            hive_ctx = await self.gather_hive_context()
            result = await self.queen.analyze_worker(worker.name, content, hive_context=hive_ctx)
        except Exception:
            _log.warning("Queen escalation analysis failed for %s", worker.name, exc_info=True)
            return

        if not isinstance(result, dict):
            return

        action = result.get("action", "wait")
        confidence = float(result.get("confidence", 0.8))
        is_plan = "plan" in reason.lower()

        proposal = AssignmentProposal(
            worker_name=worker.name,
            proposal_type="escalation",
            assessment=result.get("assessment", ""),
            queen_action=action,
            message=result.get("message", ""),
            reasoning=result.get("reasoning", ""),
            confidence=confidence,
        )

        # Plans always require user approval; other actions auto-execute
        # when the Queen is confident enough.
        if not is_plan and confidence >= self.queen.min_confidence and action != "wait":
            _log.info(
                "Queen auto-acting on %s: %s (confidence=%.0f%%)",
                worker.name,
                action,
                confidence * 100,
            )
            await self._execute_escalation_proposal(proposal)
            self.drone_log.add(
                DroneAction.CONTINUED,
                worker.name,
                f"Queen auto-acted: {action} ({confidence * 100:.0f}%)",
            )
            self._broadcast_ws(
                {
                    "type": "queen_auto_acted",
                    "worker": worker.name,
                    "action": action,
                    "confidence": confidence,
                    "assessment": result.get("assessment", ""),
                }
            )
        else:
            self._on_proposal(proposal)

    async def _execute_escalation_proposal(self, proposal: AssignmentProposal) -> bool:
        """Execute an approved escalation proposal's recommended action."""
        from swarm.tmux.cell import send_enter, send_keys
        from swarm.worker.manager import revive_worker

        worker = self.get_worker(proposal.worker_name)
        if not worker:
            return False

        action = proposal.queen_action
        if action == "send_message" and proposal.message:
            await send_keys(worker.pane_id, proposal.message)
        elif action == "continue":
            await send_enter(worker.pane_id)
        elif action == "restart":
            await revive_worker(worker, session_name=self.config.session_name)
            worker.record_revive()
        # "wait" is a no-op
        return True

    def _worker_task_map(self) -> dict[str, str]:
        """Return {worker_name: task_title} for all assigned/in-progress tasks."""
        result: dict[str, str] = {}
        for t in self.task_board.active_tasks:
            if t.assigned_worker:
                result[t.assigned_worker] = t.title
        return result

    def _on_workers_changed(self) -> None:
        task_map = self._worker_task_map()
        self._broadcast_ws(
            {
                "type": "workers_changed",
                "workers": [{"name": w.name, "state": w.state.value} for w in self.workers],
                "worker_tasks": task_map,
            }
        )
        self._expire_stale_proposals()
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

    def _on_proposal(self, proposal: AssignmentProposal) -> None:
        self.proposal_store.add(proposal)
        self._broadcast_ws(
            {
                "type": "proposal_created",
                "proposal": self._proposal_dict(proposal),
                "pending_count": len(self.proposal_store.pending),
            }
        )
        self.notification_bus.emit_escalation(
            proposal.worker_name,
            f"Queen proposes: {proposal.task_title}",
        )
        # Escalation proposals pop up a modal so the user sees them immediately
        if proposal.proposal_type == "escalation":
            self._broadcast_ws(
                {
                    "type": "queen_escalation",
                    "proposal_id": proposal.id,
                    "worker": proposal.worker_name,
                    "assessment": proposal.assessment,
                    "reasoning": proposal.reasoning,
                    "action": proposal.queen_action,
                    "message": proposal.message,
                    "confidence": proposal.confidence,
                }
            )

    def _expire_stale_proposals(self) -> None:
        """Expire proposals where the task or worker is no longer valid."""
        valid_task_ids = {t.id for t in self.task_board.available_tasks}
        valid_worker_names = {w.name for w in self.workers}
        expired = self.proposal_store.expire_stale(valid_task_ids, valid_worker_names)
        if expired:
            self.proposal_store.clear_resolved()
            self._broadcast_proposals()

    @staticmethod
    def _proposal_dict(proposal: AssignmentProposal) -> dict:
        return {
            "id": proposal.id,
            "worker_name": proposal.worker_name,
            "task_id": proposal.task_id,
            "task_title": proposal.task_title,
            "message": proposal.message,
            "reasoning": proposal.reasoning,
            "confidence": proposal.confidence,
            "proposal_type": proposal.proposal_type,
            "assessment": proposal.assessment,
            "queen_action": proposal.queen_action,
            "status": proposal.status.value,
            "created_at": proposal.created_at,
            "age": round(proposal.age, 1),
        }

    def _broadcast_proposals(self) -> None:
        pending = self.proposal_store.pending
        self._broadcast_ws(
            {
                "type": "proposals_changed",
                "proposals": [self._proposal_dict(p) for p in pending],
                "pending_count": len(pending),
            }
        )

    def _hot_apply_config(self) -> None:
        """Apply config changes to pilot, queen, and notification bus."""
        if self.pilot:
            self.pilot.drone_config = self.config.drones
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
        except Exception:
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
            except Exception:
                pass
        self.ws_clients.clear()
        # Close terminal WebSocket connections too
        for ws in list(self.terminal_ws_clients):
            try:
                await ws.close()
            except Exception:
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

    # --- Per-worker tmux operations ---

    async def send_to_worker(self, name: str, message: str, *, _log_operator: bool = True) -> None:
        """Send text to a worker's tmux pane."""
        from swarm.tmux.cell import send_keys

        worker = self._require_worker(name)
        await send_keys(worker.pane_id, message)
        if _log_operator:
            self.drone_log.add(DroneAction.OPERATOR, name, "sent message")

    async def _prep_worker_for_task(self, pane_id: str) -> None:
        """Send /get-latest and /clear before a new task assignment.

        Ensures the worker has the latest code and a fresh context window.
        Each command needs time to execute before the next is sent.
        """
        from swarm.tmux.cell import send_keys
        from swarm.worker.state import classify_pane_content
        from swarm.tmux.cell import capture_pane, get_pane_command

        # Send /get-latest to pull latest code
        await send_keys(pane_id, "/get-latest")
        # Wait for it to finish — poll until worker returns to idle prompt
        for _ in range(30):  # max ~15 seconds
            await asyncio.sleep(0.5)
            cmd = await get_pane_command(pane_id)
            content = await capture_pane(pane_id)
            state = classify_pane_content(cmd, content)
            if state == WorkerState.RESTING:
                break
        # Clear context window
        await send_keys(pane_id, "/clear")
        await asyncio.sleep(0.5)

    async def continue_worker(self, name: str) -> None:
        """Send Enter to a worker's tmux pane."""
        from swarm.tmux.cell import send_enter

        worker = self._require_worker(name)
        await send_enter(worker.pane_id)
        self.drone_log.add(DroneAction.OPERATOR, name, "continued (manual)")

    async def interrupt_worker(self, name: str) -> None:
        """Send Ctrl-C to a worker's tmux pane."""
        from swarm.tmux.cell import send_interrupt

        worker = self._require_worker(name)
        await send_interrupt(worker.pane_id)
        self.drone_log.add(DroneAction.OPERATOR, name, "interrupted (Ctrl-C)")

    async def escape_worker(self, name: str) -> None:
        """Send Escape to a worker's tmux pane."""
        from swarm.tmux.cell import send_escape

        worker = self._require_worker(name)
        await send_escape(worker.pane_id)
        self.drone_log.add(DroneAction.OPERATOR, name, "sent Escape")

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
        self.drone_log.add(DroneAction.OPERATOR, name, "killed")
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
        self.drone_log.add(DroneAction.OPERATOR, name, "revived (manual)")
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
        task_type: TaskType = TaskType.CHORE,
        tags: list[str] | None = None,
        depends_on: list[str] | None = None,
        attachments: list[str] | None = None,
        actor: str = "user",
    ) -> SwarmTask:
        """Create a task. Broadcast happens via task_board.on_change."""
        task = self.task_board.create(
            title=title,
            description=description,
            priority=priority,
            task_type=task_type,
            tags=tags,
            depends_on=depends_on,
            attachments=attachments,
        )
        self.task_history.append(task.id, TaskAction.CREATED, actor=actor, detail=title)
        return task

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
            if actor == "user":
                self.drone_log.add(
                    DroneAction.OPERATOR, worker_name, f"task assigned: {task.title}"
                )
            msg = message if message else self._build_task_message(task)
            try:
                # Prep the worker: pull latest code and clear context window
                pane_id = self._require_worker(worker_name).pane_id
                await self._prep_worker_for_task(pane_id)
                await self.send_to_worker(worker_name, msg, _log_operator=False)
                # Long messages trigger Claude Code's paste-confirmation prompt
                # ("[Pasted text … +N lines]"). Send a second Enter after a short
                # delay to accept the paste and submit the message.
                if "\n" in msg or len(msg) > 200:
                    from swarm.tmux.cell import send_enter

                    await asyncio.sleep(0.3)
                    await send_enter(pane_id)
            except Exception:
                _log.warning("failed to send task message to %s", worker_name, exc_info=True)
        return result

    @staticmethod
    def _task_detail_parts(task: SwarmTask) -> list[str]:
        """Collect title, description, attachments, and tags into a parts list."""
        parts: list[str] = [task.title]
        if task.description:
            parts.append(task.description)
        if task.attachments:
            parts.append("Attachments:")
            for a in task.attachments:
                parts.append(f"  - {a}")
        if task.tags:
            parts.append(f"Tags: {', '.join(task.tags)}")
        return parts

    @staticmethod
    def _build_task_message(task: SwarmTask) -> str:
        """Build a message string describing a task for a worker.

        If the task type has a dedicated Claude Code skill (e.g. ``/feature``),
        the message is formatted as a skill invocation so the worker's Claude
        session handles the full pipeline.  Otherwise, inline workflow steps
        are appended as before.
        """
        from swarm.tasks.workflows import get_skill_command, get_workflow_instructions

        skill = get_skill_command(task.task_type)
        if skill:
            desc = " ".join(SwarmDaemon._task_detail_parts(task))
            return f'{skill} "{desc}"'

        # Fallback: inline workflow instructions (CHORE, unknown types).
        parts = [f"Task: {task.title}"]
        if task.description:
            parts.append(f"\n{task.description}")
        if task.attachments:
            parts.append("\nAttachments:")
            for a in task.attachments:
                parts.append(f"  - {a}")
        if task.tags:
            parts.append(f"\nTags: {', '.join(task.tags)}")
        workflow = get_workflow_instructions(task.task_type)
        if workflow:
            parts.append(f"\n{workflow}")
        return "\n".join(parts)

    def complete_task(self, task_id: str, actor: str = "user", resolution: str = "") -> bool:
        """Complete a task. Raises if not found or wrong state."""
        task = self.task_board.get(task_id)
        if not task:
            raise TaskOperationError(f"Task '{task_id}' not found")
        if task.status not in (TaskStatus.ASSIGNED, TaskStatus.IN_PROGRESS):
            raise TaskOperationError(f"Task '{task_id}' cannot be completed ({task.status.value})")
        result = self.task_board.complete(task_id, resolution=resolution)
        if result:
            self.task_history.append(task_id, TaskAction.COMPLETED, actor=actor, detail=resolution)
        return result

    def unassign_task(self, task_id: str, actor: str = "user") -> bool:
        """Unassign a task, returning it to PENDING. Raises if not found or wrong state."""
        task = self.task_board.get(task_id)
        if not task:
            raise TaskOperationError(f"Task '{task_id}' not found")
        if task.status not in (TaskStatus.ASSIGNED, TaskStatus.IN_PROGRESS):
            raise TaskOperationError(f"Task '{task_id}' cannot be unassigned ({task.status.value})")
        result = self.task_board.unassign(task_id)
        if result:
            self.task_history.append(task_id, TaskAction.EDITED, actor=actor, detail="unassigned")
        return result

    def fail_task(self, task_id: str, actor: str = "user") -> bool:
        """Fail a task. Raises if not found."""
        task = self.task_board.get(task_id)
        if not task:
            raise TaskOperationError(f"Task '{task_id}' not found")
        result = self.task_board.fail(task_id)
        if result:
            self.task_history.append(task_id, TaskAction.FAILED, actor=actor)
        return result

    def remove_task(self, task_id: str, actor: str = "user") -> bool:
        """Remove a task. Raises if not found."""
        if not self.task_board.remove(task_id):
            raise TaskOperationError(f"Task '{task_id}' not found")
        self.task_history.append(task_id, TaskAction.REMOVED, actor=actor)
        return True

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
        """Edit a task. Raises if not found."""
        task = self.task_board.get(task_id)
        if not task:
            raise TaskOperationError(f"Task '{task_id}' not found")
        result = self.task_board.update(
            task_id,
            title=title,
            description=description,
            priority=priority,
            task_type=task_type,
            tags=tags,
            attachments=attachments,
            depends_on=depends_on,
        )
        if result:
            self.task_history.append(task_id, TaskAction.EDITED, actor=actor)
        return result

    async def approve_proposal(self, proposal_id: str) -> bool:
        """Approve a Queen proposal: assign task or execute escalation action."""
        proposal = self.proposal_store.get(proposal_id)
        if not proposal or proposal.status != ProposalStatus.PENDING:
            raise TaskOperationError(f"Proposal '{proposal_id}' not found or not pending")

        worker = self.get_worker(proposal.worker_name)
        if not worker:
            proposal.status = ProposalStatus.EXPIRED
            self.proposal_store.clear_resolved()
            self._broadcast_proposals()
            raise WorkerNotFoundError(f"Worker '{proposal.worker_name}' no longer exists")

        if proposal.proposal_type == "escalation":
            await self._execute_escalation_proposal(proposal)
            proposal.status = ProposalStatus.APPROVED
            self.drone_log.add(
                DroneAction.APPROVED,
                proposal.worker_name,
                f"proposal approved: {proposal.queen_action}",
            )
            self.proposal_store.clear_resolved()
            self._broadcast_proposals()
            return True

        if worker.state != WorkerState.RESTING:
            proposal.status = ProposalStatus.EXPIRED
            self.proposal_store.clear_resolved()
            self._broadcast_proposals()
            raise TaskOperationError(
                f"Worker '{proposal.worker_name}' is {worker.state.value}, not RESTING"
            )

        await self.assign_task(
            proposal.task_id,
            proposal.worker_name,
            actor="queen",
            message=proposal.message or None,
        )
        proposal.status = ProposalStatus.APPROVED
        self.drone_log.add(
            DroneAction.APPROVED,
            proposal.worker_name,
            f"proposal approved: {proposal.task_title}",
        )
        self.proposal_store.clear_resolved()
        self._broadcast_proposals()
        return True

    def reject_proposal(self, proposal_id: str) -> bool:
        """Reject a Queen proposal."""
        proposal = self.proposal_store.get(proposal_id)
        if not proposal or proposal.status != ProposalStatus.PENDING:
            raise TaskOperationError(f"Proposal '{proposal_id}' not found or not pending")
        proposal.status = ProposalStatus.REJECTED
        self.drone_log.add(
            DroneAction.REJECTED,
            proposal.worker_name,
            f"proposal rejected: {proposal.task_title}",
        )
        self.proposal_store.clear_resolved()
        self._broadcast_proposals()
        return True

    def reject_all_proposals(self) -> int:
        """Reject all pending proposals. Returns count rejected."""
        pending = self.proposal_store.pending
        for p in pending:
            p.status = ProposalStatus.REJECTED
        count = len(pending)
        if count:
            self.drone_log.add(DroneAction.REJECTED, "all", f"rejected {count} proposal(s)")
            self.proposal_store.clear_resolved()
            self._broadcast_proposals()
        return count

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
        if count:
            self.drone_log.add(DroneAction.OPERATOR, "all", f"continued {count} worker(s)")
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
        if count:
            self.drone_log.add(DroneAction.OPERATOR, "all", f"broadcast to {count} worker(s)")
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
        if count:
            self.drone_log.add(DroneAction.OPERATOR, group_name, f"group send to {count} worker(s)")
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
            worker_descriptions=self._worker_descriptions(),
            approval_rules=self.config.drones.approval_rules or None,
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
