"""Playbook domain model.

A *playbook* is a generalizable procedure synthesized from a successful
task: a trigger (when to reach for it) plus a body (steps + pitfalls),
scored by real outcomes over time.
"""

from __future__ import annotations

import hashlib
import re
import time
from dataclasses import dataclass, field
from enum import Enum

# Scope is a parameterized string, not a closed enum: ``global`` plus
# ``project:<repo>`` / ``worker:<name>`` variants. Helpers below keep
# construction/checks consistent without a brittle enum.
SCOPE_GLOBAL = "global"


def project_scope(repo: str) -> str:
    return f"project:{repo}"


def worker_scope(name: str) -> str:
    return f"worker:{name}"


class PlaybookStatus(str, Enum):
    """Lifecycle: candidate (unvetted) → active (propagated) → retired."""

    CANDIDATE = "candidate"
    ACTIVE = "active"
    RETIRED = "retired"


_WS_RE = re.compile(r"\s+")


def normalize_body(body: str) -> str:
    """Canonical form for dedupe — case/whitespace-insensitive."""
    return _WS_RE.sub(" ", (body or "").strip().lower())


def content_hash(body: str) -> str:
    """Stable hash of the normalized body — the exact-duplicate key."""
    return hashlib.sha256(normalize_body(body).encode("utf-8")).hexdigest()


@dataclass
class Playbook:
    """One row of the ``playbooks`` table."""

    name: str
    title: str = ""
    scope: str = SCOPE_GLOBAL
    trigger: str = ""
    body: str = ""
    provenance_task_ids: list[str] = field(default_factory=list)
    source_worker: str = ""
    confidence: float = 0.0
    uses: int = 0
    wins: int = 0
    losses: int = 0
    status: PlaybookStatus = PlaybookStatus.CANDIDATE
    version: int = 1
    content_hash: str = ""
    id: str = ""
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    last_used_at: float | None = None
    retired_reason: str = ""

    def __post_init__(self) -> None:
        if not self.content_hash:
            self.content_hash = content_hash(self.body)

    @property
    def winrate(self) -> float:
        """Wins / decided outcomes. 0.0 when nothing decided yet."""
        decided = self.wins + self.losses
        return self.wins / decided if decided else 0.0

    def to_api(self) -> dict[str, object]:
        return {
            "id": self.id,
            "name": self.name,
            "title": self.title,
            "scope": self.scope,
            "trigger": self.trigger,
            "body": self.body,
            "provenance_task_ids": list(self.provenance_task_ids),
            "source_worker": self.source_worker,
            "confidence": self.confidence,
            "uses": self.uses,
            "wins": self.wins,
            "losses": self.losses,
            "winrate": round(self.winrate, 3),
            "status": self.status.value,
            "version": self.version,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "last_used_at": self.last_used_at,
            "retired_reason": self.retired_reason,
        }
