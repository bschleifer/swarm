"""SwarmDaemon — long-running backend service."""

from __future__ import annotations

import asyncio
import json
import re
import time
from typing import Set

from aiohttp import web

from pathlib import Path

from swarm.config import HiveConfig, WorkerConfig, load_config, save_config
from swarm.drones.log import DroneAction, DroneLog, LogCategory, SystemAction
from swarm.drones.pilot import DronePilot
from swarm.events import EventEmitter
from swarm.logging import get_logger
from swarm.notify.bus import NotificationBus
from swarm.notify.desktop import desktop_backend
from swarm.notify.terminal import terminal_bell_backend
from swarm.queen.queen import Queen
from swarm.tasks.board import TaskBoard
from swarm.tasks.history import TaskAction, TaskHistory
from swarm.tasks.proposal import (
    AssignmentProposal,
    ProposalStatus,
    ProposalStore,
    build_worker_task_info,
)
from swarm.tasks.store import FileTaskStore
from swarm.tasks.task import (
    SwarmTask,
    TaskPriority,
    TaskStatus,
    TaskType,
    auto_classify_type,
    smart_title,
)
from swarm.tmux.hive import discover_workers, find_swarm_session
from swarm.worker.worker import Worker, WorkerState

_log = get_logger("server.daemon")


