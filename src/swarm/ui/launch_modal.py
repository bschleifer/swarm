"""Launch modal — select workers/groups to start in tmux."""

from __future__ import annotations

import re

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Label, Rule, SelectionList, Static

from swarm.config import GroupConfig, WorkerConfig


def _slugify(name: str) -> str:
    """Convert a group name to a valid Textual widget ID fragment."""
    return re.sub(r"[^a-z0-9_-]", "-", name.lower()).strip("-")


class LaunchModal(ModalScreen[list[WorkerConfig] | None]):
    """Modal for selecting which workers/groups to launch."""

    BINDINGS = [("escape", "dismiss(None)", "Close")]

    def __init__(
        self,
        workers: list[WorkerConfig],
        groups: list[GroupConfig],
    ) -> None:
        self._workers = workers
        self._groups = groups
        # Map slugified ID back to group name for button handler
        self._slug_to_group: dict[str, str] = {}
        for g in groups:
            self._slug_to_group[_slugify(g.name)] = g.name
        super().__init__()

    def compose(self) -> ComposeResult:
        with Vertical(id="launch-dialog"):
            yield Label("[bold]Launch Brood[/bold]", id="launch-title")
            yield Static(
                "Select workers or use a group preset, then launch.",
                id="launch-subtitle",
            )
            yield Rule()
            if self._groups:
                yield Static("Groups:", classes="launch-section-label")
                with Horizontal(id="launch-group-buttons"):
                    for g in self._groups:
                        yield Button(
                            f"{g.name} ({len(g.workers)})",
                            id=f"grp-{_slugify(g.name)}",
                            variant="default",
                            classes="launch-group-btn",
                        )
                yield Rule()
            yield Static("Workers:", classes="launch-section-label")
            yield SelectionList[str](
                *[
                    (f"{w.name}  [dim]{w.path}[/dim]", w.name, False)
                    for w in self._workers
                ],
                id="launch-worker-list",
            )
            yield Rule()
            with Horizontal(id="launch-buttons"):
                yield Button("Launch selected", variant="warning", id="launch-go")
                yield Button("Launch all", variant="success", id="launch-all")
                yield Button("Cancel", variant="default", id="launch-cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id or ""

        if bid == "launch-cancel":
            self.dismiss(None)
            return

        if bid == "launch-all":
            self.dismiss(list(self._workers))
            return

        if bid == "launch-go":
            sel = self.query_one("#launch-worker-list", SelectionList)
            selected_names = set(sel.selected)
            if not selected_names:
                self.notify("No workers selected", severity="warning")
                return
            chosen = [w for w in self._workers if w.name in selected_names]
            self.dismiss(chosen)
            return

        # Group preset buttons
        if bid.startswith("grp-"):
            slug = bid[4:]
            group_name = self._slug_to_group.get(slug)
            group = next((g for g in self._groups if g.name == group_name), None)
            if group:
                sel = self.query_one("#launch-worker-list", SelectionList)
                sel.deselect_all()
                for wname in group.workers:
                    try:
                        sel.select(wname)
                    except Exception:
                        pass
