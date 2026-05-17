"""PlaybookStore Phase 2: applied tracking, outcomes, promote/retire, lifecycle."""

from __future__ import annotations

import pytest

from swarm.db.core import SwarmDB
from swarm.db.playbook_store import PlaybookStore
from swarm.playbooks.models import Playbook, PlaybookStatus


@pytest.fixture
def store(tmp_path):
    return PlaybookStore(SwarmDB(tmp_path / "swarm.db"))


def _pb(store, name, **kw):
    return store.create(Playbook(name=name, body=f"body for {name}", **kw))


def test_mark_applied_bumps_uses_and_records_event(store):
    pb = _pb(store, "p1", status=PlaybookStatus.ACTIVE)
    store.mark_applied(pb.id, task_id="t-9", worker="swarm")
    store.mark_applied(pb.id, task_id="t-9", worker="swarm")
    got = store.get("p1")
    assert got.uses == 2
    assert got.last_used_at is not None
    assert store.playbooks_applied_to_task("t-9") == [pb.id]
    assert store.playbooks_applied_to_task("nope") == []


def test_record_outcome_updates_winrate(store):
    pb = _pb(store, "p1", status=PlaybookStatus.ACTIVE)
    store.record_outcome(pb.id, True, task_id="t-1")
    store.record_outcome(pb.id, True, task_id="t-2")
    store.record_outcome(pb.id, False, task_id="t-3")
    got = store.get("p1")
    assert (got.wins, got.losses) == (2, 1)
    assert abs(got.winrate - 2 / 3) < 1e-9


def test_promote_and_retire(store):
    _pb(store, "p1")  # candidate by default
    assert store.promote("p1") is True
    assert store.get("p1").status == PlaybookStatus.ACTIVE
    assert store.promote("p1") is False  # already active → no-op

    assert store.retire("p1", "superseded") is True
    got = store.get("p1")
    assert got.status == PlaybookStatus.RETIRED
    assert got.retired_reason == "superseded"
    assert store.retire("p1", "again") is False


def test_evaluate_lifecycle_auto_promote(store):
    pb = _pb(store, "p1")  # candidate
    for _ in range(3):
        store.mark_applied(pb.id, task_id="t", worker="w")
    store.record_outcome(pb.id, True)
    store.record_outcome(pb.id, True)
    store.record_outcome(pb.id, True)
    verdict = store.evaluate_lifecycle(
        "p1", promote_uses=3, promote_winrate=0.7, prune_uses=5, prune_winrate=0.3
    )
    assert verdict == "promoted"
    assert store.get("p1").status == PlaybookStatus.ACTIVE


def test_evaluate_lifecycle_prune(store):
    pb = _pb(store, "p1", status=PlaybookStatus.ACTIVE)
    for _ in range(6):
        store.mark_applied(pb.id, task_id="t", worker="w")
    store.record_outcome(pb.id, False)
    store.record_outcome(pb.id, False)
    store.record_outcome(pb.id, True)  # winrate 0.33 < 0.3? no -> tune below
    store.record_outcome(pb.id, False)  # 1/4 = 0.25 < 0.3
    verdict = store.evaluate_lifecycle(
        "p1", promote_uses=3, promote_winrate=0.7, prune_uses=5, prune_winrate=0.3
    )
    assert verdict == "retired"
    got = store.get("p1")
    assert got.status == PlaybookStatus.RETIRED
    assert got.retired_reason


def test_evaluate_lifecycle_no_prune_without_decided_outcomes(store):
    pb = _pb(store, "p1", status=PlaybookStatus.ACTIVE)
    for _ in range(8):
        store.mark_applied(pb.id, task_id="t", worker="w")  # used a lot, never verified
    verdict = store.evaluate_lifecycle(
        "p1", promote_uses=3, promote_winrate=0.7, prune_uses=5, prune_winrate=0.3
    )
    assert verdict is None  # winrate 0.0 but nothing decided — must NOT prune
    assert store.get("p1").status == PlaybookStatus.ACTIVE


def test_evaluate_lifecycle_noop_when_thresholds_unmet(store):
    pb = _pb(store, "p1")
    store.mark_applied(pb.id, task_id="t", worker="w")
    store.record_outcome(pb.id, True)
    assert (
        store.evaluate_lifecycle(
            "p1", promote_uses=3, promote_winrate=0.7, prune_uses=5, prune_winrate=0.3
        )
        is None
    )
    assert store.get("p1").status == PlaybookStatus.CANDIDATE
