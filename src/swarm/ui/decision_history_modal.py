"""Decision History modal — shows resolved Queen proposals."""

from __future__ import annotations

import time

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Label, RichLog, Rule

from swarm.tasks.proposal import AssignmentProposal, ProposalStatus

_STATUS_STYLE = {
    ProposalStatus.APPROVED: "[bold #8CB369]APPROVED[/]",
    ProposalStatus.REJECTED: "[bold #D15D4C]REJECTED[/]",
    ProposalStatus.EXPIRED: "[dim]EXPIRED[/]",
    ProposalStatus.PENDING: "[bold #D8A03D]PENDING[/]",
}

_TYPE_BADGE = {
    "assignment": "[bold #A88FD9]ASSIGN[/]",
    "escalation": "[bold #D15D4C]ESCALATE[/]",
    "completion": "[bold #8CB369]COMPLETE[/]",
}


class DecisionHistoryModal(ModalScreen[None]):
    """Modal showing resolved Queen proposals (history)."""

    BINDINGS = [("escape", "dismiss(None)", "Close")]

    def __init__(self, history: list[AssignmentProposal]) -> None:
        self._history = list(history)
        super().__init__()

    def compose(self) -> ComposeResult:
        with Vertical(id="decision-history-dialog"):
            yield Label(
                f"[bold]Queen Decision History[/] ({len(self._history)} entries)",
                id="decision-history-title",
            )
            yield Rule()
            yield RichLog(id="decision-history-log", wrap=True, markup=True)
            yield Rule()
            yield Button("Close", variant="default", id="decision-history-close")

    def on_mount(self) -> None:
        log = self.query_one("#decision-history-log", RichLog)
        if not self._history:
            log.write("[dim]No decision history yet[/dim]")
            return
        for p in self._history:
            badge = _TYPE_BADGE.get(p.proposal_type, f"[bold]{p.proposal_type.upper()}[/]")
            status = _STATUS_STYLE.get(p.status, str(p.status.value))
            age = time.strftime("%I:%M:%S %p", time.localtime(p.created_at))
            log.write(
                f"  {age}  {badge}  {status}  [bold]{p.worker_name}[/bold]  conf={p.confidence:.0%}"
            )
            if p.task_title:
                log.write(f"    Task: {p.task_title}")
            if p.reasoning:
                excerpt = p.reasoning[:150] + ("..." if len(p.reasoning) > 150 else "")
                log.write(f"    [dim]{excerpt}[/dim]")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(None)
