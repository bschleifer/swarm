"""Regression tests for /api/hooks/* endpoints.

These tests lock in the fix for two bugs that lived together in hooks.py:

1. The handlers called the nonexistent `d.broadcast(...)` method — it was
   renamed to `broadcast_ws` but hooks.py was added after the rename with a
   stale copy of the call site. Every SessionEnd / lifecycle event hook
   from a managed worker was crashing with an AttributeError.

2. The event handler read `body["hook_event"]` but Claude Code's actual
   payload field is `hook_event_name`. Masked by bug #1 until now.

Both bugs are regression-tested here against a real `SwarmDaemon` built via
the shared ``make_daemon()`` factory and a real aiohttp TestClient.
"""

from __future__ import annotations

import pytest
from aiohttp.test_utils import TestClient, TestServer

from swarm.server.api import create_app
from tests.conftest import make_daemon

# Hook endpoints are exempt from session auth (see _SESSION_AUTH_EXEMPT in
# server/api.py), but CSRF middleware still requires X-Requested-With.
_HOOK_HEADERS = {"X-Requested-With": "TestClient"}


@pytest.fixture
def daemon(monkeypatch):
    return make_daemon(monkeypatch=monkeypatch)


@pytest.fixture
async def client(daemon):
    app = create_app(daemon, enable_web=False)
    async with TestClient(TestServer(app)) as c:
        yield c


# ---------------------------------------------------------------------------
# /api/hooks/approval — fast path (safe tool)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hook_approval_safe_tool(client):
    resp = await client.post(
        "/api/hooks/approval",
        json={"tool_name": "Read", "tool_input": {"file_path": "/tmp/foo"}},
        headers=_HOOK_HEADERS,
    )
    assert resp.status == 200
    body = await resp.json()
    assert body["decision"] == "approve"
    assert "safe" in body["reason"].lower()


@pytest.mark.asyncio
async def test_hook_approval_missing_tool_name(client):
    resp = await client.post(
        "/api/hooks/approval",
        json={"tool_input": {}},
        headers=_HOOK_HEADERS,
    )
    assert resp.status == 400


# ---------------------------------------------------------------------------
# /api/hooks/session-end — regression: d.broadcast → d.broadcast_ws
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hook_session_end_broadcasts_via_broadcast_ws(client, daemon):
    """SessionEnd hook must succeed and invoke broadcast_ws (not broadcast).

    Previously hooks.py called ``d.broadcast(...)`` which doesn't exist,
    producing ``AttributeError`` → 500. This test asserts the fix.
    """
    daemon.broadcast_ws.reset_mock()

    resp = await client.post(
        "/api/hooks/session-end",
        json={"session_id": "abc123", "cwd": "/tmp/api"},
        headers=_HOOK_HEADERS,
    )

    assert resp.status == 200
    body = await resp.json()
    assert body["status"] == "ok"

    # broadcast_ws must have been called with a hook_session_end payload
    # addressed to the "api" worker (matched via cwd).
    daemon.broadcast_ws.assert_called_once()
    payload = daemon.broadcast_ws.call_args.args[0]
    assert payload["type"] == "hook_session_end"
    assert payload["worker"] == "api"
    assert payload["session_id"] == "abc123"


@pytest.mark.asyncio
async def test_hook_session_end_unknown_worker_no_broadcast(client, daemon):
    """If cwd matches no worker, session_end should no-op and NOT crash."""
    daemon.broadcast_ws.reset_mock()
    resp = await client.post(
        "/api/hooks/session-end",
        json={"session_id": "abc123", "cwd": "/nowhere"},
        headers=_HOOK_HEADERS,
    )
    assert resp.status == 200
    daemon.broadcast_ws.assert_not_called()


# ---------------------------------------------------------------------------
# /api/hooks/event — regression: both broadcast and hook_event_name bugs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hook_event_reads_hook_event_name_and_broadcasts(client, daemon):
    """Event hook must read Claude Code's ``hook_event_name`` field and
    broadcast via ``broadcast_ws``. Locks in both bug #1 and bug #2.
    """
    daemon.broadcast_ws.reset_mock()

    resp = await client.post(
        "/api/hooks/event",
        json={"hook_event_name": "PreCompact", "cwd": "/tmp/api"},
        headers=_HOOK_HEADERS,
    )

    assert resp.status == 200
    body = await resp.json()
    assert body["status"] == "ok"

    daemon.broadcast_ws.assert_called_once()
    payload = daemon.broadcast_ws.call_args.args[0]
    assert payload["type"] == "hook_event"
    assert payload["hook_event"] == "PreCompact"  # not "unknown"
    assert payload["worker"] == "api"

    # PreCompact should have set worker.compacting = True
    api_worker = next(w for w in daemon.workers if w.name == "api")
    assert api_worker.compacting is True


@pytest.mark.asyncio
async def test_hook_event_post_compact_clears_compacting(client, daemon):
    """PostCompact should clear the compacting flag on the targeted worker."""
    daemon.broadcast_ws.reset_mock()
    api_worker = next(w for w in daemon.workers if w.name == "api")
    api_worker.compacting = True
    api_worker._context_warned = True

    resp = await client.post(
        "/api/hooks/event",
        json={"hook_event_name": "PostCompact", "cwd": "/tmp/api"},
        headers=_HOOK_HEADERS,
    )
    assert resp.status == 200
    assert api_worker.compacting is False
    assert api_worker._context_warned is False


@pytest.mark.asyncio
async def test_hook_event_tolerates_legacy_hook_event_field(client, daemon):
    """Fallback: if a caller sends the legacy ``hook_event`` key (not the
    Claude Code ``hook_event_name`` field), the handler should still resolve
    the event name rather than returning "unknown".
    """
    daemon.broadcast_ws.reset_mock()

    resp = await client.post(
        "/api/hooks/event",
        json={"hook_event": "SubagentStart", "cwd": "/tmp/web"},
        headers=_HOOK_HEADERS,
    )

    assert resp.status == 200
    daemon.broadcast_ws.assert_called_once()
    payload = daemon.broadcast_ws.call_args.args[0]
    assert payload["hook_event"] == "SubagentStart"
    assert payload["worker"] == "web"


@pytest.mark.asyncio
async def test_hook_event_unknown_event_does_not_crash(client, daemon):
    """Missing both field names should resolve to 'unknown' but still 200."""
    daemon.broadcast_ws.reset_mock()

    resp = await client.post(
        "/api/hooks/event",
        json={"cwd": "/tmp/api"},
        headers=_HOOK_HEADERS,
    )

    assert resp.status == 200
    daemon.broadcast_ws.assert_called_once()
    payload = daemon.broadcast_ws.call_args.args[0]
    assert payload["hook_event"] == "unknown"
