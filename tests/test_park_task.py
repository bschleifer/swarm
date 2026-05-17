"""#406: swarm_park_task — a worker hands its own ACTIVE task back to
ASSIGNED with a reason, no blocker binding. Composes with #405 INV-1/2/3
immediately (no reconciler/reload).
"""

from __future__ import annotations

import pytest

from swarm.drones.log import SystemAction
from swarm.mcp.tools import _handle_park_task
from swarm.tasks.board import TaskBoard
from swarm.tasks.task import TaskStatus
from tests.conftest import make_daemon


@pytest.fixture
def board():
    return TaskBoard()


@pytest.fixture
def daemon(monkeypatch):
    return make_daemon(monkeypatch)


def _active(board, title, worker):
    t = board.create(title=title)
    board.assign(t.id, worker)
    board.activate(t.id)
    return board.get(t.id)


# --- TaskBoard.park -----------------------------------------------------


def test_park_own_active_task_to_assigned(board):
    t = _active(board, "x", "api")
    assert board.park(t.id, "api", "operator preempt") is True
    got = board.get(t.id)
    assert got.status == TaskStatus.ASSIGNED
    assert got.assigned_worker == "api"  # still owns it — just set down
    # INV-3: worker now has no ACTIVE task (immediately, no reconciler).
    assert board.current_task_for_worker("api") is None


def test_park_rejects_non_owner(board):
    t = _active(board, "x", "api")
    assert board.park(t.id, "web", "not mine") is False
    assert board.get(t.id).status == TaskStatus.ACTIVE  # untouched


def test_park_rejects_non_active(board):
    t = board.create(title="x")
    board.assign(t.id, "api")  # ASSIGNED, not ACTIVE
    assert board.park(t.id, "api", "r") is False
    assert board.park("missing-id", "api", "r") is False


# --- swarm_park_task MCP handler ---------------------------------------


def test_handler_parks_callers_active_task_with_buzz(daemon):
    t = daemon.task_board.create(title="initiative")
    daemon.task_board.assign(t.id, "swarm")
    daemon.task_board.activate(t.id)

    out = _handle_park_task(daemon, "swarm", {"reason": "operator preempt to #405"})

    assert "parked" in out[0]["text"].lower()
    assert daemon.task_board.get(t.id).status == TaskStatus.ASSIGNED
    assert SystemAction.TASK_PARKED in [e.action for e in daemon.drone_log.entries]


def test_handler_requires_reason(daemon):
    t = daemon.task_board.create(title="x")
    daemon.task_board.assign(t.id, "swarm")
    daemon.task_board.activate(t.id)
    out = _handle_park_task(daemon, "swarm", {})
    assert "reason" in out[0]["text"].lower()
    assert daemon.task_board.get(t.id).status == TaskStatus.ACTIVE  # unchanged


def test_handler_no_active_task(daemon):
    out = _handle_park_task(daemon, "swarm", {"reason": "nothing to park"})
    assert "no active task" in out[0]["text"].lower()


def test_park_is_not_a_blocker(daemon):
    """Distinct from swarm_report_blocker — parking creates NO binding."""
    t = daemon.task_board.create(title="x")
    daemon.task_board.assign(t.id, "swarm")
    daemon.task_board.activate(t.id)
    _handle_park_task(daemon, "swarm", {"reason": "scope change"})
    store = getattr(daemon, "blocker_store", None)
    if store is not None:
        assert store.list_for_worker("swarm") == []  # no blocker binding


def test_preempt_scenario_board_immediately_truthful(daemon):
    """Worker parks mid-initiative; board is truthful with no reload."""
    a = daemon.task_board.create(title="phase work")
    daemon.task_board.assign(a.id, "swarm")
    daemon.task_board.activate(a.id)
    assert daemon.task_board.current_task_for_worker("swarm").id == a.id

    _handle_park_task(daemon, "swarm", {"reason": "operator STOP — pivot"})

    # No reconciler, no daemon reload — immediately coherent:
    assert daemon.task_board.current_task_for_worker("swarm") is None
    assert daemon.task_board.get(a.id).status == TaskStatus.ASSIGNED
    active = [
        t for t in daemon.task_board.tasks_for_worker("swarm") if t.status == TaskStatus.ACTIVE
    ]
    assert active == []  # INV-1/2/3 satisfied
