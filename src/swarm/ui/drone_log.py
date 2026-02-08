"""Drone Log widget — shows background drones activity feed."""

from __future__ import annotations

from rich.text import Text
from textual.app import ComposeResult
from textual.widget import Widget
from textual.widgets import RichLog

from swarm.drones.log import DroneAction, DroneEntry


ACTION_STYLES = {
    DroneAction.CONTINUED: "#D8A03D",
    DroneAction.REVIVED: "#A88FD9",
    DroneAction.ESCALATED: "#D15D4C bold",
}


class DroneLogWidget(Widget):
    def compose(self) -> ComposeResult:
        yield RichLog(id="drone-rich-log", wrap=True, markup=True)

    def add_entry(self, entry: DroneEntry) -> None:
        log = self.query_one("#drone-rich-log", RichLog)
        style = ACTION_STYLES.get(entry.action, "")
        text = Text()
        text.append(entry.formatted_time + " ", style="dim")
        text.append(entry.action.value + " ", style=style)
        text.append(entry.worker_name + " ", style="bold")
        if entry.detail:
            text.append(f"({entry.detail})", style="dim")
        log.write(text)
