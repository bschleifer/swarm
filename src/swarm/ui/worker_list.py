"""Worker list sidebar — shows all workers with state indicators."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.message import Message
from textual.widget import Widget
from textual.widgets import Label, ListItem, ListView

from swarm.worker.worker import Worker, WorkerState


STATE_ICONS = {
    WorkerState.BUZZING: (".", "state-working"),
    WorkerState.WAITING: ("?", "state-waiting"),
    WorkerState.RESTING: ("~", "state-resting"),
    WorkerState.STUNG: ("!", "state-stung"),
}


class WorkerSelected(Message):
    def __init__(self, worker: Worker) -> None:
        self.worker = worker
        super().__init__()


class WorkerListItem(ListItem):
    def __init__(self, worker: Worker) -> None:
        self.worker_ref = worker
        self._last_state: WorkerState | None = None
        super().__init__()

    def compose(self) -> ComposeResult:
        icon, css_class = STATE_ICONS.get(self.worker_ref.state, ("?", ""))
        self._last_state = self.worker_ref.state
        yield Label(f" {icon} {self.worker_ref.name}", classes=css_class)

    def refresh_state(self) -> None:
        if self.worker_ref.state == self._last_state:
            return  # No change — skip update to avoid layout invalidation
        self._last_state = self.worker_ref.state
        icon, css_class = STATE_ICONS.get(self.worker_ref.state, ("?", ""))
        label = self.query_one(Label)
        label.update(f" {icon} {self.worker_ref.name}", layout=False)
        for cls in ("state-working", "state-waiting", "state-resting", "state-stung"):
            label.remove_class(cls)
        if css_class:
            label.add_class(css_class)


class WorkerListWidget(Widget):
    def __init__(self, items: list[Worker], **kwargs) -> None:
        self.hive_workers = items
        super().__init__(**kwargs)

    def compose(self) -> ComposeResult:
        list_items = [WorkerListItem(w) for w in self.hive_workers]
        yield ListView(*list_items, id="workers-lv")

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        item = event.item
        if isinstance(item, WorkerListItem):
            self.post_message(WorkerSelected(item.worker_ref))

    def refresh_workers(self) -> None:
        lv = self.query_one("#workers-lv", ListView)
        existing = list(lv.query(WorkerListItem))
        existing_names = [item.worker_ref.name for item in existing]
        current_names = [w.name for w in self.hive_workers]

        if existing_names != current_names:
            # Worker set changed — rebuild the list
            lv.clear()
            for w in self.hive_workers:
                lv.append(WorkerListItem(w))
        else:
            # Same workers — just update state icons
            for item in existing:
                item.refresh_state()

    @property
    def selected_worker(self) -> Worker | None:
        if self.hive_workers:
            lv = self.query_one("#workers-lv", ListView)
            idx = lv.index or 0
            if 0 <= idx < len(self.hive_workers):
                return self.hive_workers[idx]
        return None
