"""Phase 3: store.consolidate_into + PlaybookConsolidator sweep."""

from __future__ import annotations

import pytest

from swarm.db.core import SwarmDB
from swarm.db.playbook_store import PlaybookStore
from swarm.playbooks.consolidator import PlaybookConsolidator
from swarm.playbooks.models import (
    Playbook,
    PlaybookStatus,
    content_hash,
    project_scope,
)


@pytest.fixture
def store(tmp_path):
    return PlaybookStore(SwarmDB(tmp_path / "swarm.db"))


def _active(store, name, body, **kw):
    return store.create(
        Playbook(name=name, title=f"{name} t", trigger=f"{name} when", body=body,
                 status=PlaybookStatus.ACTIVE, **kw)
    )


class _Queen:
    def __init__(self, verdict, raises=False):
        self.verdict, self.raises, self.calls = verdict, raises, 0

    async def ask(self, prompt, **kw):
        self.calls += 1
        if self.raises:
            raise RuntimeError("boom")
        return self.verdict


# --- store.consolidate_into --------------------------------------------


def test_consolidate_into_merges_and_retires_loser(store):
    _active(store, "a", "run check then commit", provenance_task_ids=["t-1"])
    _active(store, "b", "run check then commit then push", provenance_task_ids=["t-2"])

    ok = store.consolidate_into(
        "a", "b", body="run check, commit, push", trigger="ship flow", reason="merged"
    )
    assert ok is True
    keep = store.get("a")
    assert keep.body == "run check, commit, push"
    assert keep.version == 2  # bumped
    assert keep.content_hash == content_hash("run check, commit, push")
    assert set(keep.provenance_task_ids) == {"t-1", "t-2"}
    loser = store.get("b")
    assert loser.status == PlaybookStatus.RETIRED and loser.retired_reason == "merged"
    # FTS reflects the rewritten body.
    assert "a" in [p.name for p in store.search("push", status=PlaybookStatus.ACTIVE)]


def test_consolidate_into_guards(store):
    _active(store, "a", "x body")
    assert store.consolidate_into("a", "a", body="y", trigger="t", reason="r") is False
    assert store.consolidate_into("a", "missing", body="y", trigger="t", reason="r") is False
    store.create(Playbook(name="ret", body="z", status=PlaybookStatus.RETIRED))
    assert store.consolidate_into("a", "ret", body="y", trigger="t", reason="r") is False


# --- PlaybookConsolidator.consolidate_once -----------------------------


async def test_sweep_merges_near_duplicates(store):
    _active(store, "a", "wrap sender in retry with exponential backoff and dead letter")
    _active(store, "b", "wrap the sender in retry, exponential backoff, then dead letter")
    q = _Queen({"merge": True, "keep": "A", "title": "Retry", "trigger": "5xx",
                "body": "1. retry 2. backoff 3. dead-letter"})

    merges = await PlaybookConsolidator(queen=q, store=store).consolidate_once()

    assert merges == 1
    # Order-independent: exactly one survives ACTIVE with the merged body,
    # the other is retired. (list() is newest-first, so which name is the
    # "keep" depends on iteration order — not part of the contract.)
    a, b = store.get("a"), store.get("b")
    statuses = {a.status, b.status}
    assert statuses == {PlaybookStatus.ACTIVE, PlaybookStatus.RETIRED}
    survivor = a if a.status == PlaybookStatus.ACTIVE else b
    assert survivor.body == "1. retry 2. backoff 3. dead-letter"
    assert survivor.version == 2


async def test_sweep_no_merge_when_queen_declines(store):
    _active(store, "a", "wrap sender in retry with exponential backoff dead letter")
    _active(store, "b", "wrap the sender in retry exponential backoff dead letter again")
    c = PlaybookConsolidator(queen=_Queen({"merge": False}), store=store)
    assert await c.consolidate_once() == 0
    assert store.get("a").status == PlaybookStatus.ACTIVE
    assert store.get("b").status == PlaybookStatus.ACTIVE


async def test_sweep_never_crosses_scope(store):
    _active(store, "a", "wrap sender in retry exponential backoff dead letter")
    _active(store, "b", "wrap the sender in retry exponential backoff dead letter",
            scope=project_scope("hub"))
    # Queen would say merge, but find_near_duplicate is scope-bound so the
    # pair is never even surfaced.
    q = _Queen({"merge": True, "keep": "A", "body": "merged", "trigger": "t"})
    assert await PlaybookConsolidator(queen=q, store=store).consolidate_once() == 0
    assert q.calls == 0
    assert store.get("b").status == PlaybookStatus.ACTIVE


async def test_sweep_queen_error_is_safe(store):
    _active(store, "a", "wrap sender in retry exponential backoff dead letter")
    _active(store, "b", "wrap the sender in retry exponential backoff dead letter")
    c = PlaybookConsolidator(queen=_Queen(None, raises=True), store=store)
    assert await c.consolidate_once() == 0
    assert store.get("a").status == PlaybookStatus.ACTIVE


async def test_sweep_respects_max_merges(store):
    # Three well-separated near-dup pairs so find_near_duplicate matches
    # strictly within a pair (not the all-identical pathology).
    _active(store, "retry1", "wrap the outbound sender in retry with exponential backoff")
    _active(store, "retry2", "wrap outbound sender in retry, exponential backoff please")
    _active(store, "tunnel1", "configure cloudflared tunnel and verify the reverse proxy")
    _active(store, "tunnel2", "configure the cloudflared tunnel then verify reverse proxy")
    _active(store, "flake1", "isolate the flaky pytest and check selector timeouts")
    _active(store, "flake2", "isolate flaky pytest, then check the selector timeouts")
    q = _Queen({"merge": True, "keep": "A", "body": "m", "trigger": "t"})
    merges = await PlaybookConsolidator(queen=q, store=store).consolidate_once(max_merges=2)
    assert merges == 2  # capped — third pair left for the next sweep
