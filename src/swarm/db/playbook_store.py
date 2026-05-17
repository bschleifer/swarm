"""PlaybookStore — persistence for synthesized procedural memory.

Backs the v10 ``playbooks`` / ``playbook_events`` tables. Distinct from
``SkillsStore`` (the v5 slash-command registry) — see
``docs/specs/playbook-synthesis-loop.md``.

FTS is *optional acceleration*: a ``playbooks_fts`` virtual table is
created at init when the SQLite build has fts5, and kept in sync by this
store's own writes. When fts5 is unavailable, ``search`` /
``find_near_duplicate`` fall back to ``LIKE`` so the feature degrades
rather than breaks.
"""

from __future__ import annotations

import re
import sqlite3
import time
import uuid
from typing import TYPE_CHECKING

from swarm.db.base_store import BaseStore
from swarm.logging import get_logger
from swarm.playbooks.models import Playbook, PlaybookStatus

if TYPE_CHECKING:
    from swarm.db.core import SwarmDB

_log = get_logger("db.playbook_store")

_WORD_RE = re.compile(r"[A-Za-z0-9_]+")


def _fts_query(text: str) -> str:
    """Turn arbitrary text into a safe fts5 OR-query of quoted tokens."""
    tokens = _WORD_RE.findall(text or "")
    return " OR ".join(f'"{t}"' for t in tokens)


