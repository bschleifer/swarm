"""Bee Hive — the root Textual App for the swarm dashboard."""

from __future__ import annotations

import asyncio
import os
import subprocess
from pathlib import Path

# Ensure 24-bit truecolor so the bee theme renders correctly.
os.environ.setdefault("COLORTERM", "truecolor")

from textual.app import App, ComposeResult
from textual.containers import Horizontal
from textual.theme import Theme
from textual.widgets import Footer, Header, Static

from swarm.buzz.log import BuzzLog
from swarm.buzz.pilot import BuzzPilot
from swarm.config import HiveConfig
from swarm.logging import get_logger
from swarm.notify.bus import NotificationBus
from swarm.queen.context import build_hive_context
from swarm.queen.queen import Queen
from swarm.tmux.cell import capture_pane, send_enter, send_keys
from swarm.tmux.hive import discover_workers
from swarm.ui.buzz_log import BuzzLogWidget
from swarm.ui.keys import BINDINGS
from swarm.ui.queen_modal import QueenModal
from swarm.ui.send_modal import SendModal, SendResult
from swarm.ui.task_panel import CreateTaskModal, TaskPanelWidget, TaskSelected
from swarm.ui.worker_detail import WorkerCommand, WorkerDetailWidget
from swarm.ui.worker_list import WorkerListWidget, WorkerSelected
from swarm.tasks.board import TaskBoard
from swarm.tasks.store import FileTaskStore
from swarm.tasks.task import SwarmTask
from swarm.worker.manager import kill_worker, revive_worker
from swarm.worker.worker import Worker, WorkerState

log = get_logger("ui.app")


