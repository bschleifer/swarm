"""Tests for server/api.py — REST + WebSocket API."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import AioHTTPTestCase, TestClient, TestServer

from swarm.buzz.log import BuzzLog
from swarm.buzz.pilot import BuzzPilot
from swarm.config import BuzzConfig, HiveConfig, QueenConfig
from swarm.queen.queen import Queen
from swarm.server.api import create_app
from swarm.server.daemon import SwarmDaemon
from swarm.tasks.board import TaskBoard
from swarm.worker.worker import Worker, WorkerState


@pytest.fixture
def daemon(monkeypatch):
    """Create a minimal daemon without starting it."""
    monkeypatch.setattr("swarm.queen.queen.load_session", lambda _: None)
    monkeypatch.setattr("swarm.queen.queen.save_session", lambda *a: None)

    cfg = HiveConfig(session_name="test")
    d = SwarmDaemon.__new__(SwarmDaemon)
    d.config = cfg
    d.workers = [
        Worker(name="api", path="/tmp/api", pane_id="%0"),
        Worker(name="web", path="/tmp/web", pane_id="%1"),
    ]
    import asyncio
    d._worker_lock = asyncio.Lock()
    d.buzz_log = BuzzLog()
    d.task_board = TaskBoard()
    d.queen = Queen(config=QueenConfig(cooldown=0.0), session_name="test")
    d.notification_bus = MagicMock()
    d.pilot = MagicMock(spec=BuzzPilot)
    d.pilot.enabled = True
    d.pilot.toggle = MagicMock(return_value=False)
    d.ws_clients = set()
    d.start_time = 0.0
    d._broadcast_ws = MagicMock()
    return d


@pytest.fixture
async def client(daemon):
    """Create an aiohttp test client."""
    app = create_app(daemon, enable_web=False)
    async with TestClient(TestServer(app)) as client:
        yield client


@pytest.mark.asyncio
async def test_health(client):
    resp = await client.get("/api/health")
    assert resp.status == 200
    data = await resp.json()
    assert data["status"] == "ok"
    assert data["workers"] == 2


@pytest.mark.asyncio
async def test_workers_list(client):
    resp = await client.get("/api/workers")
    assert resp.status == 200
    data = await resp.json()
    assert len(data["workers"]) == 2
    assert data["workers"][0]["name"] == "api"


@pytest.mark.asyncio
async def test_worker_detail_not_found(client):
    resp = await client.get("/api/workers/nonexistent")
    assert resp.status == 404


@pytest.mark.asyncio
async def test_worker_send_empty_message(client):
    resp = await client.post("/api/workers/api/send", json={"message": ""})
    assert resp.status == 400


@pytest.mark.asyncio
async def test_worker_send_not_string(client):
    resp = await client.post("/api/workers/api/send", json={"message": 123})
    assert resp.status == 400


@pytest.mark.asyncio
async def test_worker_continue(client):
    with patch("swarm.tmux.cell.send_enter", new_callable=AsyncMock):
        resp = await client.post("/api/workers/api/continue")
        assert resp.status == 200


@pytest.mark.asyncio
async def test_worker_kill(client, monkeypatch):
    with patch("swarm.worker.manager.kill_worker", new_callable=AsyncMock):
        resp = await client.post("/api/workers/api/kill")
        assert resp.status == 200
        data = await resp.json()
        assert data["status"] == "killed"


@pytest.mark.asyncio
async def test_buzz_log(client):
    resp = await client.get("/api/buzz/log")
    assert resp.status == 200
    data = await resp.json()
    assert "entries" in data


@pytest.mark.asyncio
async def test_buzz_log_limit_capped(client):
    resp = await client.get("/api/buzz/log?limit=99999")
    assert resp.status == 200


@pytest.mark.asyncio
async def test_buzz_status(client):
    resp = await client.get("/api/buzz/status")
    assert resp.status == 200
    data = await resp.json()
    assert "enabled" in data


@pytest.mark.asyncio
async def test_buzz_toggle(client):
    resp = await client.post("/api/buzz/toggle")
    assert resp.status == 200


@pytest.mark.asyncio
async def test_tasks_crud(client):
    # Create
    resp = await client.post("/api/tasks", json={"title": "Fix bug"})
    assert resp.status == 201
    data = await resp.json()
    task_id = data["id"]

    # List
    resp = await client.get("/api/tasks")
    assert resp.status == 200
    data = await resp.json()
    assert len(data["tasks"]) == 1

    # Assign
    resp = await client.post(f"/api/tasks/{task_id}/assign", json={"worker": "api"})
    assert resp.status == 200

    # Complete
    resp = await client.post(f"/api/tasks/{task_id}/complete")
    assert resp.status == 200


@pytest.mark.asyncio
async def test_create_task_invalid_title(client):
    resp = await client.post("/api/tasks", json={"title": ""})
    assert resp.status == 400


@pytest.mark.asyncio
async def test_create_task_invalid_priority(client):
    resp = await client.post("/api/tasks", json={"title": "Test", "priority": "mega"})
    assert resp.status == 400


@pytest.mark.asyncio
async def test_assign_task_nonexistent_worker(client):
    # First create a task
    resp = await client.post("/api/tasks", json={"title": "Test"})
    data = await resp.json()
    task_id = data["id"]

    # Assign to non-existent worker
    resp = await client.post(f"/api/tasks/{task_id}/assign", json={"worker": "nonexistent"})
    assert resp.status == 404


@pytest.mark.asyncio
async def test_assign_task_not_found(client):
    resp = await client.post("/api/tasks/nonexistent/assign", json={"worker": "api"})
    assert resp.status == 404


@pytest.mark.asyncio
async def test_complete_task_not_found(client):
    resp = await client.post("/api/tasks/nonexistent/complete")
    assert resp.status == 404


@pytest.mark.asyncio
async def test_worker_kill_not_found(client):
    resp = await client.post("/api/workers/nonexistent/kill")
    assert resp.status == 404
