"""Tests for config editor hot-reload functionality."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from swarm.buzz.log import BuzzLog
from swarm.buzz.pilot import BuzzPilot
from swarm.config import BuzzConfig, HiveConfig, NotifyConfig, QueenConfig
from swarm.queen.queen import Queen
from swarm.server.daemon import SwarmDaemon
from swarm.tasks.board import TaskBoard
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
    d.buzz_log = BuzzLog()
    d.task_board = TaskBoard()
    d.queen = Queen(config=QueenConfig(cooldown=30.0), session_name="test")
    d.notification_bus = MagicMock()
    d.pilot = MagicMock(spec=BuzzPilot)
    d.pilot.enabled = True
    d.pilot.buzz_config = cfg.buzz
    d.pilot._base_interval = cfg.buzz.poll_interval
    d.pilot._max_interval = cfg.buzz.max_idle_interval
    d.pilot.interval = cfg.buzz.poll_interval
    d.ws_clients = set()
    d.start_time = 0.0
    d._config_mtime = 0.0
    d._mtime_task = None
    d._broadcast_ws = MagicMock()
    return d


@pytest.mark.asyncio
async def test_hot_reload_updates_pilot(daemon):
    """reload_config should update pilot settings."""
    new_cfg = HiveConfig(
        session_name="test",
        buzz=BuzzConfig(
            poll_interval=20.0,
            max_idle_interval=60.0,
        ),
    )
    await daemon.reload_config(new_cfg)

    assert daemon.pilot.buzz_config.poll_interval == 20.0
    assert daemon.pilot._base_interval == 20.0
    assert daemon.pilot._max_interval == 60.0
    assert daemon.pilot.interval == 20.0


@pytest.mark.asyncio
async def test_hot_reload_updates_queen(daemon):
    """reload_config should update queen cooldown."""
    new_cfg = HiveConfig(
        session_name="test",
        queen=QueenConfig(cooldown=120.0, enabled=False),
    )
    await daemon.reload_config(new_cfg)

    assert daemon.queen.config.cooldown == 120.0
    assert daemon.queen.config.enabled is False


@pytest.mark.asyncio
async def test_hot_reload_broadcasts_ws(daemon):
    """reload_config should broadcast config_changed to WS clients."""
    new_cfg = HiveConfig(session_name="test")
    await daemon.reload_config(new_cfg)

    daemon._broadcast_ws.assert_called_with({"type": "config_changed"})
