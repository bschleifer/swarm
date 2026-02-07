"""Send modal — rich input overlay for sending tasks to workers."""

from __future__ import annotations

from dataclasses import dataclass

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Label, Select, TextArea

from swarm.worker.worker import Worker


@dataclass
class SendResult:
    """Result returned from the send modal."""

    text: str
    target: str  # worker name, group name, or "__all__"


class SendModal(ModalScreen[SendResult | None]):
    """Modal for sending multi-line tasks/messages to workers."""

    BINDINGS = [("escape", "dismiss(None)", "Close")]

    def __init__(
        self,
        worker_name: str,
        workers: list[Worker] | None = None,
        groups: list[str] | None = None,
    ) -> None:
        self.worker_name = worker_name
        self._workers = workers or []
        self._groups = groups or []
        super().__init__()

    def compose(self) -> ComposeResult:
        # Build target options
        options: list[tuple[str, str]] = []
        options.append((f"{self.worker_name} (selected)", self.worker_name))
        for w in self._workers:
            if w.name != self.worker_name:
                options.append((w.name, w.name))
        if self._groups:
            for g in self._groups:
                options.append((f"[group] {g}", f"group:{g}"))
        if len(self._workers) > 1:
            options.append(("All workers", "__all__"))

        with Vertical(id="send-dialog"):
            yield Label("Send task to worker", id="send-title")
            yield Select(options, value=self.worker_name, id="send-target")
            yield TextArea(id="send-input")
            with Horizontal(id="send-buttons"):
                yield Button("Send", variant="primary", id="send-btn")
                yield Button("Cancel", id="cancel-btn")

    def on_mount(self) -> None:
        ta = self.query_one("#send-input", TextArea)
        ta.focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "send-btn":
            self._submit()
        elif event.button.id == "cancel-btn":
            self.dismiss(None)

    def _submit(self) -> None:
        text = self.query_one("#send-input", TextArea).text.strip()
        if not text:
            self.dismiss(None)
            return
        target = self.query_one("#send-target", Select).value
        self.dismiss(SendResult(text=text, target=str(target)))

    def key_ctrl_enter(self) -> None:
        """Ctrl+Enter to send (since Enter inserts newlines in TextArea)."""
        self._submit()
