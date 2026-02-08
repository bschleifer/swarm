"""Bee Hive — the root Textual App for the swarm dashboard."""

from __future__ import annotations

import asyncio
import os
import subprocess
from pathlib import Path

# Ensure 24-bit truecolor so the bee theme renders correctly.
os.environ.setdefault("COLORTERM", "truecolor")

from textual.app import App, ComposeResult, SystemCommand
from textual.containers import Horizontal
from textual.theme import Theme
from textual.widgets import Footer, Header, Static

from swarm.drones.log import DroneLog
from swarm.drones.pilot import DronePilot
from swarm.config import HiveConfig, WorkerConfig, load_config
from swarm.logging import get_logger
from swarm.notify.bus import NotificationBus
from swarm.queen.context import build_hive_context
from swarm.queen.queen import Queen
from swarm.tmux.cell import capture_pane, send_enter, send_interrupt, send_keys
from swarm.tmux.hive import discover_workers, kill_session
from swarm.ui.drone_log import DroneLogWidget
from swarm.ui.keys import BINDINGS
from swarm.ui.queen_modal import QueenModal
from swarm.ui.send_modal import SendModal, SendResult
from swarm.ui.task_panel import CreateTaskModal, TaskPanelWidget, TaskSelected
from swarm.ui.worker_detail import WorkerCommand, WorkerDetailWidget
from swarm.ui.worker_list import WorkerListWidget, WorkerSelected
from swarm.tasks.board import TaskBoard
from swarm.tasks.store import FileTaskStore
from swarm.tasks.task import SwarmTask, TaskStatus
from swarm.ui.confirm_modal import ConfirmModal
from swarm.ui.config_modal import ConfigModal, ConfigUpdate
from swarm.ui.launch_modal import LaunchModal
from swarm.worker.manager import add_worker_live, kill_worker, launch_hive, revive_worker
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

    def get_system_commands(self, screen):
        yield from super().get_system_commands(screen)
        yield SystemCommand("Launch brood", "Start tmux session with configured workers", self.action_launch_hive)
        yield SystemCommand("Config", "Open the config editor (Alt+O)", self.action_open_config)
        yield SystemCommand("Toggle web dashboard", "Start or stop the web UI (Alt+W)", self.action_toggle_web)
        yield SystemCommand("Toggle drones", "Enable or disable the background drones (Alt+B)", self.action_toggle_drones)
        yield SystemCommand("Ask Queen", "Run Queen analysis on selected worker (Alt+Q)", self.action_ask_queen)
        yield SystemCommand("Create task", "Create a new task (Alt+N)", self.action_create_task)
        yield SystemCommand("Assign task", "Assign selected task to selected worker (Alt+D)", self.action_assign_task)
        yield SystemCommand("Attach tmux", "Attach to selected worker's tmux pane (Alt+T)", self.action_attach)
        yield SystemCommand("Screenshot", "Save a screenshot of the TUI (Alt+S)", self.action_save_screenshot)
        yield SystemCommand("Complete task", "Mark selected task as complete (Alt+F)", self.action_complete_task)
        yield SystemCommand("Fail task", "Mark selected task as failed (Alt+L)", self.action_fail_task)
        yield SystemCommand("Remove task", "Remove selected task (Alt+P)", self.action_remove_task)
        yield SystemCommand("Interrupt worker", "Send Ctrl-C to selected worker (Alt+I)", self.action_send_interrupt)
        yield SystemCommand("Kill session", "Kill tmux session and all workers (Alt+H)", self.action_kill_session)

    def __init__(self, config: HiveConfig) -> None:
        self.config = config
        self.hive_workers: list[Worker] = []
        # Persistence: tasks and drone log survive restarts
        task_store = FileTaskStore()
        drone_log_path = Path.home() / ".swarm" / "drone.jsonl"
        self.drone_log = DroneLog(log_file=drone_log_path)
        self.task_board = TaskBoard(store=task_store)
        self.pilot: DronePilot | None = None
        self.queen = Queen(config=config.queen, session_name=config.session_name)
        self.notification_bus = self._build_notification_bus(config)
        self._selected_worker: Worker | None = None
        self._selected_task: SwarmTask | None = None
        self._config_mtime: float = 0.0
        self._update_config_mtime()
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

    def _update_config_mtime(self) -> None:
        """Record the current config file mtime."""
        if self.config.source_path:
            try:
                self._config_mtime = Path(self.config.source_path).stat().st_mtime
            except OSError:
                pass

    def _check_config_file(self) -> None:
        """Reload config from disk if the file was modified externally."""
        if not self.config.source_path:
            return
        try:
            current_mtime = Path(self.config.source_path).stat().st_mtime
        except OSError:
            return
        if current_mtime <= self._config_mtime:
            return
        self._config_mtime = current_mtime
        try:
            new_config = load_config(self.config.source_path)
        except Exception:
            log.warning("failed to reload config from disk", exc_info=True)
            return

        # Hot-apply fields that don't require worker lifecycle changes
        self.config.groups = new_config.groups
        self.config.drones = new_config.drones
        self.config.queen = new_config.queen
        self.config.notifications = new_config.notifications
        self.config.workers = new_config.workers
        self.config.api_password = new_config.api_password

        # Hot-reload pilot
        if self.pilot:
            self.pilot.drone_config = new_config.drones
            self.pilot._base_interval = new_config.drones.poll_interval
            self.pilot._max_interval = new_config.drones.max_idle_interval
            self.pilot.interval = new_config.drones.poll_interval

        # Hot-reload queen
        self.queen.config = new_config.queen

        # Rebuild notification bus
        self.notification_bus = self._build_notification_bus(self.config)

        log.info("config reloaded from disk (external change detected)")
        self.notify("Config reloaded from disk", timeout=3)

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="main-area"):
            yield WorkerListWidget(self.hive_workers, id="worker-list")
            yield WorkerDetailWidget(id="worker-detail")
        yield TaskPanelWidget(self.task_board, id="task-panel")
        yield DroneLogWidget(id="drone-log")
        yield Static("Brood: loading... | Drones: OFF", id="status-bar")
        yield Footer()

    async def on_mount(self) -> None:
        self.query_one("#worker-list").border_title = "Workers"
        self.query_one("#worker-detail").border_title = "Detail"
        self.query_one("#task-panel").border_title = "Tasks"
        self.query_one("#drone-log").border_title = "Drone Log"

        self.task_board.on_change(self._on_task_board_changed)

        self.hive_workers = await discover_workers(self.config.session_name)

        if self.hive_workers:
            await self._sync_worker_ui()

        self.pilot = DronePilot(
            self.hive_workers,
            self.drone_log,
            self.config.watch_interval,
            session_name=self.config.session_name,
            drone_config=self.config.drones,
            task_board=self.task_board,
            queen=self.queen,
        )
        self.drone_log.on_entry(self._on_drone_entry)
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

    async def _sync_worker_ui(self) -> None:
        """Sync the worker list widget and select the first worker."""
        wl = self.query_one("#worker-list", WorkerListWidget)
        wl.hive_workers = self.hive_workers
        await wl.recompose()
        if self.hive_workers:
            self._selected_worker = self.hive_workers[0]
            await self._refresh_detail()

    def _on_drone_entry(self, entry) -> None:
        try:
            self.query_one("#drone-log", DroneLogWidget).add_entry(entry)
        except Exception:
            log.debug("failed to add drone log entry", exc_info=True)

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
        self._check_config_file()
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
        from swarm.server.webctl import web_is_running

        # Snapshot to avoid mid-iteration mutation
        workers = list(self.hive_workers)
        buzzing = sum(1 for w in workers if w.state == WorkerState.BUZZING)
        resting = sum(1 for w in workers if w.state == WorkerState.RESTING)
        stung = sum(1 for w in workers if w.state == WorkerState.STUNG)
        drones_on = self.pilot and self.pilot.enabled
        web_on = web_is_running()
        drones_tag = "[#8CB369]ON[/]" if drones_on else "[#D15D4C]OFF[/]"
        web_tag = "[#8CB369]ON[/]" if web_on else "[#D15D4C]OFF[/]"
        if not workers:
            parts = ["No brood running — use command palette to Launch brood"]
            parts.append(f"Drones: {drones_tag}")
            parts.append(f"Web: {web_tag}")
            try:
                self.query_one("#status-bar", Static).update(" | ".join(parts))
            except Exception:
                log.debug("failed to update status bar", exc_info=True)
            return
        parts = [f"Brood: {len(workers)} workers"]
        if buzzing:
            parts.append(f"{buzzing} buzzing")
        if resting:
            parts.append(f"{resting} resting")
        if stung:
            parts.append(f"{stung} stung")
        parts.append(f"Drones: {drones_tag}")
        parts.append(f"Web: {web_tag}")
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
        if not self._selected_worker:
            self.notify("No worker selected", timeout=5)
            return
        await send_enter(self._selected_worker.pane_id)
        self.notify(f"Continued {self._selected_worker.name}", timeout=3)

    async def action_continue_all(self) -> None:
        resting = [w for w in self.hive_workers if w.state == WorkerState.RESTING]
        if not resting:
            self.notify("No idle workers to continue", timeout=5)
            return
        for w in resting:
            await send_enter(w.pane_id)
        self.notify(f"Continued {len(resting)} workers", timeout=3)

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

    def action_complete_task(self) -> None:
        """Mark the selected task as complete (Alt+F = Finish)."""
        if not self._selected_task:
            self.notify("No task selected — select one in the Tasks panel first", timeout=5)
            return
        if self._selected_task.status not in (TaskStatus.ASSIGNED, TaskStatus.IN_PROGRESS):
            self.notify(
                f"Task '{self._selected_task.title}' cannot be completed ({self._selected_task.status.value})",
                timeout=5,
            )
            return
        self.task_board.complete(self._selected_task.id)
        self.notify(f"Task completed: {self._selected_task.title}", timeout=5)

    def action_fail_task(self) -> None:
        """Mark the selected task as failed (Alt+L = Lost)."""
        if not self._selected_task:
            self.notify("No task selected — select one in the Tasks panel first", timeout=5)
            return
        if self._selected_task.status not in (TaskStatus.PENDING, TaskStatus.ASSIGNED, TaskStatus.IN_PROGRESS):
            self.notify(
                f"Task '{self._selected_task.title}' cannot be failed ({self._selected_task.status.value})",
                timeout=5,
            )
            return
        self.task_board.fail(self._selected_task.id)
        self.notify(f"Task failed: {self._selected_task.title}", timeout=5)

    def action_remove_task(self) -> None:
        """Remove the selected task (Alt+P = Purge) — asks for confirmation."""
        if not self._selected_task:
            self.notify("No task selected — select one in the Tasks panel first", timeout=5)
            return
        task = self._selected_task

        def _on_confirm(result: bool | None) -> None:
            if result:
                self.task_board.remove(task.id)
                self._selected_task = None
                self.notify(f"Task removed: {task.title}", timeout=5)

        self.push_screen(
            ConfirmModal(f"Remove task '{task.title}'?", title="Remove Task"),
            callback=_on_confirm,
        )

    async def action_send_interrupt(self) -> None:
        """Send Ctrl-C to the selected worker (Alt+I = Interrupt)."""
        if not self._selected_worker:
            self.notify("No worker selected", timeout=5)
            return
        await send_interrupt(self._selected_worker.pane_id)
        self.notify(f"Ctrl-C sent to {self._selected_worker.name}", timeout=3)

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
        if self.pilot:
            self.pilot.stop()

        for w in list(self.hive_workers):
            self.task_board.unassign_worker(w.name)

        try:
            await kill_session(self.config.session_name)
        except Exception:
            log.warning("kill_session failed (session may already be gone)", exc_info=True)

        self.hive_workers.clear()
        self._selected_worker = None
        self._on_workers_changed()
        self._update_status_bar()
        self.notify("Session killed — all workers terminated", timeout=5)

    async def action_revive(self) -> None:
        if not self._selected_worker:
            self.notify("No worker selected", timeout=5)
            return
        if self._selected_worker.state != WorkerState.STUNG:
            self.notify(f"{self._selected_worker.name} is not stung", timeout=5)
            return
        await revive_worker(self._selected_worker, session_name=self.config.session_name)
        self._selected_worker.state = WorkerState.BUZZING
        self._on_workers_changed()
        self._update_status_bar()
        self.notify(f"Reviving {self._selected_worker.name}", timeout=5)

    async def action_kill_worker(self) -> None:
        if not self._selected_worker:
            self.notify("No worker selected", timeout=5)
            return
        worker = self._selected_worker
        self.notify(f"Killing {worker.name}...", timeout=3)
        await kill_worker(worker)
        worker.state = WorkerState.STUNG
        self.task_board.unassign_worker(worker.name)
        self._on_workers_changed()
        self._update_status_bar()
        self.notify(f"Killed {worker.name}", timeout=5)

    def _on_escalation(self, worker: Worker, reason: str) -> None:
        """Called by Drones when a worker is escalated — auto-triggers the Queen."""
        self.notification_bus.emit_escalation(worker.name, reason)
        if not self.queen.can_call:
            self.notify(f"Queen on cooldown — {worker.name} escalated: {reason}", timeout=10)
            return
        self.notify(f"Drones escalated {worker.name} — calling Queen...", timeout=30)
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
            drone_log=self.drone_log,
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

    def action_toggle_drones(self) -> None:
        if self.pilot:
            self.pilot.toggle()
            self._update_status_bar()

    def action_toggle_web(self) -> None:
        """Start or stop the web dashboard in the background."""
        from swarm.server.webctl import web_is_running, web_start, web_stop

        if web_is_running():
            _ok, msg = web_stop()
            self.notify(f"Web: {msg}", timeout=5)
        else:
            _ok, msg = web_start(
                session=self.config.session_name,
                config_path=self.config.source_path,
            )
            self.notify(f"Web: {msg}", timeout=5)
        self._update_status_bar()

    def action_launch_hive(self) -> None:
        """Open the launch modal to select workers/groups."""
        if self.hive_workers:
            self.notify("Brood already running — kill workers first or use Config to manage", timeout=5)
            return

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
            launched = await launch_hive(
                self.config.session_name,
                workers,
                self.config.panes_per_window,
            )
            self.hive_workers.extend(launched)
            if self.pilot:
                self.pilot.workers = self.hive_workers
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
        # Update config
        self.config.drones = result.drones
        self.config.queen = result.queen
        self.config.notifications = result.notifications
        self.config.workers = [WorkerConfig(name=w.name, path=w.path) for w in result.workers]
        self.config.groups = result.groups
        self.config.api_password = result.api_password

        # Hot-reload pilot
        if self.pilot:
            self.pilot.drone_config = result.drones
            self.pilot._base_interval = result.drones.poll_interval
            self.pilot._max_interval = result.drones.max_idle_interval
            self.pilot.interval = result.drones.poll_interval

        # Hot-reload queen
        self.queen.config = result.queen

        # Rebuild notification bus
        self.notification_bus = self._build_notification_bus(self.config)

        # Remove workers
        for name in result.removed_workers:
            worker = next((w for w in self.hive_workers if w.name.lower() == name.lower()), None)
            if worker:
                await kill_worker(worker)
                if worker in self.hive_workers:
                    self.hive_workers.remove(worker)
                self.task_board.unassign_worker(worker.name)

        # Add workers
        for wc in result.added_workers:
            try:
                await add_worker_live(
                    self.config.session_name, wc, self.hive_workers,
                    self.config.panes_per_window,
                )
            except Exception:
                log.warning("failed to add worker %s", wc.name, exc_info=True)

        # Save to disk
        from swarm.config import save_config
        save_config(self.config)
        self._update_config_mtime()  # prevent self-triggered reload

        self._on_workers_changed()
        self._update_status_bar()
        self.notify("Config saved and applied", timeout=5)

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
