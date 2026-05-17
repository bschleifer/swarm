"""complete_task → synthesis hook + swarm_get_playbooks MCP handler.

Covers the Phase 1 acceptance criteria that the post-ship hook fires
synthesis without blocking the ship path, and the recall tool returns
scoped active playbooks.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from swarm.db.core import SwarmDB
from swarm.db.playbook_store import PlaybookStore
from swarm.mcp.tools import _handle_get_playbooks
from swarm.playbooks.models import Playbook, PlaybookStatus, project_scope
from tests.conftest import make_daemon


@pytest.fixture
def daemon(monkeypatch):
    return make_daemon(monkeypatch)


# --- complete_task post-ship hook --------------------------------------


async def _drain():
    # Let the fire-and-forget asyncio.create_task run to completion.
    for _ in range(5):
        await asyncio.sleep(0)


async def test_complete_task_fires_synthesis(daemon):
    synth = SimpleNamespace(synthesize=AsyncMock(return_value=None))
    daemon.playbook_synthesizer = synth
    task = daemon.create_task(title="Add retry", description="5xx drops")
    daemon.task_board.assign(task.id, "api")

    assert daemon.complete_task(task.id, resolution="added backoff retry + test", verify=False)
    await _drain()

    synth.synthesize.assert_awaited_once()
    kwargs = synth.synthesize.await_args.kwargs
    assert kwargs["worker"] == "api"
    assert kwargs["resolution"] == "added backoff retry + test"


async def test_complete_task_survives_synthesis_error(daemon):
    daemon.playbook_synthesizer = SimpleNamespace(
        synthesize=AsyncMock(side_effect=RuntimeError("boom"))
    )
    task = daemon.create_task(title="x", description="y")
    daemon.task_board.assign(task.id, "api")

    # Ship must succeed even though synthesis blows up downstream.
    assert daemon.complete_task(task.id, resolution="did the thing", verify=False) is True
    await _drain()  # the raised error is swallowed by the done-callback


async def test_complete_task_no_synthesizer_is_noop(daemon):
    daemon.playbook_synthesizer = None
    task = daemon.create_task(title="x", description="y")
    daemon.task_board.assign(task.id, "api")
    assert daemon.complete_task(task.id, resolution="done well enough", verify=False) is True


# --- swarm_get_playbooks MCP handler -----------------------------------


@pytest.fixture
def store(tmp_path):
    return PlaybookStore(SwarmDB(tmp_path / "swarm.db"))


def test_get_playbooks_no_store():
    d = SimpleNamespace()
    out = _handle_get_playbooks(d, "w", {})
    assert "No playbook store" in out[0]["text"]


def test_get_playbooks_returns_active_only(store):
    store.create(
        Playbook(
            name="retry-pb",
            title="Retry with backoff",
            trigger="outbound sender drops on 5xx",
            body="1. wrap 2. backoff 3. dead-letter",
            status=PlaybookStatus.ACTIVE,
        )
    )
    store.create(
        Playbook(name="cand", title="cand", body="retry draft", status=PlaybookStatus.CANDIDATE)
    )
    d = SimpleNamespace(playbook_store=store)

    out = _handle_get_playbooks(d, "w", {"query": "retry 5xx sender"})
    text = out[0]["text"]
    assert "Retry with backoff" in text
    assert "cand" not in text  # candidates are not recalled


def test_get_playbooks_scope_filter(store):
    store.create(
        Playbook(name="g", title="global pb", body="global body", status=PlaybookStatus.ACTIVE)
    )
    store.create(
        Playbook(
            name="p",
            title="proj pb",
            body="proj body",
            scope=project_scope("swarm"),
            status=PlaybookStatus.ACTIVE,
        )
    )
    d = SimpleNamespace(playbook_store=store)
    out = _handle_get_playbooks(d, "w", {"scope": "project:swarm"})
    text = out[0]["text"]
    assert "proj pb" in text
    assert "global pb" not in text
