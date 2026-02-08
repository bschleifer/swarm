"""Config editor modal — 5-tab TabbedContent for editing hive settings."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    DataTable,
    Input,
    Label,
    Rule,
    SelectionList,
    Static,
    Switch,
    TabbedContent,
    TabPane,
)

from textual.suggester import SuggestFromList

from swarm.config import (
    DroneConfig,
    GroupConfig,
    HiveConfig,
    NotifyConfig,
    QueenConfig,
    WorkerConfig,
    discover_projects,
)


@dataclass
class ConfigUpdate:
    """Result of the config editor modal."""

    drones: DroneConfig
    queen: QueenConfig
    notifications: NotifyConfig
    workers: list[WorkerConfig]
    groups: list[GroupConfig]
    # Top-level fields (informational — structural changes need restart)
    session_name: str = "swarm"
    projects_dir: str = "~/projects"
    log_level: str = "WARNING"
    api_password: str | None = None
    added_workers: list[WorkerConfig] = field(default_factory=list)
    removed_workers: list[str] = field(default_factory=list)


class EditGroupModal(ModalScreen[list[str] | None]):
    """Sub-modal for editing group membership — shows a checklist of all workers."""

    BINDINGS = [("escape", "dismiss(None)", "Cancel")]

    def __init__(self, group_name: str, all_workers: list[str], current_members: list[str]) -> None:
        self._group_name = group_name
        self._all_workers = all_workers
        self._current_members = {m.lower() for m in current_members}
        super().__init__()

    def compose(self) -> ComposeResult:
        with Vertical(id="edit-group-dialog"):
            yield Label(f"[bold]Edit Group: {self._group_name}[/bold]", id="edit-group-title")
            yield SelectionList[str](
                *[(w, w, w.lower() in self._current_members) for w in self._all_workers],
                id="edit-group-selection",
            )
            with Horizontal(id="edit-group-buttons"):
                yield Button("Save", variant="warning", id="edit-group-save")
                yield Button("Cancel", variant="default", id="edit-group-cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "edit-group-save":
            sel = self.query_one("#edit-group-selection", SelectionList)
            self.dismiss(list(sel.selected))
        else:
            self.dismiss(None)


class ConfigModal(ModalScreen[ConfigUpdate | None]):
    """5-tab config editor modal."""

    BINDINGS = [("escape", "dismiss(None)", "Close")]

    def __init__(self, config: HiveConfig) -> None:
        self._config = config
        # Working copies
        self._workers = list(config.workers)
        self._groups = list(config.groups)
        self._original_worker_names = {w.name.lower() for w in config.workers}
        self._added: list[WorkerConfig] = []
        self._removed: list[str] = []
        # Discover project paths for autocomplete
        self._project_paths = [
            p for _, p in discover_projects(Path(config.projects_dir).expanduser())
        ]
        super().__init__()

    def compose(self) -> ComposeResult:
        with Vertical(id="config-dialog"):
            yield Label("[bold]Config Editor[/bold]", id="config-title")
            yield Static(
                f"session: {self._config.session_name}  |  projects: {self._config.projects_dir}  "
                f"[dim](restart required to change)[/dim]",
                id="config-header-info",
            )
            yield Rule()
            with TabbedContent(id="config-tabs"):
                with TabPane("Drones", id="tab-drones"):
                    yield from self._drone_fields()
                with TabPane("Queen", id="tab-queen"):
                    yield from self._queen_fields()
                with TabPane("Notifications", id="tab-notif"):
                    yield from self._notif_fields()
                with TabPane("Workers", id="tab-workers"):
                    yield from self._worker_fields()
                with TabPane("Groups", id="tab-groups"):
                    yield from self._group_fields()
            yield Rule()
            with Horizontal(id="config-buttons"):
                yield Button("Save", variant="warning", id="config-save")
                yield Button("Cancel", variant="default", id="config-cancel")

    def _drone_fields(self) -> ComposeResult:
        bz = self._config.drones
        yield self._num_field("drone-poll_interval", "Poll interval (s)", bz.poll_interval)
        yield self._num_field(
            "drone-escalation_threshold",
            "Escalation threshold (s)",
            bz.escalation_threshold,
        )
        yield self._num_field(
            "drone-max_idle_interval",
            "Max idle interval (s)",
            bz.max_idle_interval,
        )
        yield self._num_field(
            "drone-max_revive_attempts",
            "Max revive attempts",
            bz.max_revive_attempts,
        )
        yield self._num_field("drone-max_poll_failures", "Max poll failures", bz.max_poll_failures)
        yield self._toggle_field("drone-auto_approve_yn", "Auto-approve Y/N", bz.auto_approve_yn)
        yield self._toggle_field(
            "drone-auto_stop_on_complete",
            "Auto-stop on complete",
            bz.auto_stop_on_complete,
        )

    def _queen_fields(self) -> ComposeResult:
        qn = self._config.queen
        yield self._num_field("queen-cooldown", "Cooldown (s)", qn.cooldown)
        yield self._toggle_field("queen-enabled", "Enabled", qn.enabled)

    def _notif_fields(self) -> ComposeResult:
        nt = self._config.notifications
        yield self._toggle_field("notif-terminal_bell", "Terminal bell", nt.terminal_bell)
        yield self._toggle_field("notif-desktop", "Desktop notifications", nt.desktop)
        yield self._num_field("notif-debounce_seconds", "Debounce (s)", nt.debounce_seconds)
        yield Rule()
        yield Static("[bold]Web Dashboard[/bold]", classes="config-section-label")
        yield Horizontal(
            Label("API password", classes="config-label"),
            Input(
                self._config.api_password or "",
                id="cfg-web-api_password",
                password=True,
                placeholder="Leave empty for no auth",
                classes="config-num-input",
            ),
            classes="config-row",
        )

    def _worker_fields(self) -> ComposeResult:
        yield DataTable(id="worker-table")
        yield Rule()
        yield Static("Add worker:", classes="config-add-label")
        with Horizontal(classes="config-add-row"):
            yield Input(placeholder="Name", id="add-worker-name")
            path_suggester = SuggestFromList(self._project_paths) if self._project_paths else None
            yield Input(placeholder="Path", id="add-worker-path", suggester=path_suggester)
            yield Button("Add", variant="success", id="add-worker-btn")

    def _group_fields(self) -> ComposeResult:
        yield DataTable(id="group-table")
        yield Rule()
        yield Static("Add group:", classes="config-add-label")
        with Horizontal(classes="config-add-row"):
            yield Input(placeholder="Group name", id="add-group-name")
            yield Button("Add", variant="success", id="add-group-btn")

    @staticmethod
    def _num_field(id_: str, label: str, value: float | int) -> Horizontal:
        return Horizontal(
            Label(label, classes="config-label"),
            Input(str(value), id=f"cfg-{id_}", classes="config-num-input"),
            classes="config-row",
        )

    @staticmethod
    def _toggle_field(id_: str, label: str, value: bool) -> Horizontal:
        return Horizontal(
            Label(label, classes="config-label"),
            Switch(value=value, id=f"cfg-{id_}"),
            classes="config-row",
        )

    def on_mount(self) -> None:
        self._populate_worker_table()
        self._populate_group_table()

    def _populate_worker_table(self) -> None:
        table = self.query_one("#worker-table", DataTable)
        table.clear(columns=True)
        table.add_columns("Name", "Path", "")
        for w in self._workers:
            table.add_row(w.name, w.path, "[Remove]", key=w.name)

    def _populate_group_table(self) -> None:
        table = self.query_one("#group-table", DataTable)
        table.clear(columns=True)
        table.add_columns("Name", "Workers", "")
        for g in self._groups:
            table.add_row(g.name, ", ".join(g.workers), "[Remove]", key=g.name)

    def on_data_table_cell_selected(self, event: DataTable.CellSelected) -> None:
        """Handle Remove clicks and group editing in worker/group tables."""
        table = event.data_table
        row_key = event.cell_key.row_key

        if event.value == "[Remove]":
            if table.id == "worker-table" and row_key:
                name = str(row_key.value)
                self._workers = [w for w in self._workers if w.name.lower() != name.lower()]
                if name.lower() in self._original_worker_names:
                    self._removed.append(name)
                self._added = [w for w in self._added if w.name != name]
                self._populate_worker_table()
            elif table.id == "group-table" and row_key:
                name = str(row_key.value)
                self._groups = [g for g in self._groups if g.name != name]
                self._populate_group_table()
        elif table.id == "group-table" and row_key:
            # Clicking a non-Remove cell on a group row → edit group membership
            group_name = str(row_key.value)
            group = next((g for g in self._groups if g.name == group_name), None)
            if group:
                all_worker_names = [w.name for w in self._workers]
                self.app.push_screen(
                    EditGroupModal(group_name, all_worker_names, group.workers),
                    callback=lambda result: self._on_edit_group_result(group_name, result),
                )

    def _on_edit_group_result(self, group_name: str, result: list[str] | None) -> None:
        """Update group members after EditGroupModal closes."""
        if result is None:
            return
        for g in self._groups:
            if g.name == group_name:
                g.workers = list(result)
                break
        self._populate_group_table()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "config-cancel":
            self.dismiss(None)
        elif event.button.id == "config-save":
            self._save()
        elif event.button.id == "add-worker-btn":
            self._add_worker()
        elif event.button.id == "add-group-btn":
            self._add_group()

    def _add_worker(self) -> None:
        name_input = self.query_one("#add-worker-name", Input)
        path_input = self.query_one("#add-worker-path", Input)
        name = name_input.value.strip()
        path = path_input.value.strip()

        if not name or not path:
            self.notify("Name and path are required", severity="warning")
            return

        # Strict path validation
        resolved = Path(path).expanduser().resolve()
        if not resolved.exists():
            self.notify(f"Path does not exist: {resolved}", severity="error")
            return

        # Check duplicate
        if any(w.name.lower() == name.lower() for w in self._workers):
            self.notify(f"Worker '{name}' already exists", severity="warning")
            return

        wc = WorkerConfig(name=name, path=str(resolved))
        self._workers.append(wc)
        self._added.append(wc)
        self._populate_worker_table()
        name_input.value = ""
        path_input.value = ""

    def _add_group(self) -> None:
        name_input = self.query_one("#add-group-name", Input)
        name = name_input.value.strip()
        if not name:
            self.notify("Group name is required", severity="warning")
            return
        if any(g.name.lower() == name.lower() for g in self._groups):
            self.notify(f"Group '{name}' already exists", severity="warning")
            return
        self._groups.append(GroupConfig(name=name, workers=[]))
        self._populate_group_table()
        name_input.value = ""

    def _save(self) -> None:
        """Collect all values and dismiss with ConfigUpdate."""
        try:
            drones = DroneConfig(
                poll_interval=float(self.query_one("#cfg-drone-poll_interval", Input).value),
                escalation_threshold=float(
                    self.query_one("#cfg-drone-escalation_threshold", Input).value,
                ),
                max_idle_interval=float(
                    self.query_one("#cfg-drone-max_idle_interval", Input).value,
                ),
                max_revive_attempts=int(
                    self.query_one("#cfg-drone-max_revive_attempts", Input).value,
                ),
                max_poll_failures=int(self.query_one("#cfg-drone-max_poll_failures", Input).value),
                auto_approve_yn=self.query_one("#cfg-drone-auto_approve_yn", Switch).value,
                auto_stop_on_complete=self.query_one(
                    "#cfg-drone-auto_stop_on_complete",
                    Switch,
                ).value,
            )
            queen = QueenConfig(
                cooldown=float(self.query_one("#cfg-queen-cooldown", Input).value),
                enabled=self.query_one("#cfg-queen-enabled", Switch).value,
            )
            notifications = NotifyConfig(
                terminal_bell=self.query_one("#cfg-notif-terminal_bell", Switch).value,
                desktop=self.query_one("#cfg-notif-desktop", Switch).value,
                debounce_seconds=float(self.query_one("#cfg-notif-debounce_seconds", Input).value),
            )
        except (ValueError, TypeError) as e:
            self.notify(f"Invalid value: {e}", severity="error")
            return

        raw_pw = self.query_one("#cfg-web-api_password", Input).value.strip()
        api_password = raw_pw or None

        self.dismiss(
            ConfigUpdate(
                drones=drones,
                queen=queen,
                notifications=notifications,
                workers=list(self._workers),
                groups=list(self._groups),
                session_name=self._config.session_name,
                projects_dir=self._config.projects_dir,
                log_level=self._config.log_level,
                api_password=api_password,
                added_workers=list(self._added),
                removed_workers=list(self._removed),
            )
        )
