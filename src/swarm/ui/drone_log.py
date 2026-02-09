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
        self._shown_count = 0

    def compose(self) -> ComposeResult:
        yield RichLog(id="drone-rich-log", wrap=True, markup=True)

    def on_mount(self) -> None:
        self.refresh_entries()

    def refresh_entries(self) -> None:
        """Sync widget display with the DroneLog data source (pull-based)."""
        if not self._drone_log:
            return
        entries = self._drone_log.entries
        # Detect clear: shown_count beyond actual entries → reset widget
        if self._shown_count > len(entries):
            self.query_one("#drone-rich-log", RichLog).clear()
            self._shown_count = 0
        new_entries = entries[self._shown_count :]
        for entry in new_entries:
            self._write_entry(entry)
        self._shown_count = len(entries)

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
