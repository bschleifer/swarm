"""Buzz Log widget — shows auto-pilot activity feed."""

from __future__ import annotations

from rich.text import Text
from textual.app import ComposeResult
from textual.widget import Widget
from textual.widgets import RichLog

from swarm.buzz.log import BuzzAction, BuzzEntry


ACTION_STYLES = {
    BuzzAction.CONTINUED: "#D8A03D",
    BuzzAction.REVIVED: "#A88FD9",
    BuzzAction.ESCALATED: "#D15D4C bold",
}


class BuzzLogWidget(Widget):
    def compose(self) -> ComposeResult:
        yield RichLog(id="buzz-rich-log", wrap=True, markup=True)

    def add_entry(self, entry: BuzzEntry) -> None:
        log = self.query_one("#buzz-rich-log", RichLog)
        style = ACTION_STYLES.get(entry.action, "")
        text = Text()
        text.append(entry.formatted_time + " ", style="dim")
        text.append(entry.action.value + " ", style=style)
        text.append(entry.worker_name + " ", style="bold")
        if entry.detail:
            text.append(f"({entry.detail})", style="dim")
        log.write(text)
