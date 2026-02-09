"""Drone Log widget — shows background drones activity feed."""

from __future__ import annotations

from rich.text import Text
from textual.app import ComposeResult
from textual.widget import Widget
from textual.widgets import RichLog

from swarm.drones.log import DroneAction, DroneEntry, DroneLog


ACTION_STYLES = {
    DroneAction.CONTINUED: "#D8A03D",
    DroneAction.REVIVED: "#A88FD9",
    DroneAction.ESCALATED: "#D15D4C bold",
}


class DroneLogWidget(Widget):
    def __init__(self, drone_log: DroneLog | None = None, **kwargs) -> None:
        super().__init__(**kwargs)
        self._drone_log = drone_log

    def compose(self) -> ComposeResult:
        yield RichLog(id="drone-rich-log", wrap=True, markup=True)

    def on_mount(self) -> None:
        # Render existing entries (history from file)
        if self._drone_log:
            entries = self._drone_log.entries
            for entry in entries:
                self._write_entry(entry)
            # Subscribe to new entries and clear events — fully push-based
            self._drone_log.on_entry(self._on_new_entry)
            self._drone_log.on("clear", self._on_log_cleared)

    def _on_new_entry(self, entry: DroneEntry) -> None:
        """Called by DroneLog when a new entry is added."""
        self._write_entry(entry)

    def _on_log_cleared(self) -> None:
        """Called by DroneLog when clear() is invoked (e.g. kill_session)."""
        self.query_one("#drone-rich-log", RichLog).clear()

    def refresh_entries(self) -> None:
        """No-op — push-based callbacks handle everything now."""

    def _write_entry(self, entry: DroneEntry) -> None:
        log = self.query_one("#drone-rich-log", RichLog)
        style = ACTION_STYLES.get(entry.action, "")
        text = Text()
        text.append(entry.formatted_time + " ", style="dim")
        text.append(entry.action.value + " ", style=style)
        text.append(entry.worker_name + " ", style="bold")
        if entry.detail:
            text.append(f"({entry.detail})", style="dim")
        log.write(text)
