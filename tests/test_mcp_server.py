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
