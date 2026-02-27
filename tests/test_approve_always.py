"""Tests for the /action/proposal/approve-always endpoint."""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from swarm.config import HiveConfig, QueenConfig
from swarm.drones.log import DroneLog
from swarm.drones.pilot import DronePilot
from swarm.queen.queen import Queen
from swarm.queen.queue import QueenCallQueue
from swarm.server.analyzer import QueenAnalyzer
from swarm.server.config_manager import ConfigManager
from swarm.server.daemon import SwarmDaemon
from swarm.server.email_service import EmailService
from swarm.server.proposals import ProposalManager
from swarm.server.task_manager import TaskManager
from swarm.server.worker_service import WorkerService
from swarm.tasks.board import TaskBoard
from swarm.tasks.history import TaskHistory
from swarm.tasks.proposal import AssignmentProposal, ProposalStore
from swarm.web.app import handle_action_approve_always
from swarm.worker.worker import Worker, WorkerState
from tests.fakes.process import FakeWorkerProcess

_TEST_PASSWORD = "test-secret"
_HEADERS = {"X-Requested-With": "Dashboard"}


@pytest.fixture
def daemon(monkeypatch):
    monkeypatch.setattr("swarm.queen.queen.load_session", lambda _: None)
    monkeypatch.setattr("swarm.queen.queen.save_session", lambda *a: None)

    cfg = HiveConfig(session_name="test", api_password=_TEST_PASSWORD)
    cfg.source_path = str(Path(tempfile.mktemp(suffix=".yaml")))
    d = SwarmDaemon.__new__(SwarmDaemon)
    d.config = cfg
    d.workers = [
        Worker(name="w1", path="/tmp/w1", process=FakeWorkerProcess(name="w1")),
    ]
    d._worker_lock = asyncio.Lock()
    d.drone_log = DroneLog()
    d.task_board = TaskBoard()
    d.task_history = TaskHistory(log_file=Path(tempfile.mktemp(suffix=".jsonl")))
    d.queen = Queen(config=QueenConfig(cooldown=0.0), session_name="test")
    d.queen_queue = QueenCallQueue(max_concurrent=2)
    d.proposal_store = ProposalStore()
    d.proposals = ProposalManager(d.proposal_store, d)
    d.analyzer = QueenAnalyzer(d.queen, d, d.queen_queue)
    d.notification_bus = MagicMock()
    d.pilot = MagicMock(spec=DronePilot)
    d.pilot.enabled = True
    d.ws_clients = set()
    d.terminal_ws_clients = set()
    d.pool = None
    d.start_time = 0.0
    d.broadcast_ws = MagicMock()
    d.graph_mgr = None
    d.email = EmailService(
        drone_log=d.drone_log,
        queen=d.queen,
        graph_mgr=d.graph_mgr,
        broadcast_ws=d.broadcast_ws,
    )
    d.tasks = TaskManager(
        task_board=d.task_board,
        task_history=d.task_history,
        drone_log=d.drone_log,
        pilot=d.pilot,
    )
    d.send_to_worker = AsyncMock()
    d._prep_worker_for_task = AsyncMock()
    d._heartbeat_task = None
    d._usage_task = None
    d._heartbeat_snapshot = {}
    d._config_mtime = 0.0
    d._bg_tasks: set[asyncio.Task[object]] = set()
    d._notification_history: list[dict] = []
    d.config_mgr = ConfigManager(d)
    d.worker_svc = WorkerService(d)
    return d


@pytest.fixture
async def client(daemon):
    app = web.Application()
    app["daemon"] = daemon
    app.router.add_post("/action/proposal/approve-always", handle_action_approve_always)
    async with TestClient(TestServer(app)) as c:
        yield c


def _make_escalation(daemon: SwarmDaemon) -> AssignmentProposal:
    task = daemon.task_board.create(title="Run az command")
    daemon.workers[0].state = WorkerState.RESTING
    p = AssignmentProposal(
        worker_name="w1",
        task_id=task.id,
        task_title=task.title,
        proposal_type="escalation",
    )
    daemon.proposal_store.add(p)
    return p


@pytest.mark.asyncio
async def test_approve_always_valid(client, daemon):
    """Valid pattern → rule added + proposal approved."""
    p = _make_escalation(daemon)
    resp = await client.post(
        "/action/proposal/approve-always",
        data={"proposal_id": p.id, "pattern": r"\baz\b"},
        headers=_HEADERS,
    )
    assert resp.status == 200
    data = await resp.json()
    assert data["status"] == "approved"
    assert data["rule_added"] == r"\baz\b"
    # Rule was appended to config
    rules = daemon.config.drones.approval_rules
    assert len(rules) == 1
    assert rules[0].pattern == r"\baz\b"
    assert rules[0].action == "approve"


@pytest.mark.asyncio
async def test_approve_always_invalid_regex(client, daemon):
    """Invalid regex → 400."""
    p = _make_escalation(daemon)
    resp = await client.post(
        "/action/proposal/approve-always",
        data={"proposal_id": p.id, "pattern": "[invalid"},
        headers=_HEADERS,
    )
    assert resp.status == 400
    data = await resp.json()
    assert "invalid regex" in data["error"]


@pytest.mark.asyncio
async def test_approve_always_missing_proposal_id(client, daemon):
    """Missing proposal_id → 400."""
    resp = await client.post(
        "/action/proposal/approve-always",
        data={"pattern": r"\baz\b"},
        headers=_HEADERS,
    )
    assert resp.status == 400
    data = await resp.json()
    assert "proposal_id" in data["error"]


@pytest.mark.asyncio
async def test_approve_always_missing_pattern(client, daemon):
    """Missing pattern → 400."""
    p = _make_escalation(daemon)
    resp = await client.post(
        "/action/proposal/approve-always",
        data={"proposal_id": p.id},
        headers=_HEADERS,
    )
    assert resp.status == 400
    data = await resp.json()
    assert "pattern" in data["error"]


@pytest.mark.asyncio
async def test_approve_always_unknown_proposal(client, daemon):
    """Unknown proposal → 404."""
    resp = await client.post(
        "/action/proposal/approve-always",
        data={"proposal_id": "nonexistent", "pattern": r"\baz\b"},
        headers=_HEADERS,
    )
    assert resp.status == 404
    data = await resp.json()
    assert "not found" in data["error"]
