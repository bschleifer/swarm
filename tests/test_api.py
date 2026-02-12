"""Tests for server/api.py — REST + WebSocket API."""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp.test_utils import TestClient, TestServer

from swarm.drones.log import DroneLog
from swarm.drones.pilot import DronePilot
from swarm.config import GroupConfig, HiveConfig, QueenConfig, WorkerConfig
from swarm.queen.queen import Queen
from swarm.server.api import create_app
from swarm.server.daemon import SwarmDaemon
from swarm.server.analyzer import QueenAnalyzer
from swarm.server.proposals import ProposalManager
from swarm.tasks.board import TaskBoard
from swarm.tasks.history import TaskHistory
from swarm.tasks.proposal import AssignmentProposal, ProposalStore
from swarm.worker.worker import Worker, WorkerState

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
    d.task_history = TaskHistory(log_file=Path(tempfile.mktemp(suffix=".jsonl")))
    d.queen = Queen(config=QueenConfig(cooldown=0.0), session_name="test")
    d.proposal_store = ProposalStore()
    d.proposals = ProposalManager(d.proposal_store, d)
    d.analyzer = QueenAnalyzer(d.queen, d)
    d.notification_bus = MagicMock()
    d.pilot = MagicMock(spec=DronePilot)
    d.pilot.enabled = True
    d.pilot.toggle = MagicMock(return_value=False)
    d.ws_clients = set()
    d.start_time = 0.0
    d._broadcast_ws = MagicMock()
    d.send_to_worker = AsyncMock()
    d._prep_worker_for_task = AsyncMock()
    monkeypatch.setattr("swarm.tmux.cell.send_enter", AsyncMock())
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
async def test_unassign_task(client):
    # Create and assign a task
    resp = await client.post("/api/tasks", json={"title": "Test"}, headers=_API_HEADERS)
    data = await resp.json()
    task_id = data["id"]
    await client.post(f"/api/tasks/{task_id}/assign", json={"worker": "api"}, headers=_API_HEADERS)
    # Unassign it
    resp = await client.post(f"/api/tasks/{task_id}/unassign", headers=_API_HEADERS)
    assert resp.status == 200
    data = await resp.json()
    assert data["status"] == "unassigned"


@pytest.mark.asyncio
async def test_unassign_task_not_found(client):
    resp = await client.post("/api/tasks/nonexistent/unassign", headers=_API_HEADERS)
    assert resp.status == 404


@pytest.mark.asyncio
async def test_unassign_task_wrong_status(client):
    """Cannot unassign a pending task."""
    resp = await client.post("/api/tasks", json={"title": "Test"}, headers=_API_HEADERS)
    data = await resp.json()
    task_id = data["id"]
    resp = await client.post(f"/api/tasks/{task_id}/unassign", headers=_API_HEADERS)
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
async def test_rename_group(config_client):
    await config_client.post(
        "/api/config/groups",
        json={"name": "old-name", "workers": ["api"]},
        headers=_API_HEADERS,
    )
    resp = await config_client.put(
        "/api/config/groups/old-name",
        json={"name": "new-name", "workers": ["api", "web"]},
        headers=_API_HEADERS,
    )
    assert resp.status == 200
    data = await resp.json()
    assert data["group"] == "new-name"
    assert data["workers"] == ["api", "web"]


@pytest.mark.asyncio
async def test_rename_group_updates_default_group(daemon_with_path, tmp_path):
    daemon_with_path.config.groups = []
    daemon_with_path.config.default_group = "old-name"
    from swarm.config import GroupConfig

    daemon_with_path.config.groups.append(GroupConfig("old-name", ["api"]))
    app = create_app(daemon_with_path, enable_web=False)
    async with TestClient(TestServer(app)) as client:
        resp = await client.put(
            "/api/config/groups/old-name",
            json={"name": "new-name", "workers": ["api"]},
            headers=_API_HEADERS,
        )
        assert resp.status == 200
        assert daemon_with_path.config.default_group == "new-name"


