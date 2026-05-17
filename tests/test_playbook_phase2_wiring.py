"""Phase 2 wiring: recall-at-dispatch, win/loss attribution, on_verdict hook."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from swarm.config.models import PlaybookConfig
from swarm.db.core import SwarmDB
from swarm.db.playbook_store import PlaybookStore
from swarm.drones.log import SystemAction
from swarm.drones.verifier import fire_and_forget
from swarm.playbooks.models import Playbook, PlaybookStatus
from swarm.tasks.task import VerificationStatus
from tests.conftest import make_daemon


@pytest.fixture
def daemon(monkeypatch, tmp_path):
    d = make_daemon(monkeypatch)
    d.playbook_store = PlaybookStore(SwarmDB(tmp_path / "swarm.db"))
    return d


def _active(store, name, **kw):
    return store.create(
        Playbook(name=name, body=f"steps for {name}", status=PlaybookStatus.ACTIVE, **kw)
    )


# --- recall-at-dispatch -------------------------------------------------


def test_recall_injects_active_marks_applied_and_buzzes(daemon):
    _active(
        daemon.playbook_store,
        "retry-backoff",
        title="Retry with backoff",
        trigger="outbound sender drops on 5xx",
    )
    task = daemon.create_task(title="Add retry to webhook sender", description="5xx drops")

    block = daemon._recall_playbooks_for_task(task, "api")

    assert "retry-backoff" in block and "Retry with backoff" in block
    assert daemon.playbook_store.playbooks_applied_to_task(task.id)
    assert daemon.playbook_store.get("retry-backoff").uses == 1
    assert SystemAction.PLAYBOOK_APPLIED in [e.action for e in daemon.drone_log.entries]


def test_recall_excludes_candidates(daemon):
    daemon.playbook_store.create(
        Playbook(name="cand", title="cand", body="retry draft", status=PlaybookStatus.CANDIDATE)
    )
    task = daemon.create_task(title="retry sender", description="retry")
    assert daemon._recall_playbooks_for_task(task, "api") == ""


def test_recall_disabled_config_returns_empty(daemon):
    _active(daemon.playbook_store, "p1", title="p1", trigger="x")
    daemon.config.playbooks = PlaybookConfig(enabled=False)
    task = daemon.create_task(title="p1 thing", description="p1")
    assert daemon._recall_playbooks_for_task(task, "api") == ""


# --- win/loss attribution ----------------------------------------------


async def test_attribution_verified_is_win_and_autopromotes(daemon):
    daemon.config.playbooks = PlaybookConfig(auto_promote_uses=1, auto_promote_winrate=0.5)
    pb = daemon.playbook_store.create(Playbook(name="p1", body="b"))  # candidate
    task = daemon.create_task(title="t", description="d")
    daemon.task_board.assign(task.id, "api")
    daemon.playbook_store.mark_applied(pb.id, task_id=task.id, worker="api")

    await daemon._attribute_playbook_outcome(task, VerificationStatus.VERIFIED)

    got = daemon.playbook_store.get("p1")
    assert (got.wins, got.losses) == (1, 0)
    assert got.status == PlaybookStatus.ACTIVE  # auto-promoted
    assert SystemAction.PLAYBOOK_PROMOTED in [e.action for e in daemon.drone_log.entries]


async def test_attribution_reopened_is_loss(daemon):
    pb = _active(daemon.playbook_store, "p1")
    task = daemon.create_task(title="t", description="d")
    daemon.playbook_store.mark_applied(pb.id, task_id=task.id, worker="api")

    await daemon._attribute_playbook_outcome(task, VerificationStatus.REOPENED)

    got = daemon.playbook_store.get("p1")
    assert (got.wins, got.losses) == (0, 1)


async def test_attribution_skipped_is_noop(daemon):
    pb = _active(daemon.playbook_store, "p1")
    task = daemon.create_task(title="t", description="d")
    daemon.playbook_store.mark_applied(pb.id, task_id=task.id, worker="api")

    await daemon._attribute_playbook_outcome(task, VerificationStatus.SKIPPED)

    got = daemon.playbook_store.get("p1")
    assert (got.wins, got.losses) == (0, 0)


async def test_attribution_no_applied_playbooks_is_noop(daemon):
    _active(daemon.playbook_store, "p1")
    task = daemon.create_task(title="t", description="d")
    # never marked applied to this task
    await daemon._attribute_playbook_outcome(task, VerificationStatus.VERIFIED)
    assert daemon.playbook_store.get("p1").wins == 0


# --- fire_and_forget invokes on_verdict (verification-resolution path) --


async def test_fire_and_forget_invokes_on_verdict_with_terminal_status():
    on_verdict = AsyncMock()
    drone = SimpleNamespace(
        verify_completion=AsyncMock(return_value=VerificationStatus.VERIFIED),
        _on_verdict=on_verdict,
    )
    task = SimpleNamespace(number=7)
    await fire_and_forget(drone, task)
    on_verdict.assert_awaited_once_with(task, VerificationStatus.VERIFIED)


async def test_fire_and_forget_skips_on_verdict_when_verify_raises():
    on_verdict = AsyncMock()
    drone = SimpleNamespace(
        verify_completion=AsyncMock(side_effect=RuntimeError("boom")),
        _on_verdict=on_verdict,
    )
    await fire_and_forget(drone, SimpleNamespace(number=7))  # must not raise
    on_verdict.assert_not_awaited()
