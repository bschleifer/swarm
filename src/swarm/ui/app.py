"""Bee Hive — the root Textual App for the swarm dashboard."""

from __future__ import annotations

import os
import subprocess
import threading
from collections.abc import Iterable
from typing import Any

# Ensure 24-bit truecolor so the bee theme renders correctly.
os.environ.setdefault("COLORTERM", "truecolor")

from textual.app import App, ComposeResult, Screen, SystemCommand
from textual.containers import Horizontal
from textual.theme import Theme
from textual.widgets import Footer, Header, Static

from swarm.drones.log import DroneLog
from swarm.drones.pilot import DronePilot
from swarm.config import HiveConfig, WorkerConfig
from swarm.logging import get_logger
from swarm.queen.queen import Queen
from swarm.server.daemon import SwarmOperationError
from swarm.worker.worker import WorkerState
from swarm.ui.decision_history_modal import DecisionHistoryModal
from swarm.ui.drone_log import DroneLogWidget
from swarm.ui.keys import BINDINGS
from swarm.ui.proposal_banner import (
    ApproveAllProposals,
    ProposalBannerWidget,
    RejectAllProposals,
    ReviewProposals,
)
from swarm.ui.proposal_modal import ProposalReviewModal, ProposalReviewResult
from swarm.ui.queen_modal import QueenModal
from swarm.ui.send_modal import SendModal, SendResult
from swarm.ui.task_panel import (
    CreateTaskModal,
    EditTaskModal,
    TaskEditResult,
    TaskPanelWidget,
    TaskSelected,
)
from swarm.ui.worker_detail import WorkerCommand, WorkerDetailWidget
from swarm.ui.worker_list import WorkerListWidget, WorkerSelected
from swarm.tasks.board import TaskBoard
from swarm.tasks.task import SwarmTask
from swarm.ui.confirm_modal import ConfirmModal
from swarm.ui.config_modal import ConfigModal, ConfigUpdate
from swarm.ui.add_worker_modal import AddWorkerModal
from swarm.ui.launch_modal import LaunchModal
from swarm.worker.worker import Worker, worker_state_counts

log = get_logger("ui.app")


BEE_THEME = Theme(
    name="bee",
    primary="#D8A03D",  # golden honey
    secondary="#A88FD9",  # lavender
    warning="#D8A03D",  # golden honey (titles/status)
    error="#D15D4C",  # poppy red
    success="#8CB369",  # muted leaf green
    accent="#D8A03D",  # golden honey
    foreground="#E6D2B5",  # creamy beeswax
    background="#2A1B0E",  # deep hive brown
    surface="#362415",  # warm brown surface
    panel="#3E2B1B",  # slightly lighter brown
    dark=True,
    variables={
        "button-color-foreground": "#2A1B0E",
        "footer-background": "#362415",
        "footer-key-foreground": "#D8A03D",
        "footer-description-foreground": "#B0A08A",
        "border": "#D8A03D",
        "border-blurred": "#8C6A38",
        "scrollbar": "#362415",
        "input-cursor-background": "#D8A03D",
        "input-cursor-foreground": "#2A1B0E",
        "input-selection-background": "#6A4C93",
        "block-cursor-background": "#D8A03D",
        "block-cursor-foreground": "#2A1B0E",
    },
)


