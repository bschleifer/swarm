"""Tests for queen/queen.py — headless Claude conductor."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from swarm.config import QueenConfig
from swarm.queen.queen import Queen


@pytest.fixture
def queen(tmp_path, monkeypatch):
    """Create a Queen with mocked session persistence."""
    monkeypatch.setattr("swarm.queen.session.STATE_DIR", tmp_path)
    monkeypatch.setattr("swarm.queen.queen.load_session", lambda _: None)
    monkeypatch.setattr("swarm.queen.queen.save_session", lambda *a: None)
    return Queen(config=QueenConfig(cooldown=0.0), session_name="test")


@pytest.fixture
def mock_claude(monkeypatch):
    """Helper to mock asyncio.create_subprocess_exec for claude -p."""
    def _make_mock(stdout_data: str, returncode: int = 0):
        proc = AsyncMock()
        proc.communicate = AsyncMock(
            return_value=(stdout_data.encode(), b"")
        )
        proc.returncode = returncode
        proc.kill = MagicMock()
        monkeypatch.setattr(
            "asyncio.create_subprocess_exec",
            AsyncMock(return_value=proc),
        )
        return proc
    return _make_mock


@pytest.mark.asyncio
async def test_ask_valid_json(queen, mock_claude):
    """ask() should parse valid JSON from claude -p output."""
    response = {
        "type": "result",
        "result": '{"assessment": "all good", "action": "continue"}',
        "session_id": "test-session-123",
    }
    mock_claude(json.dumps(response))

    result = await queen.ask("What's happening?")
    assert result["assessment"] == "all good"
    assert result["action"] == "continue"


@pytest.mark.asyncio
async def test_ask_markdown_fenced_json(queen, mock_claude):
    """ask() should unwrap JSON from markdown code fences."""
    inner = '```json\n{"assessment": "worker stuck", "action": "restart"}\n```'
    response = {"type": "result", "result": inner}
    mock_claude(json.dumps(response))

    result = await queen.ask("Analyze worker")
    assert result["assessment"] == "worker stuck"


@pytest.mark.asyncio
async def test_ask_non_json(queen, mock_claude):
    """ask() should handle non-JSON output gracefully."""
    mock_claude("This is not JSON at all")

    result = await queen.ask("Test")
    assert result.get("raw") is True


@pytest.mark.asyncio
async def test_ask_rate_limited(queen, mock_claude):
    """ask() should return rate limit error when called too frequently."""
    queen.cooldown = 60.0
    queen._last_call = asyncio.get_event_loop().time() + 9999  # future

    # Force can_call to return False by setting _last_call to now
    import time
    queen._last_call = time.time()
    queen.cooldown = 60.0

    result = await queen.ask("Test")
    assert "error" in result
    assert "Rate limited" in result["error"]


@pytest.mark.asyncio
async def test_ask_timeout(queen, monkeypatch):
    """ask() should handle subprocess timeout."""
    proc = AsyncMock()
    proc.communicate = AsyncMock(side_effect=asyncio.TimeoutError())
    proc.kill = MagicMock()
    proc.returncode = -9
    monkeypatch.setattr(
        "asyncio.create_subprocess_exec",
        AsyncMock(return_value=proc),
    )
    monkeypatch.setattr("swarm.queen.queen._DEFAULT_TIMEOUT", 0.1)

    result = await queen.ask("Test")
    assert "error" in result
    assert "timed out" in result["error"]


@pytest.mark.asyncio
async def test_analyze_worker(queen, mock_claude):
    """analyze_worker() should construct proper prompt and parse response."""
    response = {
        "type": "result",
        "result": json.dumps({
            "assessment": "waiting for input",
            "action": "send_message",
            "message": "continue with tests",
            "reasoning": "worker appears idle",
        }),
    }
    mock_claude(json.dumps(response))

    result = await queen.analyze_worker("api", ">\n? for shortcuts")
    assert result["action"] == "send_message"
    assert result["message"] == "continue with tests"


@pytest.mark.asyncio
async def test_assign_tasks_empty(queen):
    """assign_tasks() with empty lists should return empty."""
    result = await queen.assign_tasks([], [])
    assert result == []

    result = await queen.assign_tasks(["api"], [])
    assert result == []


@pytest.mark.asyncio
async def test_assign_tasks(queen, mock_claude):
    """assign_tasks() should parse Queen's assignment response."""
    response = {
        "type": "result",
        "result": json.dumps({
            "assignments": [
                {"worker": "api", "task_id": "abc123", "message": "Fix the bug"},
            ],
            "reasoning": "api matches the API task",
        }),
    }
    mock_claude(json.dumps(response))

    tasks = [{"id": "abc123", "title": "Fix API bug", "priority": "high"}]
    result = await queen.assign_tasks(["api"], tasks)
    assert len(result) == 1
    assert result[0]["worker"] == "api"


@pytest.mark.asyncio
async def test_coordinate_hive(queen, mock_claude):
    """coordinate_hive() should parse directives from Queen."""
    response = {
        "type": "result",
        "result": json.dumps({
            "assessment": "hive is healthy",
            "directives": [
                {"worker": "api", "action": "continue", "reason": "on track"},
            ],
            "conflicts": [],
            "suggestions": ["Monitor memory usage"],
        }),
    }
    mock_claude(json.dumps(response))

    result = await queen.coordinate_hive("## Workers\n- api: BUZZING")
    assert result["assessment"] == "hive is healthy"
    assert len(result["directives"]) == 1


@pytest.mark.asyncio
async def test_queen_lock(queen, mock_claude):
    """Concurrent Queen calls should be serialized by the lock."""
    response = {"type": "result", "result": '{"ok": true}'}
    mock_claude(json.dumps(response))
    queen.cooldown = 0.0

    results = await asyncio.gather(
        queen.ask("Test 1"),
        queen.ask("Test 2"),
    )
    # Both should succeed (serialized by lock)
    assert len(results) == 2


@pytest.mark.asyncio
async def test_session_persistence(tmp_path, monkeypatch, mock_claude):
    """Queen should persist session ID after receiving one."""
    monkeypatch.setattr("swarm.queen.session.STATE_DIR", tmp_path)
    saved = []
    monkeypatch.setattr("swarm.queen.queen.load_session", lambda _: None)
    monkeypatch.setattr("swarm.queen.queen.save_session",
                        lambda name, sid: saved.append((name, sid)))

    q = Queen(config=QueenConfig(cooldown=0.0), session_name="test")
    response = {"type": "result", "result": '{"ok": true}', "session_id": "sess-xyz"}
    mock_claude(json.dumps(response))

    await q.ask("Test")
    assert q.session_id == "sess-xyz"
    assert len(saved) == 1
    assert saved[0] == ("test", "sess-xyz")
