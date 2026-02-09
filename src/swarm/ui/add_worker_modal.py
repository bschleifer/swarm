"""Add-worker modal — spawn configured-but-not-running workers into the brood."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Label, Rule, SelectionList

from swarm.config import WorkerConfig


class AddWorkerModal(ModalScreen[list[WorkerConfig] | None]):
    """Modal for selecting configured workers to add to the running session."""

    BINDINGS = [("escape", "dismiss(None)", "Close")]

    def __init__(self, available: list[WorkerConfig]) -> None:
        self._available = available
        super().__init__()

    def compose(self) -> ComposeResult:
        with Vertical(id="add-worker-dialog"):
            yield Label("[bold]Add to Brood[/bold]", id="add-worker-title")
            yield Rule()
            yield SelectionList[str](
                *[(f"{w.name}  [dim]{w.path}[/dim]", w.name, True) for w in self._available],
                id="add-worker-list",
            )
            yield Rule()
            with Horizontal(id="add-worker-buttons"):
                yield Button("Add selected", variant="warning", id="add-go")
                yield Button("Cancel", variant="default", id="add-cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id or ""

        if bid == "add-cancel":
            self.dismiss(None)
            return

        if bid == "add-go":
            sel = self.query_one("#add-worker-list", SelectionList)
            selected_names = set(sel.selected)
            if not selected_names:
                self.notify("No workers selected", severity="warning")
                return
            chosen = [w for w in self._available if w.name in selected_names]
            self.dismiss(chosen)