BEE_THEME = Theme(
    name="bee",
    primary="#D8A03D",          # golden honey
    secondary="#A88FD9",        # lavender
    warning="#D8A03D",          # golden honey (titles/status)
    error="#D15D4C",            # poppy red
    success="#8CB369",          # muted leaf green
    accent="#D8A03D",           # golden honey
    foreground="#E6D2B5",       # creamy beeswax
    background="#2A1B0E",       # deep hive brown
    surface="#362415",          # warm brown surface
    panel="#3E2B1B",            # slightly lighter brown
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

    def __init__(self, config: HiveConfig) -> None:
        self.config = config
        self.hive_workers: list[Worker] = []
        # Persistence: tasks and buzz log survive restarts
        task_store = FileTaskStore()
        buzz_log_path = Path.home() / ".swarm" / "buzz.jsonl"
        self.buzz_log = BuzzLog(log_file=buzz_log_path)
        self.task_board = TaskBoard(store=task_store)
        self.pilot: BuzzPilot | None = None
        self.queen = Queen(config=config.queen, session_name=config.session_name)
        self.notification_bus = self._build_notification_bus(config)
        self._selected_worker: Worker | None = None
        self._selected_task: SwarmTask | None = None
        super().__init__()
        self.register_theme(BEE_THEME)
        self.theme = "bee"

    @staticmethod
    def _build_notification_bus(config: HiveConfig) -> NotificationBus:
        bus = NotificationBus(debounce_seconds=config.notifications.debounce_seconds)
        if config.notifications.terminal_bell:
            from swarm.notify.terminal import terminal_bell_backend
            bus.add_backend(terminal_bell_backend)
        if config.notifications.desktop:
            from swarm.notify.desktop import desktop_backend
            bus.add_backend(desktop_backend)
        return bus

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="main-area"):
            yield WorkerListWidget(self.hive_workers, id="worker-list")
            yield WorkerDetailWidget(id="worker-detail")
        yield TaskPanelWidget(self.task_board, id="task-panel")
        yield BuzzLogWidget(id="buzz-log")
        yield Static("Hive: loading... | Buzz: OFF", id="status-bar")
        yield Footer()

    async def on_mount(self) -> None:
        self.query_one("#worker-list").border_title = "Workers"
        self.query_one("#worker-detail").border_title = "Detail"
        self.query_one("#task-panel").border_title = "Tasks"
        self.query_one("#buzz-log").border_title = "Buzz Log"

        self.task_board.on_change(self._on_task_board_changed)

        self.hive_workers = await discover_workers(self.config.session_name)

        if not self.hive_workers:
            self.query_one("#status-bar", Static).update(
                f"No hive found for session '{self.config.session_name}' — run 'swarm launch' first"
            )
            return

        wl = self.query_one("#worker-list", WorkerListWidget)
        wl.hive_workers = self.hive_workers
        await wl.recompose()

        if self.hive_workers:
            self._selected_worker = self.hive_workers[0]
            await self._refresh_detail()

        self.pilot = BuzzPilot(
            self.hive_workers,
            self.buzz_log,
            self.config.watch_interval,
            session_name=self.config.session_name,
            buzz_config=self.config.buzz,
            task_board=self.task_board,
            queen=self.queen,
        )
        self.buzz_log.on_entry(self._on_buzz_entry)
        self.pilot.on_escalate(self._on_escalation)
        self.pilot.on_workers_changed(self._on_workers_changed)
        self.pilot.on_task_assigned(self._on_task_assigned)

        self.pilot.start()
        self.pilot.enabled = False

        self.set_interval(3, self._refresh_all)
        self._update_status_bar()

        # Focus the worker list so shortcuts work immediately
        try:
            self.query_one("#workers-lv").focus()
        except Exception:
            log.debug("could not focus worker list on mount", exc_info=True)

    def _on_buzz_entry(self, entry) -> None:
        try:
            self.query_one("#buzz-log", BuzzLogWidget).add_entry(entry)
        except Exception:
            log.debug("failed to add buzz log entry", exc_info=True)

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
        if self.pilot:
            await self.pilot.poll_once()
        try:
            self.query_one("#worker-list", WorkerListWidget).refresh_workers()
        except Exception:
            log.debug("failed to refresh worker list", exc_info=True)
        await self._refresh_detail()
        self._refresh_task_panel()
        self._update_status_bar()

    def _refresh_task_panel(self) -> None:
        try:
            self.query_one("#task-panel", TaskPanelWidget).refresh_tasks()
        except Exception:
            log.debug("failed to refresh task panel", exc_info=True)

    def _on_task_board_changed(self) -> None:
        self._refresh_task_panel()

    def _on_task_assigned(self, worker: Worker, task: SwarmTask) -> None:
        self.notify(f"Queen assigned '{task.title}' → {worker.name}", timeout=8)
        self.notification_bus.emit_task_assigned(worker.name, task.title)
        self._refresh_task_panel()

    async def _refresh_detail(self) -> None:
        if self._selected_worker:
            try:
                content = await capture_pane(self._selected_worker.pane_id)
                self.query_one("#worker-detail", WorkerDetailWidget).show_worker(
                    self._selected_worker, content
                )
                self.query_one("#worker-detail").border_title = (
                    f"{self._selected_worker.name} [{self._selected_worker.state.display}]"
                )
            except Exception:
                log.debug("failed to refresh detail pane", exc_info=True)

    def _update_status_bar(self) -> None:
        # Snapshot to avoid mid-iteration mutation
        workers = list(self.hive_workers)
        buzzing = sum(1 for w in workers if w.state == WorkerState.BUZZING)
        resting = sum(1 for w in workers if w.state == WorkerState.RESTING)
        stung = sum(1 for w in workers if w.state == WorkerState.STUNG)
        buzz_state = "ON" if (self.pilot and self.pilot.enabled) else "OFF"
        parts = [f"Hive: {len(workers)} workers"]
        if buzzing:
            parts.append(f"{buzzing} buzzing")
        if resting:
            parts.append(f"{resting} resting")
        if stung:
            parts.append(f"{stung} stung")
        parts.append(f"Buzz: {buzz_state}")
        try:
            self.query_one("#status-bar", Static).update(" | ".join(parts))
        except Exception:
            log.debug("failed to update status bar", exc_info=True)

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
            await send_keys(message.worker.pane_id, message.text)
        except Exception:
            log.warning("failed to send keys to %s", message.worker.name, exc_info=True)

    # --- Actions (keybindings) ---

    def action_attach(self) -> None:
        """Attach directly to the selected worker's tmux pane."""
        if not self._selected_worker:
            return
        pane_id = self._selected_worker.pane_id
        session = self.config.session_name
        with self.suspend():
            subprocess.run(["tmux", "attach", "-t", session, ";", "select-pane", "-t", pane_id])

    async def action_send_escape(self) -> None:
        """Send Esc to the selected worker (interrupt in Claude Code)."""
        if self._selected_worker:
            from swarm.tmux.cell import send_escape
            await send_escape(self._selected_worker.pane_id)

    async def action_continue_worker(self) -> None:
        if self._selected_worker:
            await send_enter(self._selected_worker.pane_id)

    async def action_continue_all(self) -> None:
        for w in list(self.hive_workers):
            if w.state == WorkerState.RESTING:
                await send_enter(w.pane_id)

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
        targets: list[Worker] = []
        if result.target == "__all__":
            targets = list(self.hive_workers)
        elif result.target.startswith("group:"):
            group_name = result.target[6:]
            try:
                group_workers = self.config.get_group(group_name)
                group_names = {w.name.lower() for w in group_workers}
                targets = [w for w in self.hive_workers if w.name.lower() in group_names]
            except ValueError:
                targets = []
        else:
            targets = [w for w in self.hive_workers if w.name == result.target]

        for w in targets:
            self.run_worker(send_keys(w.pane_id, result.text))

    def action_create_task(self) -> None:
        """Open modal to create a new task."""
        worker_names = [w.name for w in self.hive_workers]
        modal = CreateTaskModal(workers=worker_names)
        self.push_screen(modal, callback=self._on_create_task_result)

    def _on_create_task_result(self, task: SwarmTask | None) -> None:
        if not task:
            return
        self.task_board.add(task)
        self.notify(f"Task created: {task.title}", timeout=5)

    def action_assign_task(self) -> None:
        """Assign the selected task to the selected worker."""
        if not self._selected_task:
            self.notify("No task selected — select one in the Tasks panel first", timeout=5)
            return
        if not self._selected_worker:
            self.notify("No worker selected", timeout=5)
            return
        if not self._selected_task.is_available:
            self.notify(
                f"Task '{self._selected_task.title}' is not available ({self._selected_task.status.value})",
                timeout=5,
            )
            return
        self.task_board.assign(self._selected_task.id, self._selected_worker.name)
        self.notify(
            f"Assigned '{self._selected_task.title}' → {self._selected_worker.name}",
            timeout=5,
        )

    async def action_revive(self) -> None:
        if self._selected_worker and self._selected_worker.state == WorkerState.STUNG:
            await revive_worker(self._selected_worker)

    async def action_kill_worker(self) -> None:
        if not self._selected_worker:
            return
        worker = self._selected_worker
        await kill_worker(worker)
        if worker in self.hive_workers:
            self.hive_workers.remove(worker)
        self._selected_worker = self.hive_workers[0] if self.hive_workers else None
        self._on_workers_changed()

    def _on_escalation(self, worker: Worker, reason: str) -> None:
        """Called by Buzz when a worker is escalated — auto-triggers the Queen."""
        self.notification_bus.emit_escalation(worker.name, reason)
        if not self.queen.can_call:
            self.notify(f"Queen on cooldown — {worker.name} escalated: {reason}", timeout=10)
            return
        self.notify(f"Buzz escalated {worker.name} — calling Queen...", timeout=30)
        self.run_worker(self._queen_flow(worker))

    async def _queen_flow(self, worker: Worker) -> None:
        """Shared Queen analysis flow used by both manual Q and auto-escalation."""
        self.notify(f"Queen is analyzing {worker.name}...", timeout=30)
        content = await capture_pane(worker.pane_id)

        # Build hive-wide context so Queen sees all workers
        worker_outputs: dict[str, str] = {}
        for w in list(self.hive_workers):
            try:
                worker_outputs[w.name] = await capture_pane(w.pane_id, lines=20)
            except Exception:
                log.debug("failed to capture pane for %s in queen flow", w.name)
        hive_ctx = build_hive_context(
            list(self.hive_workers),
            worker_outputs=worker_outputs,
            buzz_log=self.buzz_log,
            task_board=self.task_board,
        )

        analysis = await self.queen.analyze_worker(worker.name, content, hive_context=hive_ctx)
        self.clear_notifications()

        def _on_queen(result: dict | None) -> None:
            if not result:
                return
            action = result.get("action")
            if action == "continue":
                self.run_worker(send_enter(worker.pane_id))
            elif action == "send_message":
                msg = result.get("message", "")
                if msg:
                    self.run_worker(send_keys(worker.pane_id, msg))
            elif action == "restart":
                self.run_worker(revive_worker(worker))

        self.push_screen(QueenModal(worker.name, analysis), callback=_on_queen)

    def action_ask_queen(self) -> None:
        if not self._selected_worker:
            return
        self.run_worker(self._queen_flow(self._selected_worker))

    def action_toggle_buzz(self) -> None:
        if self.pilot:
            self.pilot.toggle()
            self._update_status_bar()

    def action_save_screenshot(self) -> None:
        path = self.save_screenshot()
        if path:
            self.notify(f"Screenshot saved: {path}", timeout=5)
        else:
            self.notify("Failed to save screenshot", timeout=5)

    def action_quit(self) -> None:
        if self.pilot:
            self.pilot.stop()
        self.exit()
