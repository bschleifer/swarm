"""Buzz Log widget — unified activity feed with category filtering."""

from __future__ import annotations

import threading

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.widget import Widget
from textual.widgets import Checkbox, Input, RichLog

from swarm.drones.log import DroneLog, SystemAction, SystemEntry

# Complete action style map covering all SystemAction values
ACTION_STYLES: dict[SystemAction, str] = {
    # Drone actions
    SystemAction.CONTINUED: "#D8A03D",
    SystemAction.REVIVED: "#A88FD9",
    SystemAction.ESCALATED: "#D15D4C bold",
    SystemAction.OPERATOR: "#8CB369",
    SystemAction.APPROVED: "#8CB369 bold",
    SystemAction.REJECTED: "#D8A03D",
    # Task events
    SystemAction.TASK_CREATED: "#A88FD9",
    SystemAction.TASK_ASSIGNED: "#A88FD9 bold",
    SystemAction.TASK_COMPLETED: "#8CB369 bold",
    SystemAction.TASK_FAILED: "#D15D4C bold",
    SystemAction.TASK_REMOVED: "#B0A08A",
    SystemAction.TASK_SEND_FAILED: "#D15D4C",
    # Queen events
    SystemAction.QUEEN_PROPOSAL: "#D8A03D bold",
    SystemAction.QUEEN_AUTO_ACTED: "#D8A03D",
    SystemAction.QUEEN_ESCALATION: "#D15D4C bold",
    SystemAction.QUEEN_COMPLETION: "#8CB369",
    # Worker events
    SystemAction.WORKER_STUNG: "#D15D4C bold",
    # System events
    SystemAction.DRAFT_OK: "#8CB369",
    SystemAction.DRAFT_FAILED: "#D15D4C",
    SystemAction.CONFIG_CHANGED: "#B0A08A",
}

_CATEGORY_LABELS = {
    "all": "All",
    "drone": "Drone",
    "task": "Task",
    "queen": "Queen",
    "worker": "Worker",
    "system": "System",
}


class DroneLogWidget(Widget):
    """Unified activity feed with category filters and text search."""

    def __init__(self, drone_log: DroneLog | None = None, **kwargs) -> None:
        super().__init__(**kwargs)
        self._drone_log = drone_log
        self._rendered_count = 0
        self._active_category: str = "all"
        self._search_text: str = ""
        self._all_entries: list[SystemEntry] = []

    def compose(self) -> ComposeResult:
        with Horizontal(id="buzz-filter-bar"):
            for key, label in _CATEGORY_LABELS.items():
                yield Checkbox(label, id=f"buzz-cat-{key}", value=(key == "all"))
            yield Input(placeholder="Search...", id="buzz-search-input")
        yield RichLog(id="drone-rich-log", wrap=True, markup=True)

    def on_mount(self) -> None:
        if self._drone_log:
            entries = self._drone_log.entries
            self._all_entries = list(entries)
            for entry in entries:
                if self._matches_filter(entry):
                    self._write_entry(entry)
            self._rendered_count = len(entries)
            self._drone_log.on_entry(self._on_new_entry)
            self._drone_log.on("clear", self._on_log_cleared)

    def on_checkbox_changed(self, event: Checkbox.Changed) -> None:
        btn = event.checkbox
        if not btn.id or not btn.id.startswith("buzz-cat-"):
            return
        cat = btn.id.removeprefix("buzz-cat-")
        if event.value:
            # Turn off other category buttons
            self._active_category = cat
            for key in _CATEGORY_LABELS:
                if key != cat:
                    try:
                        other = self.query_one(f"#buzz-cat-{key}", Checkbox)
                        if other.value:
                            other.value = False
                    except Exception:
                        pass
            self._rerender()
        else:
            # If deselecting, revert to "all"
            if cat == self._active_category:
                self._active_category = "all"
                try:
                    self.query_one("#buzz-cat-all", Checkbox).value = True
                except Exception:
                    pass
                self._rerender()

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "buzz-search-input":
            self._search_text = event.value.strip().lower()
            self._rerender()

    def _matches_filter(self, entry: SystemEntry) -> bool:
        if self._active_category != "all":
            if entry.category.value != self._active_category:
                return False
        if self._search_text:
            searchable = (f"{entry.action.value} {entry.worker_name} {entry.detail}").lower()
            if self._search_text not in searchable:
                return False
        return True

    def _rerender(self) -> None:
        """Re-render the log with current filters."""
        log = self.query_one("#drone-rich-log", RichLog)
        log.clear()
        for entry in self._all_entries:
            if self._matches_filter(entry):
                self._write_entry(entry)

    def _on_new_entry(self, entry: SystemEntry) -> None:
        """Called by DroneLog when a new entry is added."""
        if threading.current_thread() is not threading.main_thread():
            return
        self._all_entries.append(entry)
        self._rendered_count += 1
        if self._matches_filter(entry):
            self._write_entry(entry)

    def _on_log_cleared(self) -> None:
        """Called by DroneLog when clear() is invoked."""
        if threading.current_thread() is not threading.main_thread():
            return
        self.query_one("#drone-rich-log", RichLog).clear()
        self._all_entries.clear()
        self._rendered_count = 0

    def refresh_entries(self) -> None:
        """Pull-based catch-up — called every 3s by timer."""
        if not self._drone_log:
            return
        entries = self._drone_log.entries
        n = len(entries)
        if n == 0 and self._rendered_count > 0:
            self.query_one("#drone-rich-log", RichLog).clear()
            self._all_entries.clear()
            self._rendered_count = 0
        elif n < self._rendered_count:
            self._rendered_count = n
            self._all_entries = list(entries)
            self._rerender()
        elif n > self._rendered_count:
            new = entries[self._rendered_count :]
            self._all_entries.extend(new)
            for entry in new:
                if self._matches_filter(entry):
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
