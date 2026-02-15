"""Tests for web/app.py — utility functions and action handlers."""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web

from swarm.config import GroupConfig
from swarm.drones.log import DroneAction, DroneLog, LogCategory, SystemAction
from swarm.server.daemon import SwarmOperationError, WorkerNotFoundError
from swarm.tasks.board import TaskBoard
from swarm.web.app import (
    _build_worker_groups,
    _format_age,
    _json_error,
    _require_queen,
    _system_log_dicts,
    _task_dicts,
    _worker_dicts,
    handle_swarm_errors,
)
from swarm.worker.worker import Worker, WorkerState


# --- _format_age ---


def test_format_age_just_now():
    assert _format_age(time.time() - 30) == "just now"


def test_format_age_minutes():
    assert _format_age(time.time() - 300) == "5m ago"


def test_format_age_hours():
    assert _format_age(time.time() - 7200) == "2h ago"


def test_format_age_days():
    assert _format_age(time.time() - 172800) == "2d ago"


# --- _json_error ---


def test_json_error_default_status():
    resp = _json_error("oops")
    assert resp.status == 400


def test_json_error_custom_status():
    resp = _json_error("not found", 404)
    assert resp.status == 404


# --- _require_queen ---


def test_require_queen_present():
    d = MagicMock()
    d.queen = MagicMock()
    assert _require_queen(d) is d.queen


def test_require_queen_missing():
    d = MagicMock()
    d.queen = None
    with pytest.raises(SwarmOperationError, match="Queen not configured"):
        _require_queen(d)


# --- _worker_dicts ---


def test_worker_dicts():
    w = Worker(name="api", path="/tmp/api", pane_id="%0")
    w._state = WorkerState.BUZZING
    w._state_since = time.time() - 60
    daemon = MagicMock()
    daemon.workers = [w]
    result = _worker_dicts(daemon)
    assert len(result) == 1
    assert result[0]["name"] == "api"
    assert "state_duration" in result[0]
    # Should be human-readable, not raw seconds
    assert isinstance(result[0]["state_duration"], str)


# --- _task_dicts ---


def test_task_dicts_empty():
    daemon = MagicMock()
    daemon.task_board = TaskBoard()
    result = _task_dicts(daemon)
    assert result == []


def test_task_dicts_with_tasks():
    board = TaskBoard()
    board.create(title="Fix bug", description="desc")
    daemon = MagicMock()
    daemon.task_board = board
    result = _task_dicts(daemon)
    assert len(result) == 1
    assert result[0]["title"] == "Fix bug"
    assert result[0]["status"] == "pending"
    assert result[0]["priority"] == "normal"
    assert "created_age" in result[0]
    assert "blocked" in result[0]


def test_task_dicts_blocked():
    """Task with unmet dependency should be marked blocked."""
    board = TaskBoard()
    t1 = board.create(title="First")
    t2 = board.create(title="Second", depends_on=[t1.id])
    daemon = MagicMock()
    daemon.task_board = board
    result = _task_dicts(daemon)
    task_map = {t["id"]: t for t in result}
    assert task_map[t2.id]["blocked"] is True


def test_task_dicts_unblocked_when_dep_completed():
    """Task with completed dependency should not be blocked."""
    board = TaskBoard()
    t1 = board.create(title="First")
    t2 = board.create(title="Second", depends_on=[t1.id])
    # Assign first so it can be completed
    board.assign(t1.id, "worker1")
    board.complete(t1.id)
    daemon = MagicMock()
    daemon.task_board = board
    result = _task_dicts(daemon)
    task_map = {t["id"]: t for t in result}
    assert task_map[t2.id]["blocked"] is False


# --- _system_log_dicts ---


def test_system_log_dicts_empty():
    daemon = MagicMock()
    daemon.drone_log = DroneLog()
    result = _system_log_dicts(daemon)
    assert result == []


def test_system_log_dicts_excludes_system_by_default():
    """SYSTEM entries should be excluded when no category filter is set."""
    log = DroneLog()
    log.add(
        SystemAction.CONFIG_CHANGED,
        worker_name="api",
        detail="started",
        category=LogCategory.SYSTEM,
    )
    log.add(
        DroneAction.CONTINUED,
        worker_name="api",
        detail="continued",
        category=LogCategory.DRONE,
    )
    daemon = MagicMock()
    daemon.drone_log = log
    result = _system_log_dicts(daemon)
    # Only drone entry should appear
    assert len(result) == 1
    assert result[0]["category"] == "drone"


def test_system_log_dicts_category_filter():
    log = DroneLog()
    log.add(
        SystemAction.CONFIG_CHANGED,
        worker_name="api",
        detail="started",
        category=LogCategory.SYSTEM,
    )
    log.add(
        DroneAction.CONTINUED,
        worker_name="api",
        detail="continued",
        category=LogCategory.DRONE,
    )
    daemon = MagicMock()
    daemon.drone_log = log
    result = _system_log_dicts(daemon, category="system")
    assert len(result) == 1
    assert result[0]["category"] == "system"


def test_system_log_dicts_text_search():
    log = DroneLog()
    log.add(
        DroneAction.CONTINUED,
        worker_name="api",
        detail="resumed work",
        category=LogCategory.DRONE,
    )
    log.add(
        DroneAction.CONTINUED,
        worker_name="web",
        detail="kept going",
        category=LogCategory.DRONE,
    )
    daemon = MagicMock()
    daemon.drone_log = log
    result = _system_log_dicts(daemon, query="api")
    assert len(result) == 1
    assert result[0]["worker"] == "api"


