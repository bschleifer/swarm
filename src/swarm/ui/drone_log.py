"""Drone Log widget — shows background drones activity feed."""

from __future__ import annotations

import threading

from rich.text import Text
from textual.app import ComposeResult
from textual.widget import Widget
from textual.widgets import RichLog

from swarm.drones.log import DroneLog, SystemAction, SystemEntry


ACTION_STYLES = {
    SystemAction.CONTINUED: "#D8A03D",
    SystemAction.REVIVED: "#A88FD9",
    SystemAction.ESCALATED: "#D15D4C bold",
}


class DroneLogWidget(Widget):
    def __init__(self, drone_log: DroneLog | None = None, **kwargs) -> None:
        super().__init__(**kwargs)
        self._drone_log = drone_log
        self._rendered_count = 0

    def compose(self) -> ComposeResult:
        yield RichLog(id="drone-rich-log", wrap=True, markup=True)

    def on_mount(self) -> None:
        if self._drone_log:
            entries = self._drone_log.entries
            for entry in entries:
                self._write_entry(entry)
            self._rendered_count = len(entries)
            self._drone_log.on_entry(self._on_new_entry)
            self._drone_log.on("clear", self._on_log_cleared)

    def _on_new_entry(self, entry: SystemEntry) -> None:
        """Called by DroneLog when a new entry is added."""
        if threading.current_thread() is not threading.main_thread():
            return  # Pull-based refresh_entries will catch it on next tick
        self._write_entry(entry)
        self._rendered_count += 1

    def _on_log_cleared(self) -> None:
        """Called by DroneLog when clear() is invoked (e.g. kill_session)."""
        if threading.current_thread() is not threading.main_thread():
            return  # Pull-based refresh_entries will catch it on next tick
        self.query_one("#drone-rich-log", RichLog).clear()
        self._rendered_count = 0

    def refresh_entries(self) -> None:
        """Pull-based catch-up — called every 3s by timer.

        Handles entries added from non-main thread (push skipped)
        and log clears from non-main thread (push skipped).
        """
        if not self._drone_log:
            return
        entries = self._drone_log.entries
        n = len(entries)
        if n == 0 and self._rendered_count > 0:
            # Actual clear (entries empty)
            self.query_one("#drone-rich-log", RichLog).clear()
            self._rendered_count = 0
        elif n < self._rendered_count:
            # Buffer truncation — just clamp counter, content already in RichLog
            self._rendered_count = n
        elif n > self._rendered_count:
            # New entries missed by push
            for entry in entries[self._rendered_count :]:
                self._write_entry(entry)
            self._rendered_count = n

    def _write_entry(self, entry: SystemEntry) -> None:
        log = self.query_one("#drone-rich-log", RichLog)
        style = ACTION_STYLES.get(entry.action, "")
        text = Text()
        text.append(entry.formatted_time + " ", style="dim")
        text.append(entry.action.value + " ", style=style)
        text.append(entry.worker_name + " ", style="bold")
        if entry.detail:
            text.append(f"({entry.detail})", style="dim")
        log.write(text)