@pytest.mark.asyncio
async def test_rename_group_duplicate(config_client):
    await config_client.post(
        "/api/config/groups",
        json={"name": "group-a", "workers": []},
        headers=_API_HEADERS,
    )
    await config_client.post(
        "/api/config/groups",
        json={"name": "group-b", "workers": []},
        headers=_API_HEADERS,
    )
    resp = await config_client.put(
        "/api/config/groups/group-a",
        json={"name": "group-b", "workers": []},
        headers=_API_HEADERS,
    )
    assert resp.status == 409


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


# --- Phase 2: New API endpoints ---


@pytest.mark.asyncio
async def test_worker_interrupt(client):
    with patch("swarm.tmux.cell.send_interrupt", new_callable=AsyncMock):
        resp = await client.post("/api/workers/api/interrupt", headers=_API_HEADERS)
        assert resp.status == 200
        data = await resp.json()
        assert data["status"] == "interrupted"


@pytest.mark.asyncio
async def test_worker_interrupt_not_found(client):
    resp = await client.post("/api/workers/nonexistent/interrupt", headers=_API_HEADERS)
    assert resp.status == 404


@pytest.mark.asyncio
async def test_worker_revive(client, daemon):
    daemon.workers[0].state = WorkerState.STUNG
    with patch("swarm.worker.manager.revive_worker", new_callable=AsyncMock):
        resp = await client.post("/api/workers/api/revive", headers=_API_HEADERS)
        assert resp.status == 200
        data = await resp.json()
        assert data["status"] == "revived"


@pytest.mark.asyncio
async def test_worker_revive_not_found(client):
    resp = await client.post("/api/workers/nonexistent/revive", headers=_API_HEADERS)
    assert resp.status == 404


@pytest.mark.asyncio
async def test_workers_launch(client, daemon):
    daemon.config.workers = [
        WorkerConfig("new1", "/tmp/new1"),
        WorkerConfig("new2", "/tmp/new2"),
    ]
    launched = [Worker(name="new1", path="/tmp/new1", pane_id="%5")]
    with patch("swarm.worker.manager.launch_hive", new_callable=AsyncMock, return_value=launched):
        resp = await client.post(
            "/api/workers/launch",
            json={"workers": ["new1"]},
            headers=_API_HEADERS,
        )
    assert resp.status == 201
    data = await resp.json()
    assert "new1" in data["launched"]


@pytest.mark.asyncio
async def test_workers_launch_empty(client, daemon):
    """When all workers are already running, return no_new_workers."""
    daemon.config.workers = [WorkerConfig("api", "/tmp/api")]
    resp = await client.post("/api/workers/launch", json={}, headers=_API_HEADERS)
    assert resp.status == 200
    data = await resp.json()
    assert data["status"] == "no_new_workers"


@pytest.mark.asyncio
async def test_workers_spawn(client, daemon):
    new_worker = Worker(name="spawned", path="/tmp/spawned", pane_id="%9")
    with patch(
        "swarm.worker.manager.add_worker_live", new_callable=AsyncMock, return_value=new_worker
    ):
        resp = await client.post(
            "/api/workers/spawn",
            json={"name": "spawned", "path": "/tmp/spawned"},
            headers=_API_HEADERS,
        )
    assert resp.status == 201
    data = await resp.json()
    assert data["worker"] == "spawned"


@pytest.mark.asyncio
async def test_workers_spawn_invalid(client):
    resp = await client.post(
        "/api/workers/spawn", json={"name": "", "path": "/tmp/x"}, headers=_API_HEADERS
    )
    assert resp.status == 400


@pytest.mark.asyncio
async def test_workers_continue_all(client, daemon):
    daemon.workers[0].state = WorkerState.RESTING
    with patch("swarm.tmux.cell.send_enter", new_callable=AsyncMock):
        resp = await client.post("/api/workers/continue-all", headers=_API_HEADERS)
    assert resp.status == 200
    data = await resp.json()
    assert data["count"] >= 1


