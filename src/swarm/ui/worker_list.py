"""Worker list sidebar — shows all workers with state indicators, grouped by config."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.message import Message
from textual.widget import Widget
from textual.widgets import Label, ListItem, ListView

from swarm.config import GroupConfig
from swarm.worker.worker import Worker, WorkerState


STATE_ICONS = {
    WorkerState.BUZZING: (".", "state-working"),
    WorkerState.WAITING: ("?", "state-waiting"),
    WorkerState.RESTING: ("~", "state-resting"),
    WorkerState.STUNG: ("!", "state-stung"),
}

# Worst-state ordering for group header indicators
_STATE_SEVERITY = {
    WorkerState.STUNG: 3,
    WorkerState.WAITING: 2,
    WorkerState.RESTING: 1,
    WorkerState.BUZZING: 0,
}


class WorkerSelected(Message):
    def __init__(self, worker: Worker) -> None:
        self.worker = worker
        super().__init__()


class GroupHeaderItem(ListItem):
    """Non-selectable group header with worst-state indicator."""

    def __init__(self, group_name: str, workers: list[Worker]) -> None:
        self._group_name = group_name
        self._workers = workers
        super().__init__(id=f"group-{group_name}")

    def compose(self) -> ComposeResult:
        worst = self._worst_state()
        icon, css_class = STATE_ICONS.get(worst, (".", ""))
        yield Label(
            f" [{icon}] {self._group_name}",
            classes=f"group-header {css_class}",
        )

    def _worst_state(self) -> WorkerState:
        if not self._workers:
            return WorkerState.RESTING
        return max(
            (w.state for w in self._workers),
            key=lambda s: _STATE_SEVERITY.get(s, 0),
        )

    def refresh_state(self) -> None:
        worst = self._worst_state()
        icon, css_class = STATE_ICONS.get(worst, (".", ""))
        label = self.query_one(Label)
        label.update(f" [{icon}] {self._group_name}", layout=False)
        for cls in ("state-working", "state-waiting", "state-resting", "state-stung"):
            label.remove_class(cls)
        label.add_class("group-header")
        if css_class:
            label.add_class(css_class)


class WorkerListItem(ListItem):
    def __init__(self, worker: Worker, indent: bool = False) -> None:
        self.worker_ref = worker
        self._last_state: WorkerState | None = None
        self._indent = indent
        super().__init__()

    def compose(self) -> ComposeResult:
        icon, css_class = STATE_ICONS.get(self.worker_ref.state, ("?", ""))
        self._last_state = self.worker_ref.state
        prefix = "   " if self._indent else " "
        yield Label(f"{prefix}{icon} {self.worker_ref.name}", classes=css_class)

    def refresh_state(self) -> None:
        if self.worker_ref.state == self._last_state:
            return  # No change — skip update to avoid layout invalidation
        self._last_state = self.worker_ref.state
        icon, css_class = STATE_ICONS.get(self.worker_ref.state, ("?", ""))
        label = self.query_one(Label)
        prefix = "   " if self._indent else " "
        label.update(f"{prefix}{icon} {self.worker_ref.name}", layout=False)
        for cls in ("state-working", "state-waiting", "state-resting", "state-stung"):
            label.remove_class(cls)
        if css_class:
            label.add_class(css_class)


class WorkerListWidget(Widget):
    def __init__(
        self, items: list[Worker], groups: list[GroupConfig] | None = None, **kwargs
    ) -> None:
        self.hive_workers = items
        self.groups = groups or []
        super().__init__(**kwargs)

    def compose(self) -> ComposeResult:
        yield ListView(id="workers-lv")

    def on_mount(self) -> None:
        self._rebuild_list()

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        item = event.item
        if isinstance(item, WorkerListItem):
            self.post_message(WorkerSelected(item.worker_ref))
        # Skip GroupHeaderItem — not selectable

    def _worker_by_name(self, name: str) -> Worker | None:
        name_lower = name.lower()
        for w in self.hive_workers:
            if w.name.lower() == name_lower:
                return w
        return None

    def _rebuild_list(self) -> None:
        lv = self.query_one("#workers-lv", ListView)
        lv.clear()

        if not self.groups:
            # Flat list (no groups configured)
            for w in self.hive_workers:
                lv.append(WorkerListItem(w))
            return

        grouped_names: set[str] = set()
        for group in self.groups:
            members = []
            for wname in group.workers:
                worker = self._worker_by_name(wname)
                if worker:
                    members.append(worker)
                    grouped_names.add(worker.name.lower())
            if members:
                lv.append(GroupHeaderItem(group.name, members))
                for w in members:
                    lv.append(WorkerListItem(w, indent=True))

        # Ungrouped workers at bottom
        ungrouped = [w for w in self.hive_workers if w.name.lower() not in grouped_names]
        if ungrouped:
            for w in ungrouped:
                lv.append(WorkerListItem(w))

    def refresh_workers(self) -> None:
        lv = self.query_one("#workers-lv", ListView)
        existing_workers = list(lv.query(WorkerListItem))
        existing_names = [item.worker_ref.name for item in existing_workers]
        current_names = [w.name for w in self.hive_workers]

        if existing_names != current_names:
            # Worker set changed — full rebuild
            self._rebuild_list()
        else:
            # Same workers — just update state icons
            for item in existing_workers:
                item.refresh_state()
            # Also update group headers
            for header in lv.query(GroupHeaderItem):
                header.refresh_state()

    @property
    def selected_worker(self) -> Worker | None:
        if self.hive_workers:
            lv = self.query_one("#workers-lv", ListView)
            idx = lv.index or 0
            if 0 <= idx < len(self.hive_workers):
                return self.hive_workers[idx]
        return None
