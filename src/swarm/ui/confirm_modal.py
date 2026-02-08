"""Reusable yes/no confirmation dialog."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Label


class ConfirmModal(ModalScreen[bool]):
    """Simple yes/no confirmation modal. Returns True on confirm, None on cancel."""

    BINDINGS = [("escape", "dismiss(None)", "Cancel")]

    def __init__(self, prompt: str, title: str = "Confirm") -> None:
        self._prompt = prompt
        self._title = title
        super().__init__()

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm-dialog"):
            yield Label(f"[bold]{self._title}[/bold]", id="confirm-title")
            yield Label(self._prompt, id="confirm-prompt")
            with Horizontal(id="confirm-buttons"):
                yield Button("Yes", variant="warning", id="confirm-yes")
                yield Button("No", variant="default", id="confirm-no")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "confirm-yes":
            self.dismiss(True)
        else:
            self.dismiss(None)