@pytest.mark.asyncio
async def test_workers_send_all(client):
    with patch("swarm.tmux.cell.send_keys", new_callable=AsyncMock):
        resp = await client.post(
            "/api/workers/send-all",
            json={"message": "hello all"},
            headers=_API_HEADERS,
        )
    assert resp.status == 200
    data = await resp.json()
    assert data["count"] == 2


@pytest.mark.asyncio
async def test_workers_send_all_empty_msg(client):
    resp = await client.post("/api/workers/send-all", json={"message": ""}, headers=_API_HEADERS)
    assert resp.status == 400


@pytest.mark.asyncio
async def test_group_send(client, daemon):
    daemon.config.workers = [
        WorkerConfig("api", "/tmp/api"),
        WorkerConfig("web", "/tmp/web"),
    ]
    daemon.config.groups = [GroupConfig(name="backend", workers=["api"])]
    with patch("swarm.tmux.cell.send_keys", new_callable=AsyncMock):
        resp = await client.post(
            "/api/groups/backend/send",
            json={"message": "deploy"},
            headers=_API_HEADERS,
        )
    assert resp.status == 200
    data = await resp.json()
    assert data["count"] == 1


@pytest.mark.asyncio
async def test_group_send_empty_msg(client, daemon):
    daemon.config.groups = [GroupConfig(name="backend", workers=["api"])]
    resp = await client.post(
        "/api/groups/backend/send",
        json={"message": ""},
        headers=_API_HEADERS,
    )
    assert resp.status == 400


@pytest.mark.asyncio
async def test_worker_analyze(client, daemon, monkeypatch):
    # Ensure can_call returns True by setting _last_call far in the past
    daemon.queen._last_call = 0.0
    daemon.queen.cooldown = 0.0
    monkeypatch.setattr(
        daemon.queen, "analyze_worker", AsyncMock(return_value={"action": "continue"})
    )
    with patch("swarm.tmux.cell.capture_pane", new_callable=AsyncMock, return_value="output"):
        resp = await client.post("/api/workers/api/analyze", headers=_API_HEADERS)
    assert resp.status == 200
    data = await resp.json()
    assert data["action"] == "continue"


@pytest.mark.asyncio
async def test_worker_analyze_bypasses_cooldown(client, daemon, monkeypatch):
    """User-initiated analyze calls bypass the Queen cooldown."""
    import time

    daemon.queen._last_call = time.time()
    daemon.queen.cooldown = 9999.0
    monkeypatch.setattr(
        daemon.queen, "analyze_worker", AsyncMock(return_value={"assessment": "ok"})
    )
    with patch("swarm.tmux.cell.capture_pane", new_callable=AsyncMock, return_value="output"):
        resp = await client.post("/api/workers/api/analyze", headers=_API_HEADERS)
    assert resp.status == 200


@pytest.mark.asyncio
async def test_queen_coordinate(client, daemon, monkeypatch):
    daemon.queen._last_call = 0.0
    daemon.queen.cooldown = 0.0
    monkeypatch.setattr(daemon.queen, "coordinate_hive", AsyncMock(return_value={"plan": "done"}))
    with patch("swarm.tmux.cell.capture_pane", new_callable=AsyncMock, return_value="output"):
        resp = await client.post("/api/queen/coordinate", headers=_API_HEADERS)
    assert resp.status == 200
    data = await resp.json()
    assert data["plan"] == "done"


@pytest.mark.asyncio
async def test_queen_coordinate_bypasses_cooldown(client, daemon, monkeypatch):
    """User-initiated coordinate calls bypass the Queen cooldown."""
    import time

    daemon.queen._last_call = time.time()
    daemon.queen.cooldown = 9999.0
    monkeypatch.setattr(daemon.queen, "coordinate_hive", AsyncMock(return_value={"directives": []}))
    resp = await client.post("/api/queen/coordinate", headers=_API_HEADERS)
    assert resp.status == 200


@pytest.mark.asyncio
async def test_session_kill(client, daemon):
    with patch("swarm.tmux.hive.kill_session", new_callable=AsyncMock):
        resp = await client.post("/api/session/kill", headers=_API_HEADERS)
    assert resp.status == 200
    data = await resp.json()
    assert data["status"] == "killed"


