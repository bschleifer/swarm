"""Worker detail pane — live captured pane output + inline input."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.message import Message
from textual.widget import Widget
from textual.widgets import Input, RichLog, Static

from swarm.worker.worker import Worker, WorkerState


def _format_duration(seconds: float) -> str:
    """Format a duration in human-readable form."""
    if seconds < 60:
        return f"{seconds:.0f}s"
    if seconds < 3600:
        return f"{seconds / 60:.0f}m"
    return f"{seconds / 3600:.1f}h"


class WorkerCommand(Message):
    """Fired when user submits a command to the current worker."""

    def __init__(self, worker: Worker, text: str) -> None:
        self.worker = worker
        self.text = text
        super().__init__()


class WorkerDetailWidget(Widget):
    def __init__(self, **kwargs) -> None:
        self._current_worker: Worker | None = None
        super().__init__(**kwargs)

    def compose(self) -> ComposeResult:
        yield Static("", id="detail-header")
        yield RichLog(id="detail-log", wrap=True, markup=False)
        yield Input(placeholder="Type a message to this worker...", id="worker-input")

    def show_worker(self, worker: Worker, content: str) -> None:
        self._current_worker = worker
        # Update metadata header
        header = self.query_one("#detail-header", Static)
        state_dur = _format_duration(worker.state_duration)
        parts = [
            f"[bold]{worker.name}[/bold]",
            f"[dim]{worker.path}[/dim]",
            f"{worker.state.display} ({state_dur})",
        ]
        if worker.revive_count > 0:
            parts.append(f"revives: {worker.revive_count}")
        header.update("  |  ".join(parts))

        log = self.query_one("#detail-log", RichLog)
        log.clear()
        log.write(content)

    def update_content(self, content: str) -> None:
        log = self.query_one("#detail-log", RichLog)
        log.clear()
        log.write(content)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if self._current_worker and event.value.strip():
            self.post_message(WorkerCommand(self._current_worker, event.value))
            event.input.value = ""
        # Return focus to the worker list so shortcuts work again
        try:
            self.app.query_one("#workers-lv").focus()
        except Exception:
            pass

    @property
    def current_worker(self) -> Worker | None:
        return self._current_worker
