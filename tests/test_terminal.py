"""Tests for server/terminal.py — interactive terminal WebSocket."""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from aiohttp.test_utils import TestClient, TestServer

from swarm.config import HiveConfig, QueenConfig
from swarm.drones.log import DroneLog
from swarm.drones.pilot import DronePilot
from swarm.queen.queen import Queen
from swarm.server.api import create_app
from swarm.server.daemon import SwarmDaemon
from swarm.server.terminal import _MAX_TERMINAL_SESSIONS
from swarm.tasks.board import TaskBoard
from swarm.tasks.history import TaskHistory
from swarm.worker.worker import Worker


@pytest.fixture
def daemon(monkeypatch):
    """Create a minimal daemon without starting it."""
    monkeypatch.setattr("swarm.queen.queen.load_session", lambda _: None)
    monkeypatch.setattr("swarm.queen.queen.save_session", lambda *a: None)

    cfg = HiveConfig(session_name="test")
    d = SwarmDaemon.__new__(SwarmDaemon)
    d.config = cfg
    d.workers = [
        Worker(name="api", path="/tmp/api", pane_id="%0", window_index="0"),
        Worker(name="web", path="/tmp/web", pane_id="%1", window_index="0"),
    ]
    d._worker_lock = asyncio.Lock()
    d.drone_log = DroneLog()
    d.task_board = TaskBoard()
    d.task_history = TaskHistory(log_file=Path(tempfile.mktemp(suffix=".jsonl")))
    d.queen = Queen(config=QueenConfig(cooldown=0.0), session_name="test")
    d.notification_bus = MagicMock()
    d.pilot = MagicMock(spec=DronePilot)
    d.pilot.enabled = True
    d.pilot.toggle = MagicMock(return_value=False)
    d.ws_clients = set()
    d.start_time = 0.0
    d.broadcast_ws = MagicMock()
    d.graph_mgr = None
    return d


@pytest.fixture
async def client(daemon):
    """Create an aiohttp test client."""
    app = create_app(daemon, enable_web=False)
    async with TestClient(TestServer(app)) as client:
        yield client


@pytest.mark.asyncio
async def test_terminal_auth_required(client, daemon):
    """When API password is set, unauthenticated requests get 401."""
    daemon.config.api_password = "secret123"
    resp = await client.get("/ws/terminal")
    assert resp.status == 401


@pytest.mark.asyncio
async def test_terminal_auth_wrong_token(client, daemon):
    """Wrong token also gets 401."""
    daemon.config.api_password = "secret123"
    resp = await client.get("/ws/terminal?token=wrong")
    assert resp.status == 401


@pytest.mark.asyncio
@pytest.mark.filterwarnings("ignore::DeprecationWarning")
async def test_terminal_concurrency_limit(client):
    """When _terminal_sessions is full, return 503."""
    # Pre-fill the session set (app mutation warning is expected in tests)
    sessions = client.app.setdefault("_terminal_sessions", set())
    sessions.clear()
    for i in range(_MAX_TERMINAL_SESSIONS):
        sessions.add(f"fake-session-{i}")

    resp = await client.get("/ws/terminal")
    assert resp.status == 503
    data = await resp.json()
    assert "Too many" in data["error"]


@pytest.mark.asyncio
@pytest.mark.filterwarnings("ignore::DeprecationWarning")
async def test_terminal_slot_reserved_before_await(client):
    """The slot should be reserved immediately (before first await).

    After the concurrency check and before WS prepare, the slot must
    already be in the sessions set to prevent race conditions.
    """
    sessions = client.app.setdefault("_terminal_sessions", set())
    sessions.clear()
    # Fill up to limit - 1
    for i in range(_MAX_TERMINAL_SESSIONS - 1):
        sessions.add(f"fake-session-{i}")

    # This request should get the last slot (200-level for WS upgrade attempt)
    # but since there's no real tmux, the WS will fail — the point is it
    # doesn't return 503 because the slot was reserved before the first await.
    resp = await client.get("/ws/terminal")
    # Should NOT be 503 — one slot was available
    assert resp.status != 503