@pytest.mark.asyncio
async def test_workers_discover(client, daemon):
    mock_workers = [Worker(name="found", path="/tmp/found", pane_id="%9")]
    with patch(
        "swarm.server.daemon.discover_workers",
        new_callable=AsyncMock,
        return_value=mock_workers,
    ):
        resp = await client.post("/api/workers/discover", headers=_API_HEADERS)
    assert resp.status == 200
    data = await resp.json()
    assert len(data["workers"]) == 1


@pytest.mark.asyncio
async def test_drones_poll(client, daemon):
    daemon.pilot.poll_once = AsyncMock(return_value=True)
    resp = await client.post("/api/drones/poll", headers=_API_HEADERS)
    assert resp.status == 200
    data = await resp.json()
    assert data["had_action"] is True


@pytest.mark.asyncio
async def test_drones_poll_no_pilot(client, daemon):
    daemon.pilot = None
    resp = await client.post("/api/drones/poll", headers=_API_HEADERS)
    assert resp.status == 400


@pytest.mark.asyncio
async def test_upload_standalone(client, daemon, tmp_path):
    """POST /api/uploads saves a file."""
    import aiohttp

    data = aiohttp.FormData()
    data.add_field("file", b"test content", filename="test.txt")
    resp = await client.post("/api/uploads", data=data, headers=_API_HEADERS)
    assert resp.status == 201
    body = await resp.json()
    assert "path" in body


# --- Proposals ---


@pytest.mark.asyncio
async def test_proposals_list(client, daemon):
    """GET /api/proposals returns pending proposals."""
    task = daemon.task_board.create(title="Fix bug")
    p = AssignmentProposal(worker_name="api", task_id=task.id, task_title=task.title)
    daemon.proposal_store.add(p)

    resp = await client.get("/api/proposals", headers=_API_HEADERS)
    assert resp.status == 200
    data = await resp.json()
    assert data["pending_count"] == 1
    assert len(data["proposals"]) == 1
    assert data["proposals"][0]["worker_name"] == "api"


@pytest.mark.asyncio
async def test_approve_proposal(client, daemon):
    """POST /api/proposals/{id}/approve assigns the task."""
    task = daemon.task_board.create(title="Fix bug")
    daemon.workers[0].state = WorkerState.RESTING
    p = AssignmentProposal(
        worker_name="api", task_id=task.id, task_title=task.title, message="Go fix it"
    )
    daemon.proposal_store.add(p)

    resp = await client.post(
        f"/api/proposals/{p.id}/approve",
        headers=_API_HEADERS,
    )
    assert resp.status == 200
    data = await resp.json()
    assert data["status"] == "approved"
    assert daemon.task_board.get(task.id).assigned_worker == "api"


@pytest.mark.asyncio
async def test_reject_proposal(client, daemon):
    """POST /api/proposals/{id}/reject rejects the proposal."""
    task = daemon.task_board.create(title="Fix bug")
    p = AssignmentProposal(worker_name="api", task_id=task.id, task_title=task.title)
    daemon.proposal_store.add(p)

    resp = await client.post(
        f"/api/proposals/{p.id}/reject",
        headers=_API_HEADERS,
    )
    assert resp.status == 200
    data = await resp.json()
    assert data["status"] == "rejected"
    assert len(daemon.proposal_store.pending) == 0


@pytest.mark.asyncio
async def test_reject_all_proposals(client, daemon):
    """POST /api/proposals/reject-all rejects all pending proposals."""
    t1 = daemon.task_board.create(title="Bug 1")
    t2 = daemon.task_board.create(title="Bug 2")
    daemon.proposal_store.add(
        AssignmentProposal(worker_name="api", task_id=t1.id, task_title=t1.title)
    )
    daemon.proposal_store.add(
        AssignmentProposal(worker_name="web", task_id=t2.id, task_title=t2.title)
    )

    resp = await client.post("/api/proposals/reject-all", headers=_API_HEADERS)
    assert resp.status == 200
    data = await resp.json()
    assert data["count"] == 2
    assert len(daemon.proposal_store.pending) == 0