def _html_to_text(html: str) -> str:
    """Convert HTML email body to readable plain text preserving structure."""
    import html as _html

    text = html
    # Block elements → newlines
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"</p>", "\n\n", text, flags=re.IGNORECASE)
    text = re.sub(r"</div>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"</tr>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"</li>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<li[^>]*>", "  • ", text, flags=re.IGNORECASE)
    text = re.sub(r"<h[1-6][^>]*>", "\n## ", text, flags=re.IGNORECASE)
    text = re.sub(r"</h[1-6]>", "\n", text, flags=re.IGNORECASE)
    # Strip remaining tags
    text = re.sub(r"<[^>]+>", "", text)
    # Decode HTML entities
    text = _html.unescape(text)
    # Normalize whitespace within lines but preserve newlines
    lines = text.split("\n")
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in lines]
    # Collapse 3+ blank lines → 2
    text = "\n".join(lines)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


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
        self.notification_bus = self._build_notification_bus(config)
        # Apply workflow skill overrides from config
        if config.workflows:
            from swarm.tasks.workflows import apply_config_overrides

            apply_config_overrides(config.workflows)
        self.pilot: DronePilot | None = None
        self.ws_clients: Set[web.WebSocketResponse] = set()
        self.terminal_ws_clients: Set[web.WebSocketResponse] = set()
        # Track in-flight Queen analyses to prevent concurrent calls for same worker
        self._inflight_escalations: set[str] = set()
        self._inflight_completions: set[str] = set()  # keyed by "worker:task_id"
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
        self.pilot.on_task_done(self._on_task_done)
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
        if worker.name in self._inflight_escalations:
            _log.debug("skipping escalation for %s — analysis already in flight", worker.name)
            return

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
                self._inflight_escalations.add(worker.name)
                asyncio.ensure_future(self._queen_analyze_escalation(worker, reason))
            except RuntimeError:
                self._inflight_escalations.discard(worker.name)

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
            self._inflight_escalations.discard(worker.name)
            return
        finally:
            self._inflight_escalations.discard(worker.name)

        if not isinstance(result, dict):
            return

        action = result.get("action", "wait")
        confidence = float(result.get("confidence", 0.8))
        reason_lower = reason.lower()
        # User questions and plans always require user approval — the Queen
        # must never auto-act on these.  Approval-rule escalations ("choice
        # requires approval") are handled by the Queen's confidence threshold:
        # if the Queen is confident enough, it auto-acts; otherwise it creates
        # a proposal for the user.
        requires_user = "plan" in reason_lower or "user question" in reason_lower

        assessment = result.get("assessment", "")
        reasoning = result.get("reasoning", "")
        message = result.get("message", "")

        # Reject proposals with no actionable content — useless to the user
        if not assessment and not reasoning and not message:
            _log.info(
                "Queen returned empty escalation analysis for %s — dropping",
                worker.name,
            )
            return

        proposal = AssignmentProposal.escalation(
            worker_name=worker.name,
            action=action,
            assessment=assessment or reasoning or f"Escalation: {reason}",
            message=message,
            reasoning=reasoning or assessment,
            confidence=confidence,
        )

        # Race guard: another escalation may have created a proposal while Queen was thinking
        if self.proposal_store.has_pending_escalation(worker.name):
            _log.debug("dropping duplicate Queen proposal for %s", worker.name)
            return

        # Only auto-execute for routine escalations (unrecognized state, etc.)
        # where the Queen is confident. User-facing decisions always go to the user.
        if not requires_user and confidence >= self.queen.min_confidence and action != "wait":
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
            self.drone_log.add(
                SystemAction.QUEEN_AUTO_ACTED,
                worker.name,
                f"{action} ({confidence * 100:.0f}%)",
                category=LogCategory.QUEEN,
                is_notification=True,
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

    def _on_task_done(self, worker: Worker, task, resolution: str = "") -> None:
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
            self._on_proposal(proposal)
        elif self.queen.enabled and self.queen.can_call:
            key = f"{worker.name}:{task.id}"
            if key in self._inflight_completions:
                _log.debug("skipping completion analysis for %s — already in flight", key)
                return
            try:
                asyncio.get_running_loop()
                self._inflight_completions.add(key)
                asyncio.ensure_future(self._queen_analyze_completion(worker, task))
            except RuntimeError:
                self._inflight_completions.discard(key)
        else:
            # Queen unavailable — skip proposal (no way to assess completion)
            _log.info(
                "Queen unavailable — cannot assess completion for task '%s' on %s",
                task.title,
                worker.name,
            )

    async def _queen_analyze_completion(self, worker: Worker, task) -> None:
        """Ask Queen to assess whether a task is complete and draft resolution."""
        key = f"{worker.name}:{task.id}"
        # Re-check: worker may have resumed working since the event was queued
        if worker.state == WorkerState.BUZZING:
            _log.info(
                "Aborting completion analysis for '%s': worker %s resumed (BUZZING)",
                task.title,
                worker.name,
            )
            self._inflight_completions.discard(key)
            return

        try:
            from swarm.tmux.cell import capture_pane

            content = await capture_pane(worker.pane_id, lines=100)
            result = await self.queen.ask(
                f"Worker '{worker.name}' was assigned task:\n"
                f"  Title: {task.title}\n"
                f"  Description: {task.description or 'N/A'}\n"
                f"  Type: {getattr(task.task_type, 'value', task.task_type)}\n\n"
                f"The worker has been idle for {worker.state_duration:.0f}s.\n\n"
                f"Recent worker output (last 100 lines):\n{content}\n\n"
                "Analyze the output carefully. Look for concrete evidence:\n"
                "- Commits, pushes, or PRs created\n"
                "- Tests passing or failing\n"
                "- Error messages or unresolved issues\n"
                "- The worker explicitly stating it finished or got stuck\n\n"
                "Do NOT restate the task title as the resolution. Instead describe "
                "what the worker ACTUALLY DID based on the output evidence.\n\n"
                "Return JSON:\n"
                '{"done": true/false, "resolution": "what the worker actually accomplished '
                'or what remains unfinished — cite specific evidence from the output", '
                '"confidence": 0.0 to 1.0}\n\n'
                "Set done=false unless you see clear evidence of completion "
                "(commit, tests passing, worker saying done). When in doubt, say not done."
            )
        except Exception:
            _log.warning("Queen completion analysis failed for %s", worker.name, exc_info=True)
            self._inflight_completions.discard(key)
            return
        finally:
            self._inflight_completions.discard(key)

        done = result.get("done", False) if isinstance(result, dict) else False
        resolution = (
            result.get("resolution", f"Worker idle for {worker.state_duration:.0f}s")
            if isinstance(result, dict)
            else f"Worker idle for {worker.state_duration:.0f}s"
        )
        confidence = float(result.get("confidence", 0.3)) if isinstance(result, dict) else 0.3

        # Reject idle-fallback resolutions — Queen didn't provide real analysis
        if re.match(r"^worker\s+\S*\s*(?:idle|has been idle)\s+for\s+\d+", resolution, re.I):
            _log.info(
                "Queen returned idle-fallback resolution for task '%s' — not proposing",
                task.title,
            )
            return

        # Sanity check: if the resolution text contradicts "done", override
        _NOT_DONE_PHRASES = (
            "could not be verified",
            "not verified",
            "could not confirm",
            "unable to confirm",
            "unable to verify",
            "not complete",
            "not done",
            "not finished",
            "needs more work",
            "went idle without",
            "did not complete",
            "hasn't been completed",
            "recommend re-running",
        )
        res_lower = resolution.lower()
        if done and any(phrase in res_lower for phrase in _NOT_DONE_PHRASES):
            _log.info(
                "Queen said done but resolution contradicts — overriding to not done: %s",
                resolution[:120],
            )
            done = False

        if not done:
            _log.info("Queen says task '%s' is NOT done for %s", task.title, worker.name)
            return

        if confidence < 0.6:
            _log.info(
                "Queen confidence too low (%.2f) for task '%s' — skipping proposal",
                confidence,
                task.title,
            )
            return

        # Race guard: worker may have resumed while Queen was thinking
        if worker.state == WorkerState.BUZZING:
            _log.info(
                "Worker %s resumed (BUZZING) — dropping completion proposal for '%s'",
                worker.name,
                task.title,
            )
            return

        # Race guard: duplicate proposal check
        if self.proposal_store.has_pending_completion(worker.name, task.id):
            return

        proposal = AssignmentProposal.completion(
            worker_name=worker.name,
            task_id=task.id,
            task_title=task.title,
            assessment=resolution,
            reasoning=f"Worker idle for {worker.state_duration:.0f}s",
            confidence=confidence,
        )
        self._on_proposal(proposal)

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
        # When a worker resumes working, expire stale escalation AND completion
        # proposals — the worker is no longer idle so the proposals are outdated.
        if worker.state == WorkerState.BUZZING:
            # Clear in-flight analysis tracking
            self._inflight_escalations.discard(worker.name)
            self._inflight_completions = {
                k for k in self._inflight_completions if not k.startswith(f"{worker.name}:")
            }
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
        # Emit legacy "drones" type for backward compat
        self._broadcast_ws(
            {
                "type": "drones",
                "action": entry.action.value,
                "worker": entry.worker_name,
                "detail": entry.detail,
            }
        )
        # Emit new "system_log" type with category/notification info
        self._broadcast_ws(
            {
                "type": "system_log",
                "action": entry.action.value,
                "worker": entry.worker_name,
                "detail": entry.detail,
                "category": entry.category.value,
                "is_notification": entry.is_notification,
            }
        )

    def _on_proposal(self, proposal: AssignmentProposal) -> None:
        # Final dedup gate — reject if a matching pending proposal already exists
        pending = self.proposal_store.pending_for_worker(proposal.worker_name)
        for p in pending:
            if p.proposal_type == proposal.proposal_type:
                # For task-specific proposals, match on task_id
                if proposal.task_id and p.task_id == proposal.task_id:
                    _log.debug(
                        "dropping duplicate %s proposal for %s (task %s)",
                        proposal.proposal_type,
                        proposal.worker_name,
                        proposal.task_id,
                    )
                    return
                # For escalations without task_id, one per worker is enough
                if not proposal.task_id and proposal.proposal_type == "escalation":
                    _log.debug(
                        "dropping duplicate escalation proposal for %s",
                        proposal.worker_name,
                    )
                    return
        self.proposal_store.add(proposal)
        # Log to system log based on proposal type
        if proposal.proposal_type == "escalation":
            self.drone_log.add(
                SystemAction.QUEEN_ESCALATION,
                proposal.worker_name,
                proposal.assessment or proposal.reasoning or "escalation",
                category=LogCategory.QUEEN,
                is_notification=True,
            )
        elif proposal.proposal_type == "completion":
            self.drone_log.add(
                SystemAction.QUEEN_COMPLETION,
                proposal.worker_name,
                proposal.task_title or "completion",
                category=LogCategory.QUEEN,
                is_notification=True,
            )
        else:
            self.drone_log.add(
                SystemAction.QUEEN_PROPOSAL,
                proposal.worker_name,
                proposal.task_title or proposal.assessment or "proposal",
                category=LogCategory.QUEEN,
                is_notification=True,
            )
        self._broadcast_ws(
            {
                "type": "proposal_created",
                "proposal": self._proposal_dict(proposal),
                "pending_count": len(self.proposal_store.pending),
            }
        )
        if proposal.proposal_type == "escalation":
            self.notification_bus.emit_escalation(
                proposal.worker_name,
                f"Queen escalation: {proposal.assessment or proposal.task_title}",
            )
        else:
            self.notification_bus.emit_task_assigned(
                proposal.worker_name,
                f"Proposal: {proposal.task_title}",
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
        # Completion proposals also pop a modal with task resolution details
        elif proposal.proposal_type == "completion":
            task = self.task_board.get(proposal.task_id)
            has_email = bool(task and task.source_email_id)
            self._broadcast_ws(
                {
                    "type": "queen_completion",
                    "proposal_id": proposal.id,
                    "worker": proposal.worker_name,
                    "task_id": proposal.task_id,
                    "task_title": proposal.task_title,
                    "assessment": proposal.assessment,
                    "reasoning": proposal.reasoning,
                    "confidence": proposal.confidence,
                    "has_source_email": has_email,
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

    def _proposal_dict(self, proposal: AssignmentProposal) -> dict:
        d: dict = {
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
        if proposal.proposal_type == "completion" and proposal.task_id:
            task = self.task_board.get(proposal.task_id)
            d["has_source_email"] = bool(task and task.source_email_id)
        return d

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

        self._broadcast_ws({"type": "config_changed"})
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
        Waits for the worker to be idle BEFORE sending any commands — never
        injects text into a BUZZING (actively working) pane.
        """
        from swarm.tmux.cell import capture_pane, get_pane_command, send_keys
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
            self.init_pilot(enabled=self.config.drones.enabled)
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
        source_email_id: str = "",
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
            source_email_id=source_email_id,
        )
        self.task_history.append(task.id, TaskAction.CREATED, actor=actor, detail=title)
        self.drone_log.add(
            SystemAction.TASK_CREATED,
            actor,
            title,
            category=LogCategory.TASK,
        )
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
            msg = self._build_task_message(task)
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
                    from swarm.tmux.cell import send_enter

                    await asyncio.sleep(0.3)
                    await send_enter(pane_id)
            except Exception:
                _log.warning("failed to send task message to %s", worker_name, exc_info=True)
                # Undo assignment — worker was /clear'd but never got the task
                self.task_board.unassign(task_id)
                self.task_history.append(
                    task_id,
                    TaskAction.UNASSIGNED,
                    actor="system",
                    detail=f"send failed to {worker_name} — returned to pending",
                )
                self._broadcast_ws(
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

    @staticmethod
    def _task_detail_parts(task: SwarmTask) -> list[str]:
        """Collect title, description, and tags into a parts list (no attachments)."""
        parts: list[str] = [f"#{task.number}: {task.title}" if task.number else task.title]
        if task.description:
            parts.append(task.description)
        if task.tags:
            parts.append(f"Tags: {', '.join(task.tags)}")
        return parts

    @staticmethod
    def _attachment_lines(task: SwarmTask) -> str:
        """Format attachment paths as separate lines for the worker."""
        if not task.attachments:
            return ""
        lines = ["\nAttachments (read these files for context):"]
        for a in task.attachments:
            lines.append(f"  - {a}")
        return "\n".join(lines)

    @staticmethod
    def _build_task_message(task: SwarmTask) -> str:
        """Build a message string describing a task for a worker.

        If the task type has a dedicated Claude Code skill (e.g. ``/feature``),
        the message is formatted as a skill invocation so the worker's Claude
        session handles the full pipeline.  Otherwise, inline workflow steps
        are appended as before.

        Attachments are always listed on separate lines (never squished into
        the skill command's quoted argument) so the worker can see and read them.
        """
        from swarm.tasks.workflows import get_skill_command, get_workflow_instructions

        skill = get_skill_command(task.task_type)
        if skill:
            desc = " ".join(SwarmDaemon._task_detail_parts(task))
            msg = f'{skill} "{desc}"'
            attachments = SwarmDaemon._attachment_lines(task)
            if attachments:
                msg = f"{msg}{attachments}"
            return msg

        # Fallback: inline workflow instructions (CHORE, unknown types).
        prefix = f"Task #{task.number}: " if task.number else "Task: "
        parts = [f"{prefix}{task.title}"]
        if task.description:
            parts.append(f"\n{task.description}")
        attachments = SwarmDaemon._attachment_lines(task)
        if attachments:
            parts.append(attachments)
        if task.tags:
            parts.append(f"\nTags: {', '.join(task.tags)}")
        workflow = get_workflow_instructions(task.task_type)
        if workflow:
            parts.append(f"\n{workflow}")
        return "\n".join(parts)

    def complete_task(
        self, task_id: str, actor: str = "user", resolution: str = "", send_reply: bool = False
    ) -> bool:
        """Complete a task. Raises if not found or wrong state.

        When *send_reply* is True and the task originated from an email,
        draft and send a reply via the Graph API.
        """
        task = self.task_board.get(task_id)
        if not task:
            raise TaskOperationError(f"Task '{task_id}' not found")
        if task.status not in (TaskStatus.ASSIGNED, TaskStatus.IN_PROGRESS):
            raise TaskOperationError(f"Task '{task_id}' cannot be completed ({task.status.value})")

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
        """Draft a reply via Queen and create as draft in Outlook."""
        try:
            # Resolve RFC 822 Message-ID (<...@...>) to Graph message ID
            graph_id = message_id
            if "<" in message_id and "@" in message_id:
                resolved = await self.graph_mgr.resolve_message_id(message_id)
                if not resolved:
                    reason = f"Could not resolve RFC 822 ID '{message_id[:60]}'"
                    _log.warning(reason)
                    self._broadcast_ws(
                        {
                            "type": "draft_reply_failed",
                            "task_title": task_title,
                            "task_id": task_id,
                            "error": reason,
                        }
                    )
                    return
                graph_id = resolved

            reply_text = await self.queen.draft_email_reply(task_title, task_type, resolution)
            ok = await self.graph_mgr.create_reply_draft(graph_id, reply_text)
            if ok:
                _log.info("Draft reply created for task '%s'", task_title[:50])
                self._broadcast_ws({"type": "draft_reply_ok", "task_title": task_title})
                self.drone_log.add(
                    SystemAction.DRAFT_OK,
                    "system",
                    task_title[:80],
                    category=LogCategory.SYSTEM,
                )
            else:
                _log.warning("Draft reply failed for task '%s'", task_title[:50])
                self._broadcast_ws(
                    {
                        "type": "draft_reply_failed",
                        "task_title": task_title,
                        "task_id": task_id,
                        "error": "Graph API returned failure",
                    }
                )
                self.drone_log.add(
                    SystemAction.DRAFT_FAILED,
                    "system",
                    task_title[:80],
                    category=LogCategory.SYSTEM,
                    is_notification=True,
                )
        except Exception as exc:
            _log.warning("Draft reply error for '%s'", task_title[:50], exc_info=True)
            self._broadcast_ws(
                {
                    "type": "draft_reply_failed",
                    "task_title": task_title,
                    "task_id": task_id,
                    "error": str(exc)[:200],
                }
            )
            self.drone_log.add(
                SystemAction.DRAFT_FAILED,
                "system",
                f"{task_title[:60]}: {str(exc)[:80]}",
                category=LogCategory.SYSTEM,
                is_notification=True,
            )

    async def retry_draft_reply(self, task_id: str) -> None:
        """Retry drafting an email reply for an already-completed task."""
        task = self.task_board.get(task_id)
        if not task:
            raise TaskOperationError(f"Task '{task_id}' not found")
        if not task.source_email_id:
            raise TaskOperationError("Task has no source email")
        if not task.resolution:
            raise TaskOperationError("Task has no resolution text")
        if not self.graph_mgr:
            raise TaskOperationError("Microsoft Graph not configured")

        await self._send_completion_reply(
            task.source_email_id, task.title, task.task_type.value, task.resolution, task_id
        )

    def unassign_task(self, task_id: str, actor: str = "user") -> bool:
        """Unassign a task, returning it to PENDING. Raises if not found or wrong state."""
        task = self.task_board.get(task_id)
        if not task:
            raise TaskOperationError(f"Task '{task_id}' not found")
        if task.status not in (TaskStatus.ASSIGNED, TaskStatus.IN_PROGRESS):
            raise TaskOperationError(f"Task '{task_id}' cannot be unassigned ({task.status.value})")
        result = self.task_board.unassign(task_id)
        if result:
            self.pilot.clear_proposed_completion(task_id)
            self.task_history.append(task_id, TaskAction.EDITED, actor=actor, detail="unassigned")
        return result

    def reopen_task(self, task_id: str, actor: str = "user") -> bool:
        """Reopen a completed or failed task, returning it to PENDING."""
        task = self.task_board.get(task_id)
        if not task:
            raise TaskOperationError(f"Task '{task_id}' not found")
        if task.status not in (TaskStatus.COMPLETED, TaskStatus.FAILED):
            raise TaskOperationError(f"Task '{task_id}' cannot be reopened ({task.status.value})")
        result = self.task_board.reopen(task_id)
        if result:
            self.pilot.clear_proposed_completion(task_id)
            self.task_history.append(task_id, TaskAction.REOPENED, actor=actor)
        return result

    def fail_task(self, task_id: str, actor: str = "user") -> bool:
        """Fail a task. Raises if not found."""
        task = self.task_board.get(task_id)
        if not task:
            raise TaskOperationError(f"Task '{task_id}' not found")
        result = self.task_board.fail(task_id)
        if result:
            self.task_history.append(task_id, TaskAction.FAILED, actor=actor)
            self.drone_log.add(
                SystemAction.TASK_FAILED,
                actor,
                task.title,
                category=LogCategory.TASK,
                is_notification=True,
            )
        return result

    def remove_task(self, task_id: str, actor: str = "user") -> bool:
        """Remove a task. Raises if not found."""
        task = self.task_board.get(task_id)
        if not task:
            raise TaskOperationError(f"Task '{task_id}' not found")
        self.task_board.remove(task_id)
        self.task_history.append(task_id, TaskAction.REMOVED, actor=actor)
        self.drone_log.add(
            SystemAction.TASK_REMOVED,
            actor,
            task.title,
            category=LogCategory.TASK,
        )
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

    async def approve_proposal(self, proposal_id: str, draft_response: bool = False) -> bool:
        """Approve a Queen proposal: assign task or execute escalation action.

        When *draft_response* is True and the proposal is a completion with a
        source email, the reply pipeline is triggered.
        """
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

        if proposal.proposal_type == "completion":
            resolution = proposal.assessment or proposal.reasoning or ""
            self.complete_task(
                proposal.task_id, actor="queen", resolution=resolution, send_reply=draft_response
            )
            proposal.status = ProposalStatus.APPROVED
            self.drone_log.add(
                DroneAction.APPROVED,
                proposal.worker_name,
                f"task completed: {proposal.task_title}",
            )
            self.proposal_store.clear_resolved()
            self._broadcast_proposals()
            return True

        if worker.state not in (WorkerState.RESTING, WorkerState.WAITING):
            proposal.status = ProposalStatus.EXPIRED
            self.proposal_store.clear_resolved()
            self._broadcast_proposals()
            raise TaskOperationError(
                f"Worker '{proposal.worker_name}' is {worker.state.value}, not idle"
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
        # Allow pilot to re-propose this task if it stays idle
        if proposal.proposal_type == "completion" and proposal.task_id:
            self.pilot.clear_proposed_completion(proposal.task_id)
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
            # Allow pilot to re-propose rejected completion tasks
            if p.proposal_type == "completion" and p.task_id:
                self.pilot.clear_proposed_completion(p.task_id)
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
        """Create a task with auto-title generation and type classification.

        If *title* is empty, uses Claude to generate one from the description.
        If *task_type* is None, auto-classifies from title + description.
        """
        if not title and description:
            title = await smart_title(description) or ""
        if not title:
            raise SwarmOperationError("title or description required")
        if task_type is None:
            task_type = auto_classify_type(title, description)
        return self.create_task(
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
        """Fetch an image from a URL and save as attachment. Returns saved path.

        Supports http/https and file:/// URLs (with Windows-to-WSL path conversion).
        Raises ValueError for unsupported schemes, SwarmOperationError on fetch failure.
        """
        import aiohttp as _aiohttp

        if url.startswith("blob:"):
            raise ValueError("blob: URLs cannot be fetched")
        if not url.startswith(("http://", "https://", "file:///")):
            raise ValueError("unsupported URL scheme")

        if url.startswith("file:///"):
            from urllib.parse import unquote as _unquote

            local_path = _unquote(url[8:])  # strip file:///
            # Convert Windows path to WSL if needed
            if len(local_path) > 1 and local_path[1] == ":":
                drive = local_path[0].lower()
                local_path = f"/mnt/{drive}{local_path[2:].replace(chr(92), '/')}"
            fp = Path(local_path)
            if not fp.exists():
                raise FileNotFoundError(f"file not found: {local_path}")
            img_data = fp.read_bytes()
            filename = fp.name
        else:
            async with _aiohttp.ClientSession() as sess:
                async with sess.get(url, timeout=_aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status != 200:
                        raise SwarmOperationError(f"HTTP {resp.status}")
                    img_data = await resp.read()
                    filename = url.split("/")[-1].split("?")[0] or "image.png"

        if not img_data:
            raise SwarmOperationError("empty response from URL")

        return self.save_attachment(filename, img_data)

    async def process_email_data(
        self,
        subject: str,
        body_content: str,
        body_type: str,
        attachment_dicts: list[dict],
        effective_id: str,
        *,
        graph_token: str = "",
    ) -> dict:
        """Process fetched email data into task fields.

        Converts HTML to text, saves attachments, generates title, classifies type.
        Returns a dict ready for the frontend task-creation form.
        """
        if body_type.lower() == "html":
            body_text = _html_to_text(body_content)
        else:
            body_text = body_content.strip()

        description = f"Subject: {subject}\n\n{body_text}" if subject else body_text

        # Save attachments
        import base64

        paths: list[str] = []
        for att in attachment_dicts:
            if att.get("@odata.type") != "#microsoft.graph.fileAttachment":
                continue
            name = att.get("name", "attachment")
            raw_b64 = att.get("contentBytes", "")
            if raw_b64:
                content_bytes = base64.b64decode(raw_b64)
                if content_bytes:
                    paths.append(self.save_attachment(name, content_bytes))

        title = await smart_title(description) or subject
        task_type = auto_classify_type(title, description)

        return {
            "title": title,
            "description": description,
            "task_type": task_type.value,
            "attachments": paths,
            "message_id": effective_id,
        }

    def toggle_drones(self) -> bool:
        """Toggle drone pilot and persist to config. Returns new enabled state."""
        if not self.pilot:
            return False
        new_state = self.pilot.toggle()
        self.config.drones.enabled = new_state
        self.save_config()
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
        """Send Enter to all RESTING/WAITING workers. Returns count of workers continued."""
        from swarm.tmux.cell import send_enter

        count = 0
        for w in list(self.workers):
            if w.state in (WorkerState.RESTING, WorkerState.WAITING):
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
                worker_outputs[w.name] = await capture_pane(w.pane_id, lines=60)
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

    async def analyze_worker(self, worker_name: str, *, force: bool = False) -> dict:
        """Run Queen analysis on a specific worker. Returns Queen's analysis dict.

        Does NOT include full hive context — per-worker analysis should focus
        on just that worker's pane output.  Includes assigned task info so the
        Queen can recommend ``complete_task`` when appropriate.
        Use ``coordinate_hive()`` for hive-wide analysis.
        """
        from swarm.tmux.cell import capture_pane

        worker = self._require_worker(worker_name)
        content = await capture_pane(worker.pane_id)

        task_info = build_worker_task_info(self.task_board, worker.name)

        return await self.queen.analyze_worker(
            worker.name, content, force=force, task_info=task_info
        )

    async def coordinate_hive(self, *, force: bool = False) -> dict:
        """Run Queen coordination across the entire hive. Returns coordination dict."""
        hive_ctx = await self.gather_hive_context()
        return await self.queen.coordinate_hive(hive_ctx, force=force)

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