def test_system_log_dicts_limit():
    log = DroneLog()
    for i in range(10):
        log.add(
            DroneAction.CONTINUED,
            worker_name=f"w{i}",
            detail=f"entry {i}",
            category=LogCategory.DRONE,
        )
    daemon = MagicMock()
    daemon.drone_log = log
    result = _system_log_dicts(daemon, limit=3)
    assert len(result) == 3


def test_system_log_dicts_multi_category_filter():
    """Comma-separated category filter should match any of the values."""
    log = DroneLog()
    log.add(DroneAction.CONTINUED, "api", "c", category=LogCategory.DRONE)
    log.add(SystemAction.TASK_CREATED, "api", "t", category=LogCategory.TASK)
    log.add(SystemAction.QUEEN_PROPOSAL, "api", "q", category=LogCategory.QUEEN)
    daemon = MagicMock()
    daemon.drone_log = log
    result = _system_log_dicts(daemon, category="drone,task")
    assert len(result) == 2
    cats = {r["category"] for r in result}
    assert cats == {"drone", "task"}


def test_system_log_dicts_operator_category():
    """OPERATOR entries should use operator category when explicitly set."""
    log = DroneLog()
    log.add(DroneAction.OPERATOR, "api", "continued", category=LogCategory.OPERATOR)
    daemon = MagicMock()
    daemon.drone_log = log
    result = _system_log_dicts(daemon)
    assert len(result) == 1
    assert result[0]["category"] == "operator"


def test_system_log_dicts_operator_filter():
    """Operator category should be filterable."""
    log = DroneLog()
    log.add(DroneAction.CONTINUED, "api", "drone", category=LogCategory.DRONE)
    log.add(DroneAction.OPERATOR, "api", "manual", category=LogCategory.OPERATOR)
    daemon = MagicMock()
    daemon.drone_log = log
    result = _system_log_dicts(daemon, category="operator")
    assert len(result) == 1
    assert result[0]["category"] == "operator"


def test_system_log_dicts_newest_first():
    """Entries should be returned newest-first."""
    log = DroneLog()
    log.add(DroneAction.CONTINUED, "api", "first", category=LogCategory.DRONE)
    log.add(DroneAction.CONTINUED, "api", "second", category=LogCategory.DRONE)
    log.add(DroneAction.CONTINUED, "api", "third", category=LogCategory.DRONE)
    daemon = MagicMock()
    daemon.drone_log = log
    result = _system_log_dicts(daemon)
    assert result[0]["detail"] == "third"
    assert result[-1]["detail"] == "first"


def test_system_log_dicts_invalid_category_ignored():
    """Invalid category values in comma-separated filter should be silently ignored."""
    log = DroneLog()
    log.add(DroneAction.CONTINUED, "api", "c", category=LogCategory.DRONE)
    daemon = MagicMock()
    daemon.drone_log = log
    result = _system_log_dicts(daemon, category="bogus,drone")
    assert len(result) == 1
    assert result[0]["category"] == "drone"


# --- _build_worker_groups ---


def test_build_worker_groups_no_config_groups():
    daemon = MagicMock()
    daemon.workers = [Worker(name="api", path="/tmp", pane_id="%0")]
    daemon.config.groups = []
    groups, ungrouped = _build_worker_groups(daemon)
    assert groups == []
    assert ungrouped == []


def test_build_worker_groups_with_groups():
    daemon = MagicMock()
    w1 = Worker(name="api", path="/tmp/api", pane_id="%0")
    w1._state = WorkerState.BUZZING
    w2 = Worker(name="web", path="/tmp/web", pane_id="%1")
    w2._state = WorkerState.RESTING
    daemon.workers = [w1, w2]
    daemon.config.groups = [GroupConfig(name="backend", workers=["api"])]
    groups, ungrouped = _build_worker_groups(daemon)
    assert len(groups) == 1
    assert groups[0]["name"] == "backend"
    assert len(groups[0]["members"]) == 1
    assert len(ungrouped) == 1
    assert ungrouped[0]["name"] == "web"


def test_build_worker_groups_skips_all_group():
    """The auto-generated 'all' group should be skipped when real groups exist."""
    daemon = MagicMock()
    w1 = Worker(name="api", path="/tmp/api", pane_id="%0")
    w1._state = WorkerState.BUZZING
    daemon.workers = [w1]
    daemon.config.groups = [
        GroupConfig(name="all", workers=["api"]),
        GroupConfig(name="backend", workers=["api"]),
    ]
    groups, ungrouped = _build_worker_groups(daemon)
    # 'all' group should be skipped
    assert len(groups) == 1
    assert groups[0]["name"] == "backend"


# --- handle_swarm_errors ---


@pytest.mark.asyncio
async def test_handle_swarm_errors_success():
    @handle_swarm_errors
    async def handler(request):
        return web.Response(text="ok")

    resp = await handler(MagicMock())
    assert resp.status == 200


@pytest.mark.asyncio
async def test_handle_swarm_errors_worker_not_found():
    @handle_swarm_errors
    async def handler(request):
        raise WorkerNotFoundError("api")

    resp = await handler(MagicMock())
    assert resp.status == 404


@pytest.mark.asyncio
async def test_handle_swarm_errors_operation_error():
    @handle_swarm_errors
    async def handler(request):
        raise SwarmOperationError("bad state")

    resp = await handler(MagicMock())
    assert resp.status == 409


@pytest.mark.asyncio
async def test_handle_swarm_errors_generic_error():
    @handle_swarm_errors
    async def handler(request):
        raise RuntimeError("boom")

    with patch("swarm.web.app.console_log"):
        resp = await handler(MagicMock())
    assert resp.status == 500