@pytest.mark.asyncio
async def test_approve_proposal_not_found(client, daemon):
    resp = await client.post("/api/proposals/nonexistent/approve", headers=_API_HEADERS)
    assert resp.status == 404


@pytest.mark.asyncio
async def test_reject_proposal_not_found(client, daemon):
    resp = await client.post("/api/proposals/nonexistent/reject", headers=_API_HEADERS)
    assert resp.status == 404


# --- Approval rules + min_confidence in config API ---


@pytest.mark.asyncio
async def test_update_config_approval_rules(config_client):
    resp = await config_client.put(
        "/api/config",
        json={
            "drones": {
                "approval_rules": [
                    {"pattern": "^Allow", "action": "approve"},
                    {"pattern": "delete|remove", "action": "escalate"},
                ]
            }
        },
        headers=_API_HEADERS,
    )
    assert resp.status == 200
    data = await resp.json()
    rules = data["drones"]["approval_rules"]
    assert len(rules) == 2
    assert rules[0]["pattern"] == "^Allow"
    assert rules[1]["action"] == "escalate"


@pytest.mark.asyncio
async def test_update_config_approval_rules_invalid_regex(config_client):
    resp = await config_client.put(
        "/api/config",
        json={"drones": {"approval_rules": [{"pattern": "[bad", "action": "approve"}]}},
        headers=_API_HEADERS,
    )
    assert resp.status == 400
    data = await resp.json()
    assert "invalid regex" in data["error"]


@pytest.mark.asyncio
async def test_update_config_approval_rules_invalid_action(config_client):
    resp = await config_client.put(
        "/api/config",
        json={"drones": {"approval_rules": [{"pattern": ".*", "action": "deny"}]}},
        headers=_API_HEADERS,
    )
    assert resp.status == 400
    data = await resp.json()
    assert "action" in data["error"]


@pytest.mark.asyncio
async def test_update_config_min_confidence(config_client):
    resp = await config_client.put(
        "/api/config",
        json={"queen": {"min_confidence": 0.5}},
        headers=_API_HEADERS,
    )
    assert resp.status == 200
    data = await resp.json()
    assert data["queen"]["min_confidence"] == 0.5


@pytest.mark.asyncio
async def test_update_config_min_confidence_invalid(config_client):
    resp = await config_client.put(
        "/api/config",
        json={"queen": {"min_confidence": 1.5}},
        headers=_API_HEADERS,
    )
    assert resp.status == 400
    data = await resp.json()
    assert "min_confidence" in data["error"]


@pytest.mark.asyncio
async def test_update_config_workflows(config_client):
    from swarm.tasks.workflows import SKILL_COMMANDS, _DEFAULT_SKILL_COMMANDS

    try:
        resp = await config_client.put(
            "/api/config",
            json={"workflows": {"bug": "/my-fix", "chore": "/my-chore"}},
            headers=_API_HEADERS,
        )
        assert resp.status == 200
        data = await resp.json()
        assert data["workflows"]["bug"] == "/my-fix"
        assert data["workflows"]["chore"] == "/my-chore"
    finally:
        SKILL_COMMANDS.clear()
        SKILL_COMMANDS.update(_DEFAULT_SKILL_COMMANDS)


@pytest.mark.asyncio
async def test_update_config_workflows_invalid_type(config_client):
    resp = await config_client.put(
        "/api/config",
        json={"workflows": {"invalid_type": "/foo"}},
        headers=_API_HEADERS,
    )
    assert resp.status == 400
    data = await resp.json()
    assert "invalid_type" in data["error"]


@pytest.mark.asyncio
async def test_server_stop(daemon):
    """POST /api/server/stop triggers the shutdown event."""
    app = create_app(daemon, enable_web=False)
    shutdown = asyncio.Event()
    app["shutdown_event"] = shutdown
    async with TestClient(TestServer(app)) as c:
        resp = await c.post("/api/server/stop", headers=_API_HEADERS)
        # Read status before the connection may drop
        assert resp.status == 200
    assert shutdown.is_set()
