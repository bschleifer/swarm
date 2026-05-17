"""Tests for PlaybookStore + Playbook model (playbook-synthesis-loop Phase 1)."""

from __future__ import annotations

import pytest

from swarm.db.core import SwarmDB
from swarm.db.playbook_store import PlaybookStore
from swarm.playbooks.models import (
    Playbook,
    PlaybookStatus,
    content_hash,
    project_scope,
)


@pytest.fixture
def store(tmp_path):
    db = SwarmDB(tmp_path / "swarm.db")
    return PlaybookStore(db), db


# --- migration / schema -------------------------------------------------


def test_migration_creates_tables(store):
    _, db = store
    names = {
        r["name"]
        for r in db.fetchall(
            "SELECT name FROM sqlite_master WHERE type='table' AND name IN "
            "('playbooks','playbook_events')"
        )
    }
    assert names == {"playbooks", "playbook_events"}


def test_v10_migration_idempotent(tmp_path):
    db = SwarmDB(tmp_path / "swarm.db")
    # Re-running the v10 migration on an already-v10 DB must not raise.
    db._migrate_v10_playbooks()
    db._migrate_v10_playbooks()
    assert db.fetchone("SELECT 1 FROM playbooks LIMIT 1") is None  # table exists, empty


# --- model --------------------------------------------------------------


def test_content_hash_normalizes_case_and_whitespace():
    assert content_hash("Run   /check\nthen commit") == content_hash("run /check then commit")
    assert content_hash("a") != content_hash("b")


def test_winrate():
    pb = Playbook(name="x", wins=3, losses=1)
    assert pb.winrate == 0.75
    assert Playbook(name="y").winrate == 0.0  # nothing decided


# --- CRUD ---------------------------------------------------------------


def test_create_and_get_roundtrip(store):
    st, _ = store
    pb = st.create(
        Playbook(
            name="tdd-bug-fix",
            title="TDD bug fix",
            trigger="when fixing a reported bug",
            body="1. write failing test\n2. minimal fix\n3. /check",
            provenance_task_ids=["t-1"],
            source_worker="swarm",
            scope=project_scope("swarm"),
        )
    )
    assert pb.id
    got = st.get("tdd-bug-fix")
    assert got is not None
    assert got.id == pb.id
    assert got.provenance_task_ids == ["t-1"]
    assert got.scope == "project:swarm"
    assert st.get_by_id(pb.id).name == "tdd-bug-fix"


def test_exact_duplicate_is_folded_not_duplicated(store):
    st, db = store
    body = "1. write failing test\n2. minimal fix\n3. /check"
    a = st.create(Playbook(name="pb-a", body=body, provenance_task_ids=["t-1"]))
    b = st.create(Playbook(name="pb-b", body=body, provenance_task_ids=["t-2"]))

    # Same content_hash → second create folds into the first, no new row.
    assert b.id == a.id
    rows = db.fetchall("SELECT id FROM playbooks")
    assert len(rows) == 1
    folded = st.get("pb-a")
    assert folded.uses == 1
    assert set(folded.provenance_task_ids) == {"t-1", "t-2"}
    assert st.get("pb-b") is None


def test_search_ranks_relevant_and_filters_scope_status(store):
    st, _ = store
    st.create(
        Playbook(
            name="cf-deploy",
            title="Cloudflare tunnel deploy",
            trigger="deploying behind a cloudflare tunnel",
            body="configure cloudflared and verify the reverse proxy",
            status=PlaybookStatus.ACTIVE,
        )
    )
    st.create(
        Playbook(
            name="pytest-flake",
            title="Debug a flaky pytest",
            trigger="a test passes alone but fails under load",
            body="isolate the test, check selector timeouts",
            status=PlaybookStatus.ACTIVE,
        )
    )
    st.create(
        Playbook(
            name="cand-x",
            title="cloudflare candidate",
            body="cloudflare tunnel notes",
            status=PlaybookStatus.CANDIDATE,
        )
    )

    hits = st.search("cloudflare tunnel", status=PlaybookStatus.ACTIVE)
    names = [h.name for h in hits]
    assert "cf-deploy" in names
    assert "cand-x" not in names  # status filter excludes candidates
    assert "pytest-flake" not in names


def test_find_near_duplicate_respects_exclude(store):
    st, _ = store
    st.create(
        Playbook(
            name="orig",
            title="ship pipeline",
            body="run check then commit then push",
            status=PlaybookStatus.ACTIVE,
        )
    )
    near = st.find_near_duplicate("run check then commit then push the branch")
    assert near is not None and near.name == "orig"
    assert st.find_near_duplicate("run check then commit", exclude_name="orig") is None


def test_list_filters(store):
    st, _ = store
    st.create(Playbook(name="g1", body="global one", status=PlaybookStatus.ACTIVE))
    st.create(
        Playbook(
            name="p1",
            body="proj one",
            scope=project_scope("swarm"),
            status=PlaybookStatus.CANDIDATE,
        )
    )
    assert {p.name for p in st.list(status=PlaybookStatus.ACTIVE)} == {"g1"}
    assert {p.name for p in st.list(scope="project:swarm")} == {"p1"}
