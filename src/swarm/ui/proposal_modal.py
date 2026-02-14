"""Proposal review modal — lists pending proposals with per-item approve/reject."""

from __future__ import annotations

from dataclasses import dataclass

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Label, RichLog, Rule

from swarm.tasks.proposal import AssignmentProposal

_TYPE_BADGE = {
    "assignment": "[bold #A88FD9]ASSIGN[/]",
    "escalation": "[bold #D15D4C]ESCALATE[/]",
    "completion": "[bold #8CB369]COMPLETE[/]",
}


@dataclass
class ProposalAction:
    """Result of reviewing a single proposal."""

    proposal_id: str
    approved: bool


@dataclass
class ProposalReviewResult:
    """Batch result from the review modal."""

    actions: list[ProposalAction]


class ProposalReviewModal(ModalScreen[ProposalReviewResult | None]):
    """Modal listing pending proposals with per-item approve/reject."""

    BINDINGS = [("escape", "dismiss(None)", "Close")]

    def __init__(self, proposals: list[AssignmentProposal]) -> None:
        self._proposals = list(proposals)
        self._decisions: dict[str, bool] = {}  # proposal_id → approved
        super().__init__()

    def compose(self) -> ComposeResult:
        with Vertical(id="proposal-review-dialog"):
            yield Label(
                f"[bold]Review Proposals[/] ({len(self._proposals)} pending)",
                id="proposal-review-title",
            )
            yield Rule()
            yield RichLog(id="proposal-review-log", wrap=True, markup=True)
            yield Rule()
            with Horizontal(id="proposal-review-buttons"):
                yield Button("Done", variant="warning", id="proposal-done-btn")
                yield Button("Approve All", variant="success", id="proposal-approve-all-btn")
                yield Button("Reject All", variant="error", id="proposal-reject-all-btn")
                yield Button("Cancel", variant="default", id="proposal-cancel-btn")

    def on_mount(self) -> None:
        log = self.query_one("#proposal-review-log", RichLog)
        if not self._proposals:
            log.write("[dim]No pending proposals[/dim]")
            return
        for i, p in enumerate(self._proposals):
            badge = _TYPE_BADGE.get(p.proposal_type, f"[bold]{p.proposal_type.upper()}[/]")
            conf = f"[dim]confidence: {p.confidence:.0%}[/dim]"
            log.write(f"\n{'=' * 60}")
            log.write(f"  #{i + 1}  {badge}  [bold]{p.worker_name}[/bold]  {conf}")
            if p.task_title:
                log.write(f"  Task: {p.task_title}")
            if p.assessment:
                excerpt = p.assessment[:200] + ("..." if len(p.assessment) > 200 else "")
                log.write(f"  Assessment: [dim]{excerpt}[/dim]")
            if p.reasoning:
                excerpt = p.reasoning[:200] + ("..." if len(p.reasoning) > 200 else "")
                log.write(f"  Reasoning: [dim]{excerpt}[/dim]")
            if p.message:
                log.write(f"  Message: [italic]{p.message[:100]}[/italic]")
            status = self._decisions.get(p.id)
            if status is True:
                log.write("  [bold #8CB369]>>> APPROVED[/]")
            elif status is False:
                log.write("  [bold #D15D4C]>>> REJECTED[/]")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "proposal-cancel-btn":
            self.dismiss(None)
        elif event.button.id == "proposal-approve-all-btn":
            actions = [ProposalAction(p.id, True) for p in self._proposals]
            self.dismiss(ProposalReviewResult(actions=actions))
        elif event.button.id == "proposal-reject-all-btn":
            actions = [ProposalAction(p.id, False) for p in self._proposals]
            self.dismiss(ProposalReviewResult(actions=actions))
        elif event.button.id == "proposal-done-btn":
            # Return whatever individual decisions were made
            if self._decisions:
                actions = [
                    ProposalAction(pid, approved) for pid, approved in self._decisions.items()
                ]
                self.dismiss(ProposalReviewResult(actions=actions))
            else:
                # No individual decisions — approve all by default
                actions = [ProposalAction(p.id, True) for p in self._proposals]
                self.dismiss(ProposalReviewResult(actions=actions))
