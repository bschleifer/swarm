"""User-facing feedback capture (bugs, feature requests, questions).

Captures diagnostic context from the running swarm instance, redacts it,
and builds a pre-filled GitHub Issue URL the user can submit from their
own browser with their own GitHub account.
"""

from __future__ import annotations

from swarm.feedback.builder import FeedbackPayload, build_issue_url, build_markdown
from swarm.feedback.collector import collect_attachments
from swarm.feedback.install_id import get_install_id
from swarm.feedback.redact import redact_text

__all__ = [
    "FeedbackPayload",
    "build_issue_url",
    "build_markdown",
    "collect_attachments",
    "get_install_id",
    "redact_text",
]
