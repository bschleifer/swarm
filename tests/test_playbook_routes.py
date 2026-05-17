"""#404 Phase 4: /api/playbooks list + promote/retire routes."""

from __future__ import annotations

import pytest
from aiohttp.test_utils import TestClient, TestServer

from swarm.db.core import SwarmDB
from swarm.db.playbook_store import PlaybookStore
from swarm.playbooks.models import Playbook, PlaybookStatus
from swarm.server.api import create_app
from tests.conftest import make_daemon

_H = {"X-Requested-With": "test"}


@pytest.fixture
def client_store(tmp_path):
    store = PlaybookStore(SwarmDB(tmp_path / "swarm.db"))
    daemon = make_daemon()
    daemon.playbook_store = store
    app = create_app(daemon, enable_web=False)
    return app, store


async def test_list_returns_all_including_candidates(client_store):
    app, store = client_store
    store.create(
        Playbook(
            name="active-pb",
            title="Active PB",
            trigger="when X",
            body="steps",
            status=PlaybookStatus.ACTIVE,
            wins=3,
            losses=1,
            uses=5,
            provenance_task_ids=["t-1"],
        )
    )
    store.create(Playbook(name="cand-pb", body="draft", status=PlaybookStatus.CANDIDATE))
    async with TestClient(TestServer(app)) as c:
        resp = await c.get("/api/playbooks", headers=_H)
        assert resp.status == 200
        data = await resp.json()
    by_name = {p["name"]: p for p in data["playbooks"]}
    assert set(by_name) == {"active-pb", "cand-pb"}
    a = by_name["active-pb"]
    assert a["status"] == "active" and a["winrate"] == 0.75
    assert a["uses"] == 5 and a["provenance_task_ids"] == ["t-1"]
    assert by_name["cand-pb"]["status"] == "candidate"  # candidates surfaced


async def test_list_status_filter_and_invalid(client_store):
    app, store = client_store
    store.create(Playbook(name="a", body="b", status=PlaybookStatus.ACTIVE))
    store.create(Playbook(name="c", body="d", status=PlaybookStatus.CANDIDATE))
    async with TestClient(TestServer(app)) as c:
        r = await c.get("/api/playbooks?status=active", headers=_H)
        names = {p["name"] for p in (await r.json())["playbooks"]}
        assert names == {"a"}
        bad = await c.get("/api/playbooks?status=bogus", headers=_H)
        assert bad.status == 400


async def test_promote_and_retire(client_store):
    app, store = client_store
    store.create(Playbook(name="pb", body="b"))  # candidate
    async with TestClient(TestServer(app)) as c:
        r = await c.post("/api/playbooks/pb/promote", headers=_H)
        assert r.status == 200 and (await r.json())["promoted"] is True
        assert store.get("pb").status == PlaybookStatus.ACTIVE
        # promote again → 404 (already active)
        assert (await c.post("/api/playbooks/pb/promote", headers=_H)).status == 404

        r2 = await c.post("/api/playbooks/pb/retire", headers=_H, json={"reason": "superseded"})
        assert r2.status == 200 and (await r2.json())["retired"] is True
        got = store.get("pb")
        assert got.status == PlaybookStatus.RETIRED and got.retired_reason == "superseded"


async def test_promote_missing_is_404(client_store):
    app, _ = client_store
    async with TestClient(TestServer(app)) as c:
        assert (await c.post("/api/playbooks/nope/promote", headers=_H)).status == 404


async def test_store_unavailable_503():
    daemon = make_daemon()
    daemon.playbook_store = None
    app = create_app(daemon, enable_web=False)
    async with TestClient(TestServer(app)) as c:
        assert (await c.get("/api/playbooks", headers=_H)).status == 503