class BeeHiveApp(App):
    """The Bee Hive — swarm dashboard TUI."""

    CSS_PATH = "styles.tcss"
    TITLE = "Bee Hive"
    BINDINGS = BINDINGS

    def get_system_commands(self, screen: Screen) -> Iterable[SystemCommand]:
        yield from super().get_system_commands(screen)
        yield SystemCommand(
            "Brood: Launch",
            "Start tmux session with configured workers",
            self.action_launch_hive,
        )
        yield SystemCommand(
            "Brood: Kill session",
            "Kill tmux session and all workers (Alt+H)",
            self.action_kill_session,
        )
        yield SystemCommand(
            "Config",
            "Edit config — add/remove workers, change paths (Alt+O)",
            self.action_open_config,
        )
        yield SystemCommand(
            "Drones: Toggle",
            "Enable or disable the background drones (Alt+B)",
            self.action_toggle_drones,
        )
        yield SystemCommand(
            "Queen: Analyze worker",
            "Run Queen analysis on selected worker (Alt+Q)",
            self.action_ask_queen,
        )
        yield SystemCommand(
            "Task: Assign to worker",
            "Assign selected task to selected worker (Alt+D)",
            self.action_assign_task,
        )
        yield SystemCommand(
            "Task: Complete",
            "Mark selected task as complete (Alt+F)",
            self.action_complete_task,
        )
        yield SystemCommand(
            "Task: Create",
            "Create a new task (Alt+N)",
            self.action_create_task,
        )
        yield SystemCommand(
            "Task: Fail",
            "Mark selected task as failed (Alt+L)",
            self.action_fail_task,
        )
        yield SystemCommand(
            "Task: Remove",
            "Remove selected task (Alt+P)",
            self.action_remove_task,
        )
        yield SystemCommand(
            "Web: Toggle dashboard",
            "Start or stop the web UI (Alt+W)",
            self.action_toggle_web,
        )
        yield SystemCommand(
            "Worker: Attach tmux",
            "Attach to selected worker's tmux pane (Alt+T)",
            self.action_attach,
        )
        yield SystemCommand(
            "Worker: Add to brood",
            "Spawn configured workers into the running session",
            self.action_add_worker,
        )
        yield SystemCommand(
            "Worker: Interrupt",
            "Send Ctrl-C to selected worker (Alt+I)",
            self.action_send_interrupt,
        )
        yield SystemCommand(
            "Proposals: Review",
            "Review pending Queen proposals (Alt+J)",
            self.action_review_proposals,
        )
        yield SystemCommand(
            "Queen: Decision history",
            "View resolved Queen proposals",
            self.action_decision_history,
        )

    def __init__(self, config: HiveConfig) -> None:
        from swarm.server.daemon import SwarmDaemon

        self.daemon = SwarmDaemon(config)
        self._selected_worker: Worker | None = None
        self._selected_task: SwarmTask | None = None
        self._refreshing = False
        self._last_status_text: str = ""
        super().__init__()
        self.register_theme(BEE_THEME)
        self.theme = "bee"

    # --- Property delegates to self.daemon ---

    @property
    def config(self) -> HiveConfig:
        return self.daemon.config

    @config.setter
    def config(self, value: HiveConfig) -> None:
        self.daemon.config = value

    @property
    def hive_workers(self) -> list[Worker]:
        return self.daemon.workers

    @property
    def drone_log(self) -> DroneLog:
        return self.daemon.drone_log

    @property
    def task_board(self) -> TaskBoard:
        return self.daemon.task_board

    @property
    def pilot(self) -> DronePilot | None:
        return self.daemon.pilot

    @property
    def queen(self) -> Queen:
        return self.daemon.queen

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="main-area"):
            yield WorkerListWidget(self.hive_workers, groups=self.config.groups, id="worker-list")
            yield WorkerDetailWidget(id="worker-detail")
        yield TaskPanelWidget(self.task_board, id="task-panel")
        yield DroneLogWidget(self.drone_log, id="drone-log")
        yield ProposalBannerWidget(id="proposal-banner")
        yield Static("Brood: loading... | Drones: OFF", id="status-bar")
        yield Footer()

    async def on_mount(self) -> None:
        # Kill any stale subprocess web server from a previous session
        from swarm.server.webctl import web_is_running, web_stop

        if web_is_running():
            web_stop()
            log.info("killed stale subprocess web server from previous session")

        self.query_one("#worker-list").border_title = "Workers"
        self.query_one("#worker-detail").border_title = "Detail"
        self.query_one("#task-panel").border_title = "Tasks"
        self.query_one("#drone-log").border_title = "Buzz Log"
        self.query_one("#proposal-banner").display = False

        # TUI-local on_change for refreshing the task panel widget
        self.task_board.on_change(self._on_task_board_changed_tui)

        await self.daemon.discover()

        if self.hive_workers:
            await self._sync_worker_ui()

        self.daemon.init_pilot(enabled=False)
        # Subscribe to daemon-level events (not pilot directly)
        self.daemon.on("escalation", self._on_escalation)
        self.daemon.on("workers_changed", self._on_workers_changed)
        self.daemon.on("task_assigned", self._on_task_assigned)

        self.set_interval(3, self._refresh_all)
        self._update_status_bar()

        # Focus the worker list so shortcuts work immediately
        try:
            self.query_one("#workers-lv").focus()
        except Exception:
            log.debug("could not focus worker list on mount", exc_info=True)

    async def _sync_worker_ui(self) -> None:
        """Sync the worker list widget and select the first worker."""
        wl = self.query_one("#worker-list", WorkerListWidget)
        wl.hive_workers = self.hive_workers
        wl.refresh_workers()
        if self.hive_workers:
            self._selected_worker = self.hive_workers[0]
            await self._refresh_detail()

    def _refresh_drone_log(self) -> None:
        """Pull new entries from DroneLog into the widget."""
        try:
            self.query_one("#drone-log", DroneLogWidget).refresh_entries()
        except Exception:
            log.debug("failed to refresh drone log", exc_info=True)

    def _on_workers_changed(self) -> None:
        """Called by pilot when workers are added/removed (dead pane, rediscovery)."""
        # Fix selected worker if it was removed
        if self._selected_worker and self._selected_worker not in self.hive_workers:
            self._selected_worker = self.hive_workers[0] if self.hive_workers else None
        try:
            wl = self.query_one("#worker-list", WorkerListWidget)
            wl.hive_workers = self.hive_workers
            wl.refresh_workers()
        except Exception:
            log.debug("failed to update worker list after change", exc_info=True)

    async def _refresh_all(self) -> None:
        if self._refreshing:
            return  # Previous refresh still running — skip this tick
        self._refreshing = True
        try:
            await self._do_refresh()
        finally:
            self._refreshing = False

    async def _do_refresh(self) -> None:
        reloaded = self.daemon.check_config_file()
        if reloaded:
            self.notify("Config reloaded from disk", timeout=3)
        # Only poll from here when the pilot loop isn't already polling
        if not (self.pilot and self.pilot.enabled):
            try:
                await self.daemon.poll_once()
            except Exception:
                log.warning("poll_once failed", exc_info=True)
        try:
            self.query_one("#worker-list", WorkerListWidget).refresh_workers()
        except Exception:
            log.debug("failed to refresh worker list", exc_info=True)
        try:
            await self._refresh_detail()
        except Exception:
            log.debug("failed to refresh detail pane", exc_info=True)
        self._refresh_task_panel()
        self._refresh_drone_log()
        self._refresh_proposal_banner()
        self._update_status_bar()

    def _refresh_task_panel(self) -> None:
        try:
            self.query_one("#task-panel", TaskPanelWidget).refresh_tasks()
        except Exception:
            log.debug("failed to refresh task panel", exc_info=True)

    def _on_task_board_changed_tui(self) -> None:
        # Skip if called from WUI thread — pull-based refresh handles it
        if threading.current_thread() is not threading.main_thread():
            return
        self._refresh_task_panel()

    def _on_task_assigned(self, worker: Worker, task: SwarmTask) -> None:
        self.notify(f"Queen assigned '{task.title}' → {worker.name}", timeout=8)
        self._refresh_task_panel()

    async def _refresh_detail(self) -> None:
        if self._selected_worker:
            try:
                content = await self.daemon.capture_worker_output(self._selected_worker.name)
                self.query_one("#worker-detail", WorkerDetailWidget).show_worker(
                    self._selected_worker, content
                )
                self.query_one(
                    "#worker-detail"
                ).border_title = (
                    f"{self._selected_worker.name} [{self._selected_worker.state.display}]"
                )
            except Exception:
                log.debug("failed to refresh detail pane", exc_info=True)

    def _update_status_bar(self) -> None:
        from swarm.server.webctl import web_is_running_embedded

        # Snapshot to avoid mid-iteration mutation
        workers = list(self.hive_workers)
        counts = worker_state_counts(workers)
        buzzing, resting, stung = counts["buzzing"], counts["resting"], counts["stung"]
        drones_on = self.pilot and self.pilot.enabled
        web_on = web_is_running_embedded()
        drones_tag = "[#8CB369]ON[/]" if drones_on else "[#D15D4C]OFF[/]"
        web_tag = "[#8CB369]ON[/]" if web_on else "[#D15D4C]OFF[/]"
        if not workers:
            parts = ["No brood running — use command palette to Launch brood"]
            parts.append(f"Drones: {drones_tag}")
            parts.append(f"Web: {web_tag}")
        else:
            parts = [f"Brood: {len(workers)} workers"]
            if buzzing:
                parts.append(f"{buzzing} buzzing")
            if resting:
                parts.append(f"{resting} resting")
            if stung:
                parts.append(f"{stung} stung")
            parts.append(f"Drones: {drones_tag}")
            parts.append(f"Web: {web_tag}")
        text = " | ".join(parts)
        if text == self._last_status_text:
            return
        self._last_status_text = text
        try:
            self.query_one("#status-bar", Static).update(text, layout=False)
        except Exception:
            log.debug("failed to update status bar", exc_info=True)

    def _refresh_proposal_banner(self) -> None:
        try:
            banner = self.query_one("#proposal-banner", ProposalBannerWidget)
            banner.refresh_proposals(self.daemon.proposal_store.pending)
        except Exception:
            log.debug("failed to refresh proposal banner", exc_info=True)

    # --- Proposal message handlers ---

    def on_review_proposals(self, message: ReviewProposals) -> None:
        self.action_review_proposals()

    def on_approve_all_proposals(self, message: ApproveAllProposals) -> None:
        self.run_worker(self._do_approve_all_proposals())

    def on_reject_all_proposals(self, message: RejectAllProposals) -> None:
        count = self.daemon.reject_all_proposals()
        self.notify(f"Rejected {count} proposal(s)", timeout=5)
        self._refresh_proposal_banner()

    def action_review_proposals(self) -> None:
        """Open the proposal review modal."""
        pending = self.daemon.proposal_store.pending
        if not pending:
            self.notify("No pending proposals", timeout=5)
            return
        self.push_screen(
            ProposalReviewModal(pending),
            callback=self._on_proposal_review_result,
        )

    def _on_proposal_review_result(self, result: ProposalReviewResult | None) -> None:
        if not result:
            return
        self.run_worker(self._do_proposal_actions(result))

    async def _do_proposal_actions(self, result: ProposalReviewResult) -> None:
        approved = 0
        rejected = 0
        for action in result.actions:
            if action.approved:
                ok = await self.daemon.approve_proposal(action.proposal_id)
                if ok:
                    approved += 1
            else:
                ok = self.daemon.reject_proposal(action.proposal_id)
                if ok:
                    rejected += 1
        parts = []
        if approved:
            parts.append(f"{approved} approved")
        if rejected:
            parts.append(f"{rejected} rejected")
        if parts:
            self.notify(f"Proposals: {', '.join(parts)}", timeout=5)
        self._refresh_proposal_banner()

    async def _do_approve_all_proposals(self) -> None:
        pending = self.daemon.proposal_store.pending
        count = 0
        for p in pending:
            ok = await self.daemon.approve_proposal(p.id)
            if ok:
                count += 1
        self.notify(f"Approved {count} proposal(s)", timeout=5)
        self._refresh_proposal_banner()

    def action_decision_history(self) -> None:
        """Show resolved Queen proposals."""
        history = self.daemon.proposal_store.history
        self.push_screen(DecisionHistoryModal(history))

    # --- Message handlers ---

    def on_worker_selected(self, message: WorkerSelected) -> None:
        self._selected_worker = message.worker
        self.run_worker(self._refresh_detail())

    def on_task_selected(self, message: TaskSelected) -> None:
        self._selected_task = message.task
        self.notify(
            f"Task: {message.task.title} [{message.task.status.value}]",
            timeout=5,
        )

    async def on_worker_command(self, message: WorkerCommand) -> None:
        """Handle inline input from the detail pane."""
        try:
            await self.daemon.send_to_worker(message.worker.name, message.text)
        except Exception:
            log.warning("failed to send keys to %s", message.worker.name, exc_info=True)
            self.notify(f"Failed to send to {message.worker.name}", severity="error", timeout=5)

    # --- Actions (keybindings) ---

    def action_attach(self) -> None:
        """Attach directly to the selected worker's tmux pane."""
        if not self._selected_worker:
            return
        pane_id = self._selected_worker.pane_id
        session = self.config.session_name
        with self.suspend():
            subprocess.run(["tmux", "select-pane", "-t", pane_id])
            subprocess.run(["tmux", "attach", "-t", session])

    async def action_send_escape(self) -> None:
        """Send Esc to the selected worker (interrupt in Claude Code)."""
        if self._selected_worker:
            await self.daemon.escape_worker(self._selected_worker.name)

    async def action_continue_worker(self) -> None:
        if not self._selected_worker:
            self.notify("No worker selected", timeout=5)
            return
        await self.daemon.continue_worker(self._selected_worker.name)
        self.notify(f"Continued {self._selected_worker.name}", timeout=3)

    async def action_continue_all(self) -> None:
        count = await self.daemon.continue_all()
        if not count:
            self.notify("No idle workers to continue", timeout=5)
            return
        self.notify(f"Continued {count} workers", timeout=3)

    def action_send_message(self) -> None:
        """Open the send modal for multi-line task input."""
        if not self._selected_worker:
            return
        groups = [g.name for g in self.config.groups]
        modal = SendModal(
            self._selected_worker.name,
            workers=list(self.hive_workers),
            groups=groups,
        )
        self.push_screen(modal, callback=self._on_send_result)

    def _on_send_result(self, result: SendResult | None) -> None:
        if not result:
            return
        if result.target == "__all__":
            self.run_worker(self.daemon.send_all(result.text))
        elif result.target.startswith("group:"):
            group_name = result.target[6:]
            self.run_worker(self.daemon.send_group(group_name, result.text))
        else:
            self.run_worker(self.daemon.send_to_worker(result.target, result.text))

    def action_create_task(self) -> None:
        """Open modal to create a new task."""
        modal = CreateTaskModal()
        self.push_screen(modal, callback=self._on_create_task_result)

    def _on_create_task_result(self, task: SwarmTask | None) -> None:
        if not task:
            return
        self.daemon.create_task(
            title=task.title,
            description=task.description,
            priority=task.priority,
            task_type=task.task_type,
        )
        self.notify(f"Task created: {task.title}", timeout=5)

    def action_edit_task(self) -> None:
        """Open modal to edit the selected task."""
        if not self._selected_task:
            self.notify("No task selected — select one in the Tasks panel first", timeout=5)
            return
        # Re-fetch to get latest state
        task = self.task_board.get(self._selected_task.id)
        if not task:
            self.notify("Task no longer exists", timeout=5)
            return
        modal = EditTaskModal(task)
        self.push_screen(modal, callback=self._on_edit_task_result)

    def _on_edit_task_result(self, result: TaskEditResult | None) -> None:
        if not result:
            return
        try:
            self.daemon.edit_task(
                result.task_id,
                title=result.title,
                description=result.description,
                priority=result.priority,
                tags=result.tags,
                task_type=result.task_type,
            )
            self.notify(f"Task updated: {result.title}", timeout=5)
        except SwarmOperationError as e:
            self.notify(str(e), timeout=5)

    def action_assign_task(self) -> None:
        """Assign the selected task to the selected worker."""
        if not self._selected_task:
            self.notify("No task selected — select one in the Tasks panel first", timeout=5)
            return
        if not self._selected_worker:
            self.notify("No worker selected", timeout=5)
            return
        try:
            self.daemon.assign_task(self._selected_task.id, self._selected_worker.name)
            self.notify(
                f"Assigned '{self._selected_task.title}' → {self._selected_worker.name}",
                timeout=5,
            )
        except SwarmOperationError as e:
            self.notify(str(e), timeout=5)

    def action_complete_task(self) -> None:
        """Mark the selected task as complete (Alt+F = Finish)."""
        if not self._selected_task:
            self.notify("No task selected — select one in the Tasks panel first", timeout=5)
            return
        try:
            self.daemon.complete_task(self._selected_task.id)
            self.notify(f"Task completed: {self._selected_task.title}", timeout=5)
        except SwarmOperationError as e:
            self.notify(str(e), timeout=5)

    def action_fail_task(self) -> None:
        """Mark the selected task as failed (Alt+L = Lost)."""
        if not self._selected_task:
            self.notify("No task selected — select one in the Tasks panel first", timeout=5)
            return
        try:
            self.daemon.fail_task(self._selected_task.id)
            self.notify(f"Task failed: {self._selected_task.title}", timeout=5)
        except SwarmOperationError as e:
            self.notify(str(e), timeout=5)

    def action_remove_task(self) -> None:
        """Remove the selected task (Alt+P = Purge) — asks for confirmation."""
        if not self._selected_task:
            self.notify("No task selected — select one in the Tasks panel first", timeout=5)
            return
        task = self._selected_task

        def _on_confirm(result: bool | None) -> None:
            if result:
                try:
                    self.daemon.remove_task(task.id)
                    self._selected_task = None
                    self.notify(f"Task removed: {task.title}", timeout=5)
                except SwarmOperationError as e:
                    self.notify(str(e), timeout=5)

        self.push_screen(
            ConfirmModal(f"Remove task '{task.title}'?", title="Remove Task"),
            callback=_on_confirm,
        )

    async def action_send_interrupt(self) -> None:
        """Send Ctrl-C to the selected worker (Alt+I = Interrupt)."""
        if not self._selected_worker:
            self.notify("No worker selected", timeout=5)
            return
        await self.daemon.interrupt_worker(self._selected_worker.name)
        self.notify(f"Ctrl-C sent to {self._selected_worker.name}", timeout=3)

    def action_add_worker(self) -> None:
        """Show modal to spawn configured-but-not-running workers into the brood."""
        running_names = {w.name.lower() for w in self.hive_workers}
        available = [w for w in self.config.workers if w.name.lower() not in running_names]
        if not available:
            self.notify("All configured workers are already running", timeout=5)
            return
        self.push_screen(AddWorkerModal(available), callback=self._on_add_worker_result)

    def _on_add_worker_result(self, result: list[WorkerConfig] | None) -> None:
        if not result:
            return
        self.run_worker(self._do_add_workers(result))

    async def _do_add_workers(self, workers: list[WorkerConfig]) -> None:
        """Spawn selected workers into the running session."""
        added = 0
        for wc in workers:
            try:
                await self.daemon.spawn_worker(wc)
                added += 1
            except Exception:
                log.warning("failed to add worker %s", wc.name, exc_info=True)
                self.notify(f"Failed to add {wc.name}", severity="error", timeout=5)
        if added:
            self._on_workers_changed()
            self._update_status_bar()
            self.notify(f"Added {added} worker(s) to brood", timeout=5)

    def action_kill_session(self) -> None:
        """Kill the entire tmux session (Alt+H = Halt) — asks for confirmation."""

        def _on_confirm(result: bool | None) -> None:
            if result:
                self.run_worker(self._do_kill_session())

        self.push_screen(
            ConfirmModal(
                "Kill the entire swarm session? This will terminate ALL workers.",
                title="Kill Session",
            ),
            callback=_on_confirm,
        )

    async def _do_kill_session(self) -> None:
        """Actually kill the tmux session."""
        await self.daemon.kill_session()
        self._selected_worker = None
        self._on_workers_changed()
        self._update_status_bar()
        self.notify("Session killed — all workers terminated", timeout=5)

    async def action_revive(self) -> None:
        if not self._selected_worker:
            self.notify("No worker selected", timeout=5)
            return
        try:
            await self.daemon.revive_worker(self._selected_worker.name)
            self._on_workers_changed()
            self._update_status_bar()
            self.notify(f"Reviving {self._selected_worker.name}", timeout=5)
        except SwarmOperationError as e:
            self.notify(str(e), timeout=5)

    async def action_kill_worker(self) -> None:
        if not self._selected_worker:
            self.notify("No worker selected", timeout=5)
            return
        worker = self._selected_worker
        self.notify(f"Killing {worker.name}...", timeout=3)
        try:
            await self.daemon.kill_worker(worker.name)
            self._on_workers_changed()
            self._update_status_bar()
            self.notify(f"Killed {worker.name}", timeout=5)
        except SwarmOperationError as e:
            self.notify(str(e), timeout=5)

    def _on_escalation(self, worker: Worker, reason: str) -> None:
        """Called by Drones when a worker is escalated — auto-triggers the Queen."""
        if not self.queen.can_call:
            self.notify(f"Queen on cooldown — {worker.name} escalated: {reason}", timeout=10)
            return
        self.notify(f"Drones escalated {worker.name} — calling Queen...", timeout=30)
        self.run_worker(self._queen_flow(worker))

    async def _queen_flow(self, worker: Worker) -> None:
        """Shared Queen analysis flow used by both manual Q and auto-escalation."""
        self.notify(f"Queen is analyzing {worker.name}...", timeout=30)
        try:
            analysis = await self.daemon.analyze_worker(worker.name)
        except Exception as e:
            self.clear_notifications()
            self.notify(f"Queen analysis failed: {e}", severity="error", timeout=10)
            return
        self.clear_notifications()

        def _on_queen(result: dict[str, Any] | None) -> None:
            if not result:
                return
            action = result.get("action")
            if action == "continue":
                self.run_worker(self.daemon.continue_worker(worker.name))
            elif action == "send_message":
                msg = result.get("message", "")
                if msg:
                    self.run_worker(self.daemon.send_to_worker(worker.name, msg))
            elif action == "restart":
                if worker.state == WorkerState.STUNG:
                    self.run_worker(self.daemon.revive_worker(worker.name))
                else:
                    # Worker isn't dead — send Enter to continue instead
                    self.run_worker(self.daemon.continue_worker(worker.name))

        self.push_screen(QueenModal(worker.name, analysis), callback=_on_queen)

    def action_ask_queen(self) -> None:
        if not self._selected_worker:
            return
        self.run_worker(self._queen_flow(self._selected_worker))

    def action_toggle_drones(self) -> None:
        self.daemon.toggle_drones()
        self._update_status_bar()

    def action_toggle_web(self) -> None:
        """Start or stop the web dashboard in-process (shares state with TUI)."""
        from swarm.server.webctl import (
            web_is_running_embedded,
            web_start_embedded,
            web_stop_embedded,
        )

        if web_is_running_embedded():
            _ok, msg = web_stop_embedded()
            self.notify(f"Web: {msg}", timeout=5)
        else:
            _ok, msg = web_start_embedded(self.daemon, port=self.config.port)
            self.notify(f"Web: {msg}", timeout=5)
        self._update_status_bar()

    def action_launch_hive(self) -> None:
        """Open the launch modal to select workers/groups."""
        if not self.config.workers:
            self.notify("No workers configured — open Config to add workers first", timeout=5)
            return

        self.push_screen(
            LaunchModal(self.config.workers, self.config.groups),
            callback=self._on_launch_result,
        )

    def _on_launch_result(self, result: list[WorkerConfig] | None) -> None:
        if not result:
            return
        self.run_worker(self._do_launch(result))

    async def _do_launch(self, workers: list[WorkerConfig]) -> None:
        """Actually launch selected workers in tmux."""
        self.notify(f"Launching {len(workers)} workers...", timeout=10)
        try:
            launched = await self.daemon.launch_workers(workers)
            await self._sync_worker_ui()
            self._update_status_bar()
            self.notify(f"Brood launched: {len(launched)} workers", timeout=5)
        except Exception as e:
            log.error("failed to launch hive", exc_info=True)
            self.notify(f"Launch failed: {e}", severity="error", timeout=10)

    def action_open_config(self) -> None:
        """Open the config editor modal."""
        if self.config.daemon_url:
            self.notify("Config editing via remote daemon not yet supported in TUI", timeout=5)
            return
        self.push_screen(ConfigModal(self.config), callback=self._on_config_result)

    def _on_config_result(self, result: ConfigUpdate | None) -> None:
        if not result:
            return
        self.run_worker(self._apply_config_update(result))

    async def _apply_config_update(self, result: ConfigUpdate) -> None:
        """Apply config changes: hot-reload settings, add/remove workers, save."""
        # Update config fields
        self.config.drones = result.drones
        self.config.queen = result.queen
        self.config.notifications = result.notifications
        self.config.workers = [WorkerConfig(name=w.name, path=w.path) for w in result.workers]
        self.config.groups = result.groups
        self.config.api_password = result.api_password

        # Hot-reload pilot, queen, notification bus via daemon
        await self.daemon.reload_config(self.daemon.config)

        # Remove workers
        for name in result.removed_workers:
            try:
                await self.daemon.kill_worker(name)
            except SwarmOperationError:
                log.debug("kill_worker failed for %s during config update", name, exc_info=True)

        # Add workers
        for wc in result.added_workers:
            try:
                await self.daemon.spawn_worker(wc)
            except Exception:
                log.warning("failed to add worker %s", wc.name, exc_info=True)

        # Save to disk and update mtime to prevent self-triggered reload
        self.daemon.save_config()

        self._on_workers_changed()
        self._update_status_bar()
        self.notify("Config saved and applied", timeout=5)

    async def action_quit(self) -> None:
        await self.daemon.stop()
        # Stop embedded web server if running
        from swarm.server.webctl import web_is_running_embedded, web_stop_embedded

        if web_is_running_embedded():
            web_stop_embedded()
        self.exit()
