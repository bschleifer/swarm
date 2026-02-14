"""Tests for config editor hot-reload functionality."""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from swarm.drones.log import DroneLog
from swarm.drones.pilot import DronePilot
from swarm.config import DroneConfig, HiveConfig, QueenConfig
from swarm.queen.queen import Queen
from swarm.server.config_manager import ConfigManager
from swarm.server.worker_service import WorkerService
from swarm.server.daemon import SwarmDaemon
from swarm.tasks.board import TaskBoard
from swarm.tasks.history import TaskHistory
from swarm.worker.worker import Worker


@pytest.fixture
def daemon(monkeypatch):
    monkeypatch.setattr("swarm.queen.queen.load_session", lambda _: None)
    monkeypatch.setattr("swarm.queen.queen.save_session", lambda *a: None)

    cfg = HiveConfig(session_name="test")
    d = SwarmDaemon.__new__(SwarmDaemon)
    d.config = cfg
    d.workers = [Worker(name="api", path="/tmp/api", pane_id="%0")]
    d._worker_lock = asyncio.Lock()
    d.drone_log = DroneLog()
    d.task_board = TaskBoard()
    d.task_history = TaskHistory(log_file=Path(tempfile.mktemp(suffix=".jsonl")))
    d.queen = Queen(config=QueenConfig(cooldown=30.0), session_name="test")
    d.notification_bus = MagicMock()
    d.pilot = MagicMock(spec=DronePilot)
    d.pilot.enabled = True
    d.pilot.drone_config = cfg.drones
    d.pilot.interval = cfg.drones.poll_interval
    d.ws_clients = set()
    d.start_time = 0.0
    d._config_mtime = 0.0
    d._mtime_task = None
    d.broadcast_ws = MagicMock()
    d.graph_mgr = None
    d.config_mgr = ConfigManager(d)
    d.worker_svc = WorkerService(d)
    return d


@pytest.mark.asyncio
async def test_hot_reload_updates_pilot(daemon):
    """reload_config should update pilot settings."""
    new_cfg = HiveConfig(
        session_name="test",
        drones=DroneConfig(
            poll_interval=20.0,
            max_idle_interval=60.0,
        ),
    )
    await daemon.reload_config(new_cfg)

    assert daemon.pilot.drone_config.poll_interval == 20.0
    daemon.pilot.set_poll_intervals.assert_called_once_with(20.0, 60.0)
    assert daemon.pilot.interval == 20.0


@pytest.mark.asyncio
async def test_hot_reload_updates_queen(daemon):
    """reload_config should update queen cooldown."""
    new_cfg = HiveConfig(
        session_name="test",
        queen=QueenConfig(cooldown=120.0, enabled=False),
    )
    await daemon.reload_config(new_cfg)

    assert daemon.queen.cooldown == 120.0
    assert daemon.queen.enabled is False


@pytest.mark.asyncio
async def test_hot_reload_broadcasts_ws(daemon):
    """reload_config should broadcast config_changed to WS clients."""
    new_cfg = HiveConfig(session_name="test")
    await daemon.reload_config(new_cfg)

    daemon.broadcast_ws.assert_called_with({"type": "config_changed"})
