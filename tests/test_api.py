"""Tests for server/api.py — REST + WebSocket API."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp.test_utils import TestClient, TestServer

from swarm.drones.log import DroneLog
from swarm.drones.pilot import DronePilot
from swarm.config import HiveConfig, QueenConfig, WorkerConfig
from swarm.queen.queen import Queen
from swarm.server.api import create_app
from swarm.server.daemon import SwarmDaemon
from swarm.tasks.board import TaskBoard
from swarm.worker.worker import Worker

# Default headers for API requests (CSRF requires X-Requested-With)
_API_HEADERS = {"X-Requested-With": "TestClient"}


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
    d.drone_log = DroneLog()
    d.task_board = TaskBoard()
    d.queen = Queen(config=QueenConfig(cooldown=0.0), session_name="test")
    d.notification_bus = MagicMock()
    d.pilot = MagicMock(spec=DronePilot)
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
    resp = await client.post("/api/workers/api/send", json={"message": ""}, headers=_API_HEADERS)
    assert resp.status == 400


@pytest.mark.asyncio
async def test_worker_send_not_string(client):
    resp = await client.post("/api/workers/api/send", json={"message": 123}, headers=_API_HEADERS)
    assert resp.status == 400


@pytest.mark.asyncio
async def test_worker_continue(client):
    with patch("swarm.tmux.cell.send_enter", new_callable=AsyncMock):
        resp = await client.post("/api/workers/api/continue", headers=_API_HEADERS)
        assert resp.status == 200


@pytest.mark.asyncio
async def test_worker_kill(client, monkeypatch):
    with patch("swarm.worker.manager.kill_worker", new_callable=AsyncMock):
        resp = await client.post("/api/workers/api/kill", headers=_API_HEADERS)
        assert resp.status == 200
        data = await resp.json()
        assert data["status"] == "killed"


@pytest.mark.asyncio
async def test_drone_log(client):
    resp = await client.get("/api/drones/log")
    assert resp.status == 200
    data = await resp.json()
    assert "entries" in data


@pytest.mark.asyncio
async def test_drone_log_limit_capped(client):
    resp = await client.get("/api/drones/log?limit=99999")
    assert resp.status == 200


@pytest.mark.asyncio
async def test_drone_status(client):
    resp = await client.get("/api/drones/status")
    assert resp.status == 200
    data = await resp.json()
    assert "enabled" in data


@pytest.mark.asyncio
async def test_drone_toggle(client):
    resp = await client.post("/api/drones/toggle", headers=_API_HEADERS)
    assert resp.status == 200


@pytest.mark.asyncio
async def test_tasks_crud(client):
    # Create
    resp = await client.post("/api/tasks", json={"title": "Fix bug"}, headers=_API_HEADERS)
    assert resp.status == 201
    data = await resp.json()
    task_id = data["id"]

    # List
    resp = await client.get("/api/tasks")
    assert resp.status == 200
    data = await resp.json()
    assert len(data["tasks"]) == 1

    # Assign
    resp = await client.post(
        f"/api/tasks/{task_id}/assign", json={"worker": "api"}, headers=_API_HEADERS
    )
    assert resp.status == 200

    # Complete
    resp = await client.post(f"/api/tasks/{task_id}/complete", headers=_API_HEADERS)
    assert resp.status == 200


@pytest.mark.asyncio
async def test_create_task_invalid_title(client):
    resp = await client.post("/api/tasks", json={"title": ""}, headers=_API_HEADERS)
    assert resp.status == 400


@pytest.mark.asyncio
async def test_create_task_invalid_priority(client):
    resp = await client.post(
        "/api/tasks", json={"title": "Test", "priority": "mega"}, headers=_API_HEADERS
    )
    assert resp.status == 400


@pytest.mark.asyncio
async def test_assign_task_nonexistent_worker(client):
    # First create a task
    resp = await client.post("/api/tasks", json={"title": "Test"}, headers=_API_HEADERS)
    data = await resp.json()
    task_id = data["id"]

    # Assign to non-existent worker
    resp = await client.post(
        f"/api/tasks/{task_id}/assign", json={"worker": "nonexistent"}, headers=_API_HEADERS
    )
    assert resp.status == 404


@pytest.mark.asyncio
async def test_assign_task_not_found(client):
    resp = await client.post(
        "/api/tasks/nonexistent/assign", json={"worker": "api"}, headers=_API_HEADERS
    )
    assert resp.status == 404


@pytest.mark.asyncio
async def test_complete_task_not_found(client):
    resp = await client.post("/api/tasks/nonexistent/complete", headers=_API_HEADERS)
    assert resp.status == 404


@pytest.mark.asyncio
async def test_worker_kill_not_found(client):
    resp = await client.post("/api/workers/nonexistent/kill", headers=_API_HEADERS)
    assert resp.status == 404


# --- Config API ---


@pytest.fixture
def daemon_with_path(daemon, tmp_path):
    """Daemon with a source_path so save_config works."""
    daemon.config.source_path = str(tmp_path / "swarm.yaml")
    daemon.config.workers = [WorkerConfig("api", "/tmp/api"), WorkerConfig("web", "/tmp/web")]
    daemon.config.groups = []
    # Stub reload_config to just update config without starting async tasks
    daemon.reload_config = AsyncMock()
    return daemon


@pytest.fixture
async def config_client(daemon_with_path):
    app = create_app(daemon_with_path, enable_web=False)
    async with TestClient(TestServer(app)) as client:
        yield client


@pytest.mark.asyncio
async def test_get_config(config_client):
    resp = await config_client.get("/api/config")
    assert resp.status == 200
    data = await resp.json()
    assert "session_name" in data
    assert "drones" in data
    assert "queen" in data
    assert "notifications" in data
    assert "workers" in data


@pytest.mark.asyncio
async def test_update_config_drones(config_client):
    resp = await config_client.put(
        "/api/config",
        json={"drones": {"poll_interval": 15.0}},
        headers=_API_HEADERS,
    )
    assert resp.status == 200
    data = await resp.json()
    assert data["drones"]["poll_interval"] == 15.0


@pytest.mark.asyncio
async def test_update_config_validation(config_client):
    resp = await config_client.put(
        "/api/config",
        json={"drones": {"poll_interval": "not_a_number"}},
        headers=_API_HEADERS,
    )
    assert resp.status == 400


@pytest.mark.asyncio
async def test_add_worker_api(config_client, tmp_path):
    """Add a worker with a valid path."""
    worker_dir = tmp_path / "new-project"
    worker_dir.mkdir()
    with patch("swarm.worker.manager.add_worker_live", new_callable=AsyncMock) as mock_add:
        mock_add.return_value = Worker(name="new-proj", path=str(worker_dir), pane_id="%5")
        resp = await config_client.post(
            "/api/config/workers",
            json={"name": "new-proj", "path": str(worker_dir)},
            headers=_API_HEADERS,
        )
        assert resp.status == 201
        data = await resp.json()
        assert data["worker"] == "new-proj"


@pytest.mark.asyncio
async def test_add_worker_duplicate(config_client, tmp_path):
    worker_dir = tmp_path / "api"
    worker_dir.mkdir()
    resp = await config_client.post(
        "/api/config/workers",
        json={"name": "api", "path": str(worker_dir)},
        headers=_API_HEADERS,
    )
    assert resp.status == 409


@pytest.mark.asyncio
async def test_remove_worker_api(config_client):
    with patch("swarm.worker.manager.kill_worker", new_callable=AsyncMock):
        resp = await config_client.delete("/api/config/workers/api", headers=_API_HEADERS)
        assert resp.status == 200
        data = await resp.json()
        assert data["status"] == "removed"


@pytest.mark.asyncio
async def test_add_group(config_client):
    resp = await config_client.post(
        "/api/config/groups",
        json={"name": "team", "workers": ["api", "web"]},
        headers=_API_HEADERS,
    )
    assert resp.status == 201


@pytest.mark.asyncio
async def test_update_group(config_client):
    # First add a group
    await config_client.post(
        "/api/config/groups",
        json={"name": "team", "workers": ["api"]},
        headers=_API_HEADERS,
    )
    # Then update it
    resp = await config_client.put(
        "/api/config/groups/team",
        json={"workers": ["api", "web"]},
        headers=_API_HEADERS,
    )
    assert resp.status == 200
    data = await resp.json()
    assert data["workers"] == ["api", "web"]


@pytest.mark.asyncio
async def test_remove_group(config_client):
    await config_client.post(
        "/api/config/groups",
        json={"name": "disposable", "workers": []},
        headers=_API_HEADERS,
    )
    resp = await config_client.delete("/api/config/groups/disposable", headers=_API_HEADERS)
    assert resp.status == 200


@pytest.mark.asyncio
async def test_config_auth_required(daemon_with_path, tmp_path):
    """When api_password is set, mutating config endpoints require auth."""
    daemon_with_path.config.api_password = "secret"
    app = create_app(daemon_with_path, enable_web=False)
    async with TestClient(TestServer(app)) as client:
        resp = await client.put(
            "/api/config",
            json={"drones": {"poll_interval": 5.0}},
            headers=_API_HEADERS,
        )
        assert resp.status == 401


@pytest.mark.asyncio
async def test_config_auth_pass(daemon_with_path, tmp_path):
    """Correct password passes auth check."""
    daemon_with_path.config.api_password = "secret"
    app = create_app(daemon_with_path, enable_web=False)
    async with TestClient(TestServer(app)) as client:
        resp = await client.put(
            "/api/config",
            json={"drones": {"poll_interval": 5.0}},
            headers={**_API_HEADERS, "Authorization": "Bearer secret"},
        )
        assert resp.status == 200


@pytest.mark.asyncio
async def test_list_projects(config_client, tmp_path):
    """GET /api/config/projects lists git repos."""
    # projects_dir is ~/projects by default, may not have repos — just check 200
    resp = await config_client.get("/api/config/projects")
    assert resp.status == 200
    data = await resp.json()
    assert "projects" in data
