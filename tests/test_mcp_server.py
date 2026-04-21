"""Integration tests for the MCP HTTP transports.

Focuses on the behaviours that can't be covered by the tool-schema unit
tests in ``test_mcp_tools.py`` — specifically the SSE streams and the
``initialize`` handshake.
"""

from __future__ import annotations

import asyncio
import json
import uuid

import pytest
from aiohttp.test_utils import TestClient, TestServer

from swarm.server.api import create_app
from tests.conftest import make_daemon

_HDR = {"X-Requested-With": "TestClient"}


@pytest.fixture
def daemon(monkeypatch):
    return make_daemon(monkeypatch=monkeypatch)


@pytest.fixture
async def client(daemon):
    app = create_app(daemon, enable_web=False)
    async with TestClient(TestServer(app)) as c:
        yield c


# ---------------------------------------------------------------------------
# initialize — capability advertisement
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_initialize_advertises_tools_list_changed(client):
    """Clients decide whether to listen for tools/list_changed based on
    the ``listChanged`` capability on initialize. Advertising it is what
    makes conformant clients keep an SSE subscription open at all."""
    resp = await client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "initialize"},
        headers=_HDR,
    )
    assert resp.status == 200
    body = await resp.json()
    caps = body["result"]["capabilities"]
    assert caps["tools"].get("listChanged") is True


# ---------------------------------------------------------------------------
# Streamable SSE (GET /mcp) — list_changed push on open
# ---------------------------------------------------------------------------


class _SseReader:
    """Stateful SSE reader — preserves bytes past one ``\\n\\n`` boundary
    so subsequent events coalesced into the same TCP chunk don't get
    dropped."""

    def __init__(self, resp):
        self._resp = resp
        self._buf = b""

    async def next_event(self, timeout: float = 2.0) -> tuple[str | None, str | None]:
        """Return (event_name, data_str). (None, None) on timeout/EOF."""
        try:
            async with asyncio.timeout(timeout):
                while b"\n\n" not in self._buf:
                    chunk = await self._resp.content.read(256)
                    if not chunk:
                        return None, None
                    self._buf += chunk
        except TimeoutError:
            return None, None
        block, self._buf = self._buf.split(b"\n\n", 1)
        event_name: str | None = None
        data: str | None = None
        for line in block.decode().splitlines():
            if line.startswith("event: "):
                event_name = line[len("event: ") :]
            elif line.startswith("data: "):
                data = line[len("data: ") :]
        return event_name, data


@pytest.mark.asyncio
async def test_streamable_sse_pushes_list_changed_on_connect(client):
    """Opening GET /mcp must immediately yield a list_changed notification."""
    resp = await client.get("/mcp", headers=_HDR)
    try:
        assert resp.status == 200
        reader = _SseReader(resp)
        event, data = await reader.next_event()
        assert event == "message", f"expected 'message' event, got {event!r}"
        assert data is not None
        payload = json.loads(data)
        assert payload.get("method") == "notifications/tools/list_changed"
        assert payload.get("jsonrpc") == "2.0"
        assert "id" not in payload  # notifications never carry an id
    finally:
        resp.close()


# ---------------------------------------------------------------------------
# Legacy SSE (GET /mcp/sse) — endpoint event THEN list_changed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_legacy_sse_sends_endpoint_then_list_changed(client):
    """The legacy SSE transport's first event must stay as ``endpoint`` —
    clients rely on it to discover the /mcp/message URL — but a
    list_changed push should follow so reconnect-after-reload still
    refreshes the schema."""
    resp = await client.get("/mcp/sse", headers=_HDR)
    try:
        assert resp.status == 200
        reader = _SseReader(resp)

        event1, data1 = await reader.next_event()
        assert event1 == "endpoint"
        assert data1 and "/mcp/message?session_id=" in data1

        event2, data2 = await reader.next_event()
        assert event2 == "message"
        assert data2 is not None
        payload = json.loads(data2)
        assert payload.get("method") == "notifications/tools/list_changed"
    finally:
        resp.close()