class PlaybookStore(BaseStore):
    """CRUD + FTS search + exact-dup rejection for ``playbooks``."""

    def __init__(self, db: SwarmDB) -> None:
        self._db = db
        self._fts = self._ensure_fts()

    # -- FTS bootstrap -------------------------------------------------

    def _ensure_fts(self) -> bool:
        try:
            self._db.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS playbooks_fts "
                "USING fts5(name UNINDEXED, title, trigger, body)"
            )
            self._db.commit()
            return True
        except sqlite3.OperationalError:
            _log.info("fts5 unavailable — PlaybookStore falls back to LIKE search")
            return False
        except Exception:
            _log.warning("playbooks_fts init failed — using LIKE search", exc_info=True)
            return False

    def _fts_upsert(self, pb: Playbook) -> None:
        if not self._fts:
            return
        try:
            self._db.execute("DELETE FROM playbooks_fts WHERE name = ?", (pb.name,))
            self._db.execute(
                "INSERT INTO playbooks_fts (name, title, trigger, body) VALUES (?, ?, ?, ?)",
                (pb.name, pb.title, pb.trigger, pb.body),
            )
        except Exception:
            _log.debug("playbooks_fts upsert failed for %s", pb.name, exc_info=True)

    # -- writes --------------------------------------------------------

    def create(self, pb: Playbook) -> Playbook:
        """Insert a playbook, or fold an exact duplicate into the existing.

        Exact dup = same ``content_hash``. Rather than a second row we
        append provenance + bump ``uses`` on the incumbent and return it
        (Hermes-style "reject duplicate memory"). The caller can tell it
        was a dup because the returned ``id`` is not ``pb.id``.
        """
        existing = self._db.fetchone(
            "SELECT * FROM playbooks WHERE content_hash = ?", (pb.content_hash,)
        )
        if existing is not None:
            incumbent = _row_to_pb(existing)
            merged = sorted(set(incumbent.provenance_task_ids) | set(pb.provenance_task_ids))
            self._db.execute(
                "UPDATE playbooks SET provenance_task_ids = ?, uses = uses + 1, "
                "updated_at = ? WHERE id = ?",
                (self._json(merged), time.time(), incumbent.id),
            )
            self._db.commit()
            self.record_event(
                incumbent.id,
                "synthesized",
                worker=pb.source_worker,
                detail="exact-duplicate folded",
            )
            refreshed = self._db.fetchone("SELECT * FROM playbooks WHERE id = ?", (incumbent.id,))
            return _row_to_pb(refreshed)

        pb.id = pb.id or uuid.uuid4().hex
        now = time.time()
        pb.created_at = pb.created_at or now
        pb.updated_at = now
        self._db.execute(
            """
            INSERT INTO playbooks
              (id, name, title, scope, trigger, body, provenance_task_ids,
               source_worker, confidence, uses, wins, losses, status, version,
               content_hash, created_at, updated_at, last_used_at, retired_reason)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                pb.id,
                pb.name,
                pb.title,
                pb.scope,
                pb.trigger,
                pb.body,
                self._json(pb.provenance_task_ids),
                pb.source_worker,
                pb.confidence,
                pb.uses,
                pb.wins,
                pb.losses,
                pb.status.value,
                pb.version,
                pb.content_hash,
                pb.created_at,
                pb.updated_at,
                pb.last_used_at,
                pb.retired_reason,
            ),
        )
        self._fts_upsert(pb)
        self._db.commit()
        self.record_event(pb.id, "synthesized", worker=pb.source_worker)
        return pb

    def record_event(
        self,
        playbook_id: str,
        event: str,
        *,
        task_id: str = "",
        worker: str = "",
        detail: str = "",
    ) -> None:
        self._db.execute(
            "INSERT INTO playbook_events (playbook_id, task_id, worker, event, ts, detail) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (playbook_id, task_id, worker, event, time.time(), detail),
        )
        self._db.commit()

    # -- Phase 2: applied tracking + outcomes + lifecycle --------------

    def mark_applied(self, playbook_id: str, *, task_id: str, worker: str) -> None:
        """Record that a playbook was injected into a task's dispatch.

        Bumps ``uses`` + ``last_used_at`` and writes an ``applied`` event
        so outcome attribution can later find which playbooks a task
        used.
        """
        now = time.time()
        self._db.execute(
            "UPDATE playbooks SET uses = uses + 1, last_used_at = ?, updated_at = ? WHERE id = ?",
            (now, now, playbook_id),
        )
        self._db.commit()
        self.record_event(playbook_id, "applied", task_id=task_id, worker=worker)

    def playbooks_applied_to_task(self, task_id: str) -> list[str]:
        rows = self._db.fetchall(
            "SELECT DISTINCT playbook_id FROM playbook_events "
            "WHERE event = 'applied' AND task_id = ?",
            (task_id,),
        )
        return [r["playbook_id"] for r in rows]

    def record_outcome(self, playbook_id: str, win: bool, *, task_id: str = "") -> None:
        col = "wins" if win else "losses"
        self._db.execute(
            f"UPDATE playbooks SET {col} = {col} + 1, updated_at = ? WHERE id = ?",
            (time.time(), playbook_id),
        )
        self._db.commit()
        self.record_event(playbook_id, "win" if win else "loss", task_id=task_id)

    def promote(self, name: str) -> bool:
        """candidate → active. Returns False if missing or already active."""
        pb = self.get(name)
        if pb is None or pb.status == PlaybookStatus.ACTIVE:
            return False
        self._db.execute(
            "UPDATE playbooks SET status = ?, updated_at = ? WHERE name = ?",
            (PlaybookStatus.ACTIVE.value, time.time(), name),
        )
        self._db.commit()
        self.record_event(pb.id, "promoted")
        return True

    def retire(self, name: str, reason: str) -> bool:
        """→ retired with reason. Returns False if missing or already retired."""
        pb = self.get(name)
        if pb is None or pb.status == PlaybookStatus.RETIRED:
            return False
        self._db.execute(
            "UPDATE playbooks SET status = ?, retired_reason = ?, updated_at = ? WHERE name = ?",
            (PlaybookStatus.RETIRED.value, reason, time.time(), name),
        )
        self._db.commit()
        self.record_event(pb.id, "retired", detail=reason)
        return True

    def evaluate_lifecycle(
        self,
        name: str,
        *,
        promote_uses: int,
        promote_winrate: float,
        prune_uses: int,
        prune_winrate: float,
    ) -> str | None:
        """Apply auto-promote / prune rules to one playbook.

        Config-free by design — the daemon passes thresholds from
        ``PlaybookConfig`` so the store has no config dependency.
        Returns ``"promoted"`` / ``"retired"`` / ``None``. Never prunes
        on a 0.0 winrate that simply reflects no decided outcomes yet.
        """
        pb = self.get(name)
        if pb is None:
            return None
        if (
            pb.status == PlaybookStatus.CANDIDATE
            and pb.uses >= promote_uses
            and pb.winrate >= promote_winrate
        ):
            return "promoted" if self.promote(name) else None
        decided = pb.wins + pb.losses
        if (
            pb.status != PlaybookStatus.RETIRED
            and pb.uses >= prune_uses
            and decided > 0
            and pb.winrate < prune_winrate
        ):
            return "retired" if self.retire(name, "auto-pruned: low win rate") else None
        return None

    # -- reads ---------------------------------------------------------

    def get(self, name: str) -> Playbook | None:
        row = self._db.fetchone("SELECT * FROM playbooks WHERE name = ?", (name,))
        return _row_to_pb(row) if row else None

    def get_by_id(self, pb_id: str) -> Playbook | None:
        row = self._db.fetchone("SELECT * FROM playbooks WHERE id = ?", (pb_id,))
        return _row_to_pb(row) if row else None

    def list(
        self,
        *,
        scope: str | None = None,
        status: PlaybookStatus | None = None,
        limit: int = 200,
    ) -> list[Playbook]:
        sql = "SELECT * FROM playbooks"
        clauses: list[str] = []
        params: list[object] = []
        if scope is not None:
            clauses.append("scope = ?")
            params.append(scope)
        if status is not None:
            clauses.append("status = ?")
            params.append(status.value)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY updated_at DESC LIMIT ?"
        params.append(limit)
        return [_row_to_pb(r) for r in self._db.fetchall(sql, tuple(params))]

    def search(
        self,
        query: str,
        *,
        scope: str | None = None,
        status: PlaybookStatus | None = PlaybookStatus.ACTIVE,
        limit: int = 10,
    ) -> list[Playbook]:
        """Rank playbooks by relevance to *query* (fts5, LIKE fallback)."""
        rows: list[sqlite3.Row] = []
        if self._fts and (fq := _fts_query(query)):
            try:
                rows = self._db.fetchall(
                    "SELECT p.* FROM playbooks_fts f JOIN playbooks p ON p.name = f.name "
                    "WHERE playbooks_fts MATCH ? ORDER BY rank LIMIT ?",
                    (fq, limit * 4),
                )
            except sqlite3.OperationalError:
                rows = []
        if not rows:
            like = f"%{query.strip()}%"
            rows = self._db.fetchall(
                "SELECT * FROM playbooks WHERE title LIKE ? OR trigger LIKE ? OR body LIKE ? "
                "ORDER BY updated_at DESC LIMIT ?",
                (like, like, like, limit * 4),
            )
        out: list[Playbook] = []
        for r in rows:
            pb = _row_to_pb(r)
            if scope is not None and pb.scope != scope:
                continue
            if status is not None and pb.status != status:
                continue
            out.append(pb)
            if len(out) >= limit:
                break
        return out

    def find_near_duplicate(
        self, body: str, *, scope: str | None = None, exclude_name: str | None = None
    ) -> Playbook | None:
        """Best existing playbook that overlaps *body* — consolidation hint.

        Phase 1 uses it only as a signal for the synthesizer to prefer
        updating an incumbent over creating a near-twin; the merge logic
        itself lands in Phase 2/3.
        """
        for pb in self.search(body, scope=scope, status=None, limit=1):
            if exclude_name and pb.name == exclude_name:
                continue
            return pb
        return None

    # -- helpers -------------------------------------------------------

    @staticmethod
    def _json(value: object) -> str:
        import json

        return json.dumps(value)


def _row_to_pb(row: sqlite3.Row) -> Playbook:
    return Playbook(
        id=row["id"],
        name=row["name"],
        title=row["title"] or "",
        scope=row["scope"] or "global",
        trigger=row["trigger"] or "",
        body=row["body"] or "",
        provenance_task_ids=BaseStore._parse_json_field(row["provenance_task_ids"], []),
        source_worker=row["source_worker"] or "",
        confidence=float(row["confidence"] or 0.0),
        uses=int(row["uses"] or 0),
        wins=int(row["wins"] or 0),
        losses=int(row["losses"] or 0),
        status=PlaybookStatus(row["status"] or "candidate"),
        version=int(row["version"] or 1),
        content_hash=row["content_hash"] or "",
        created_at=float(row["created_at"] or time.time()),
        updated_at=float(row["updated_at"] or time.time()),
        last_used_at=row["last_used_at"],
        retired_reason=row["retired_reason"] or "",
    )
