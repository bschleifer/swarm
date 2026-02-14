"""Proposal banner — persistent 1-line banner for pending Queen proposals."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.message import Message
from textual.widget import Widget
from textual.widgets import Button, Label

from swarm.tasks.proposal import AssignmentProposal


class ReviewProposals(Message):
    """Fired when user clicks [Review]."""


class ApproveAllProposals(Message):
    """Fired when user clicks [Approve All]."""


class RejectAllProposals(Message):
    """Fired when user clicks [Reject All]."""


class ProposalBannerWidget(Widget):
    """1-line persistent banner showing pending proposal count + actions."""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._count = 0

    def compose(self) -> ComposeResult:
        with Horizontal(id="proposal-banner-row"):
            yield Label("", id="proposal-banner-text")
            yield Button("Review", variant="warning", id="proposal-review-btn")
            yield Button("Approve All", variant="success", id="proposal-approve-all-btn")
            yield Button("Reject All", variant="error", id="proposal-reject-all-btn")

    def refresh_proposals(self, proposals: list[AssignmentProposal]) -> None:
        """Update the banner with current pending proposals."""
        count = len(proposals)
        if count == self._count and count == 0:
            return
        self._count = count
        if count == 0:
            self.display = False
            return
        self.display = True
        # Type breakdown
        types: dict[str, int] = {}
        for p in proposals:
            types[p.proposal_type] = types.get(p.proposal_type, 0) + 1
        parts = [f"{v} {k}" for k, v in sorted(types.items())]
        plural = "s" if count != 1 else ""
        breakdown = ", ".join(parts)
        text = f" [bold #D8A03D]{count} pending proposal{plural}[/] ({breakdown})"
        self.query_one("#proposal-banner-text", Label).update(text, layout=False)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "proposal-review-btn":
            self.post_message(ReviewProposals())
        elif event.button.id == "proposal-approve-all-btn":
            self.post_message(ApproveAllProposals())
        elif event.button.id == "proposal-reject-all-btn":
            self.post_message(RejectAllProposals())