# ---------------------------------------------------------------------------
# Regression: tools/list payload carries the current schema
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tools_list_returns_current_in_memory_tools(client):
    """After the list_changed push tells a client to refresh, this is
    what they'll get — the in-memory ``TOOLS`` list for this daemon
    process. Locks in that tools/list actually serves the new schema
    and not some cached alternative."""
    req_id = uuid.uuid4().hex[:8]
    resp = await client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": req_id, "method": "tools/list"},
        headers=_HDR,
    )
    assert resp.status == 200
    body = await resp.json()
    tools = body["result"]["tools"]
    names = {t["name"] for t in tools}
    # The swarm MCP surface always includes these coordination primitives.
    assert {"swarm_complete_task", "swarm_task_status", "swarm_check_messages"} <= names


# ---------------------------------------------------------------------------
# Task #226: broadcast_tools_list_changed — server-initiated push to all
# currently connected MCP clients (not just ones opening a fresh stream).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_broadcast_pushes_list_changed_to_open_streamable_session(client):
    """Call ``broadcast_tools_list_changed`` while a streamable SSE stream
    is open — the connected client must receive the notification on that
    same stream. This is the path future hot-reload-of-tools code will
    use; today it's also called defensively on daemon startup.
    """
    from swarm.mcp import server as mcp_server

    resp = await client.get("/mcp", headers=_HDR)
    try:
        assert resp.status == 200
        reader = _SseReader(resp)
        # Drain the list_changed pushed on open so the next event we read
        # is unambiguously the one the broadcast emits.
        event, _ = await reader.next_event()
        assert event == "message"

        # Give aiohttp a moment to register the session in broadcast_subscribers
        # after prepare() returns.
        await asyncio.sleep(0.05)

        sent = await mcp_server.broadcast_tools_list_changed()
        assert sent >= 1

        event, data = await reader.next_event()
        assert event == "message"
        payload = json.loads(data)
        assert payload.get("method") == "notifications/tools/list_changed"
    finally:
        resp.close()


@pytest.mark.asyncio
async def test_broadcast_with_no_subscribers_is_safe(daemon):
    """Calling broadcast before any client connects must be a no-op, not
    a crash — daemon startup calls it defensively right after tool
    registration when there are no sessions yet."""
    from swarm.mcp import server as mcp_server

    # Snapshot + clear to isolate this test from whatever other tests left behind.
    snapshot = set(mcp_server._broadcast_subscribers)
    mcp_server._broadcast_subscribers.clear()
    try:
        sent = await mcp_server.broadcast_tools_list_changed()
        assert sent == 0
    finally:
        mcp_server._broadcast_subscribers.update(snapshot)


@pytest.mark.asyncio
async def test_broadcast_prunes_dead_subscribers(daemon):
    """A closed StreamResponse in the subscriber set must not raise and
    must be removed from the set so subsequent broadcasts don't keep
    hitting it."""
    from unittest.mock import AsyncMock, MagicMock

    from swarm.mcp import server as mcp_server

    dead = MagicMock()
    dead.write = AsyncMock(side_effect=ConnectionResetError("client gone"))
    snapshot = set(mcp_server._broadcast_subscribers)
    mcp_server._broadcast_subscribers.clear()
    mcp_server._broadcast_subscribers.add(dead)
    try:
        sent = await mcp_server.broadcast_tools_list_changed()
        assert sent == 0
        assert dead not in mcp_server._broadcast_subscribers
    finally:
        mcp_server._broadcast_subscribers.update(snapshot)


@pytest.mark.asyncio
async def test_reconnect_after_bounce_replays_list_changed(client):
    """Simulates the "daemon process bounced; client auto-reconnects"
    path. Each fresh GET /mcp must receive a list_changed on open so a
    client that cached a pre-bounce schema is pushed off it — this is
    task #226's Acceptance #3 (reconnect-on-bounce) as a regression test.
    """
    # First connection — open, read the push, close.
    resp1 = await client.get("/mcp", headers=_HDR)
    try:
        reader = _SseReader(resp1)
        event, data = await reader.next_event()
        assert event == "message"
        assert json.loads(data)["method"] == "notifications/tools/list_changed"
    finally:
        resp1.close()

    # Simulate reconnect — new GET, must independently receive list_changed.
    resp2 = await client.get("/mcp", headers=_HDR)
    try:
        reader = _SseReader(resp2)
        event, data = await reader.next_event()
        assert event == "message"
        assert json.loads(data)["method"] == "notifications/tools/list_changed"
    finally:
        resp2.close()


