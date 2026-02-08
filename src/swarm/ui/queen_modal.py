"""Queen modal — overlay for Queen interaction with override controls."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, RichLog, Rule


class QueenModal(ModalScreen[dict | None]):
    """Modal for displaying Queen's analysis with editable overrides."""

    BINDINGS = [("escape", "dismiss(None)", "Close")]

    def __init__(self, worker_name: str, analysis: dict) -> None:
        self.worker_name = worker_name
        self.analysis = analysis
        super().__init__()

    def compose(self) -> ComposeResult:
        with Vertical(id="queen-dialog"):
            yield Label(
                f"[bold]Queen — {self.worker_name}[/bold]", id="queen-title"
            )
            yield Rule()
            yield RichLog(id="queen-log", wrap=True, markup=True)
            yield Rule()
            yield Label("[bold]Message to send:[/bold]", id="msg-label")
            yield Input(
                placeholder="Type a message to send to the worker...",
                id="queen-input",
            )
            with Horizontal(id="queen-buttons"):
                yield Button(
                    "Send Message", variant="warning", id="queen-send"
                )
                yield Button("Continue", variant="success", id="queen-continue")
                yield Button("Restart", variant="error", id="queen-restart")
                yield Button("Dismiss", variant="default", id="queen-dismiss")

    def on_mount(self) -> None:
        log = self.query_one("#queen-log", RichLog)

        if isinstance(self.analysis, dict):
            if "assessment" in self.analysis:
                log.write("[bold]Assessment[/bold]")
                log.write(f"  {self.analysis['assessment']}\n")
            if "reasoning" in self.analysis:
                log.write("[bold]Reasoning[/bold]")
                log.write(f"  {self.analysis['reasoning']}\n")
            if "error" in self.analysis:
                log.write(f"[bold red]Error:[/bold red] {self.analysis['error']}")

            # Pre-fill the input with Queen's suggested message
            action = self.analysis.get("action", "wait")
            msg = self.analysis.get("message", "")
            inp = self.query_one("#queen-input", Input)
            if msg:
                inp.value = str(msg)

            # Highlight the recommended button
            if action == "send_message":
                log.write("[bold]Recommended:[/bold] Send message")
            elif action == "continue":
                log.write("[bold]Recommended:[/bold] Continue (send Enter)")
            elif action == "restart":
                log.write("[bold]Recommended:[/bold] Restart worker")
            else:
                log.write(f"[bold]Recommended:[/bold] {action}")

            if "result" in self.analysis and "assessment" not in self.analysis:
                log.write(str(self.analysis["result"]))
        else:
            log.write(str(self.analysis))

    def on_button_pressed(self, event: Button.Pressed) -> None:
        msg = self.query_one("#queen-input", Input).value.strip()

        if event.button.id == "queen-send":
            self.dismiss({"action": "send_message", "message": msg})
        elif event.button.id == "queen-continue":
            self.dismiss({"action": "continue"})
        elif event.button.id == "queen-restart":
            self.dismiss({"action": "restart"})
        else:
            self.dismiss(None)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Enter in the input field sends the message."""
        msg = event.value.strip()
        if msg:
            self.dismiss({"action": "send_message", "message": msg})