# ---------------------------------------------------------------------------
# Mcp-Session-Id validation — the real fix for stale tool schemas after
# daemon reload. Per MCP Streamable HTTP spec §8.4: when the server does
# not recognize a given session id, it MUST respond with 404, and the
# client MUST start a new session by sending a new InitializeRequest.
# Without this, Claude Code clients keep reusing their pre-restart
# session ID, never trigger a re-initialize, and serve stale tool
# schemas forever — the symptom behind this being the third fix attempt.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_initialize_registers_session_id(client):
    """The session ID we emit on initialize must be accepted on a
    follow-up request — basic positive path for session tracking."""
    resp = await client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "initialize"},
        headers=_HDR,
    )
    assert resp.status == 200
    session_id = resp.headers.get("Mcp-Session-Id")
    assert session_id, "initialize response must include Mcp-Session-Id"

    # Same session — server recognises it.
    follow_up = await client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        headers={**_HDR, "Mcp-Session-Id": session_id},
    )
    assert follow_up.status == 200


@pytest.mark.asyncio
async def test_unknown_session_id_on_non_initialize_returns_404(client):
    """An unknown session ID (e.g. cached from a pre-restart daemon) must
    get rejected with 404 + a JSON-RPC error payload so the client
    re-initializes instead of silently serving stale schemas.
    """
    resp = await client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 42, "method": "tools/list"},
        headers={**_HDR, "Mcp-Session-Id": "stale-from-old-daemon"},
    )
    assert resp.status == 404
    body = await resp.json()
    assert body.get("jsonrpc") == "2.0"
    assert body.get("id") == 42
    assert "error" in body
    # Code is JSON-RPC application error range; message names the cause.
    assert body["error"].get("code") == -32000
    assert "session" in body["error"].get("message", "").lower()


@pytest.mark.asyncio
async def test_unknown_session_id_on_initialize_is_allowed(client):
    """A client carrying a stale session ID may include it on its
    re-init request — that's expected when Claude Code reconnects after
    a bounce. We must NOT reject initialize; the whole point is to let
    the client establish a fresh session."""
    resp = await client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "initialize"},
        headers={**_HDR, "Mcp-Session-Id": "stale-from-old-daemon"},
    )
    assert resp.status == 200
    # A NEW session ID must be issued (not the stale one echoed back).
    new_session = resp.headers.get("Mcp-Session-Id")
    assert new_session
    assert new_session != "stale-from-old-daemon"


@pytest.mark.asyncio
async def test_missing_session_id_is_allowed(client):
    """Session-less clients (no Mcp-Session-Id header at all) are still
    accepted — session management is optional per spec, and this path
    keeps backward compatibility with MCP clients that don't implement
    it."""
    resp = await client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        headers=_HDR,
    )
    assert resp.status == 200


@pytest.mark.asyncio
async def test_delete_mcp_terminates_session(client):
    """A DELETE /mcp with a known session ID must remove it from the
    active set so the NEXT request on that session gets the 404 kick."""
    # Start a session.
    init = await client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "initialize"},
        headers=_HDR,
    )
    session_id = init.headers["Mcp-Session-Id"]

    # Confirm it's valid right now.
    ok = await client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        headers={**_HDR, "Mcp-Session-Id": session_id},
    )
    assert ok.status == 200

    # Terminate.
    delete = await client.delete("/mcp", headers={**_HDR, "Mcp-Session-Id": session_id})
    assert delete.status == 204

    # Reusing it now should get rejected.
    rejected = await client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 3, "method": "tools/list"},
        headers={**_HDR, "Mcp-Session-Id": session_id},
    )
    assert rejected.status == 404
