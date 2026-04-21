"""MCP server — Streamable HTTP + legacy SSE transport.

Implements the MCP transport protocol:
- POST /mcp  — Streamable HTTP (current standard, no OAuth trigger)
- GET  /mcp  — SSE stream for Streamable HTTP server-initiated messages
- GET  /mcp/sse — Legacy SSE transport (deprecated, triggers OAuth in Claude Code)
- POST /mcp/message — Legacy SSE message endpoint

Claude Code connects as an MCP client and calls tools defined in tools.py.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import TYPE_CHECKING, Any

from aiohttp import web

from swarm.logging import get_logger
from swarm.mcp.tools import TOOLS, handle_tool_call

if TYPE_CHECKING:
    from swarm.server.daemon import SwarmDaemon

_log = get_logger("mcp.server")

# MCP protocol version
_PROTOCOL_VERSION = "2024-11-05"
_SERVER_NAME = "swarm"
_SERVER_VERSION = "1.0.0"

# How often the streamable SSE handler polls its transport for disconnect.
# Small enough that broadcast-while-connected tests complete quickly;
# large enough that healthy long-lived connections aren't churning CPU.
_SSE_KEEPALIVE_POLL = 0.5


def register(app: web.Application) -> None:
    """Register MCP endpoints on the aiohttp application."""
    # Streamable HTTP (current standard)
    app.router.add_post("/mcp", handle_streamable_http)
    app.router.add_get("/mcp", handle_streamable_sse)
    app.router.add_delete("/mcp", handle_streamable_delete)

    # Legacy SSE transport (kept for backward compat)
    app.router.add_get("/mcp/sse", handle_sse)
    app.router.add_post("/mcp/message", handle_message)


# ---------------------------------------------------------------------------
# Streamable HTTP transport (POST /mcp)
# ---------------------------------------------------------------------------


async def handle_streamable_http(request: web.Request) -> web.Response:
    """Handle a JSON-RPC request via Streamable HTTP.

    The client POSTs JSON-RPC to /mcp and receives the response directly
    in the HTTP response body. No persistent SSE connection needed.
    """
    # Identify worker: query param (from per-worker .mcp.json) > header > unknown
    worker_name = (
        request.rel_url.query.get("worker") or request.headers.get("X-Swarm-Worker") or "unknown"
    )
    session_id = request.headers.get("Mcp-Session-Id", "")

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)

    method = body.get("method", "")
    msg_id = body.get("id")
    params = body.get("params", {})

    # Session validation — the core mechanism that makes tool-schema
    # changes propagate to connected workers on daemon reload. See the
    # comment on ``_active_session_ids`` above. Policy:
    #   * ``initialize`` is always allowed (clients MUST be able to
    #     re-establish a session, especially carrying a stale ID from a
    #     previous daemon).
    #   * Missing / empty ``Mcp-Session-Id`` is allowed (spec says session
    #     management is optional — don't break session-less clients).
    #   * Anything else with an unknown ID gets 404, per MCP spec §8.4,
    #     and Claude Code re-initializes.
    if session_id and session_id not in _active_session_ids and method != "initialize":
        _log.info(
            "MCP session unknown: worker=%s session=%s method=%s — returning 404",
            worker_name,
            session_id,
            method,
        )
        rpc_err: dict[str, Any] = {
            "jsonrpc": "2.0",
            "id": msg_id,
            "error": {"code": -32000, "message": "session_not_found"},
        }
        return web.json_response(rpc_err, status=404)

    # Handle notifications (no id) — just acknowledge
    if msg_id is None:
        if method == "notifications/initialized":
            _log.info("MCP initialized: worker=%s session=%s", worker_name, session_id)
        return web.Response(status=204)

    result = _dispatch(request, worker_name, method, params)

    # Build JSON-RPC response
    rpc_response: dict[str, Any] = {"jsonrpc": "2.0", "id": msg_id}
    if isinstance(result, dict) and "error" in result:
        rpc_response["error"] = result["error"]
    else:
        rpc_response["result"] = result

    # For initialize, include session ID in response header AND register it.
    headers: dict[str, str] = {}
    if method == "initialize":
        new_session = uuid.uuid4().hex[:16]
        _active_session_ids.add(new_session)
        headers["Mcp-Session-Id"] = new_session
        _log.info("MCP session created: worker=%s session=%s", worker_name, new_session)

    return web.json_response(rpc_response, headers=headers)


async def handle_streamable_sse(request: web.Request) -> web.StreamResponse:
    """GET /mcp — SSE stream for server-initiated messages (Streamable HTTP).

    As soon as the stream opens we push one ``notifications/tools/list_changed``
    JSON-RPC notification. If the client connected after a daemon reload,
    any schema it cached from the previous process is now stale; receiving
    this notification makes the client re-call ``tools/list`` without
    needing its host session to restart.

    The response is also registered in ``_broadcast_subscribers`` so
    :func:`broadcast_tools_list_changed` can push additional
    notifications to it later — the hook future hot-reload code will
    use to notify already-connected clients without making them
    reconnect.
    """
    response = web.StreamResponse(
        status=200,
        reason="OK",
        headers={
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        },
    )
    await response.prepare(request)

    _broadcast_subscribers.add(response)
    try:
        try:
            await _push_tools_list_changed(response)
        except Exception:
            _log.debug("streamable SSE: list_changed push failed", exc_info=True)

        # Hold the handler open until the client disconnects so
        # ``broadcast_tools_list_changed`` can push additional
        # notifications on this same stream. ``request.content`` is
        # useless for a body-less GET (EOFs immediately in some
        # transport implementations), so poll the underlying transport
        # instead. Broken by aiohttp cancelling the handler on
        # disconnect — caught and translated to a clean exit.
        try:
            while True:
                transport = request.transport
                if transport is None or transport.is_closing():
                    break
                await asyncio.sleep(_SSE_KEEPALIVE_POLL)
        except asyncio.CancelledError:
            _log.debug("streamable SSE: handler cancelled (client disconnect)")
            raise
        except Exception:
            _log.debug("streamable SSE stream ended", exc_info=True)
    finally:
        _broadcast_subscribers.discard(response)

    return response


async def handle_streamable_delete(request: web.Request) -> web.Response:
    """DELETE /mcp — session teardown (Streamable HTTP).

    Removes the session ID from the active set so a subsequent request
    with the same ID will be rejected with 404. A client that sends
    DELETE is explicitly disposing of its session; honouring the
    termination lets the paired 404-on-stale-ID path stay consistent.
    """
    session_id = request.headers.get("Mcp-Session-Id", "")
    if session_id:
        _active_session_ids.discard(session_id)
    return web.Response(status=204)


# ---------------------------------------------------------------------------
# Legacy SSE transport
# ---------------------------------------------------------------------------

# Active legacy SSE connections: session_id → (worker_name, response).
# Used by the legacy SSE transport to route POSTed JSON-RPC messages back
# to the right client stream.
_sessions: dict[str, tuple[str, web.StreamResponse]] = {}

# Every currently-open SSE response that should receive server-initiated
# messages — both the Streamable HTTP GET /mcp and the legacy GET /mcp/sse
# register here. This is the set ``broadcast_tools_list_changed()``
# iterates. A separate, transport-agnostic collection so callers don't
# have to care which transport a session is using.
_broadcast_subscribers: set[web.StreamResponse] = set()

# Active Streamable HTTP session IDs issued by this daemon process. Per
# MCP Streamable HTTP spec §8.4, the server MUST respond with 404 to any
# request carrying a session ID it does not recognise; the client then
# starts a new session by sending a new InitializeRequest. Keeping this
# set in-process (not persisted) is deliberate — when the daemon
# os.execv's, every session ID issued by the old process becomes
# unknown to the new one, which forces every connected Claude Code
# client to re-initialize. That's the actual mechanism that propagates
# tool-schema changes to existing workers; the listChanged capability
# and the SSE list_changed push are both downstream of it. Previously
# this was unvalidated — clients kept reusing their pre-restart session
# IDs silently and served stale schemas forever.
_active_session_ids: set[str] = set()


async def handle_sse(request: web.Request) -> web.StreamResponse:
    """Legacy SSE endpoint — persistent connection for server→client msgs."""
    worker_name = (
        request.rel_url.query.get("worker") or request.headers.get("X-Swarm-Worker") or "unknown"
    )
    session_id = uuid.uuid4().hex[:16]

    response = web.StreamResponse(
        status=200,
        reason="OK",
        headers={
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Swarm-Session": session_id,
        },
    )
    await response.prepare(request)

    # Register session BEFORE sending the endpoint event — the client may
    # POST immediately after receiving the URL, causing a race if we register after.
    _sessions[session_id] = (worker_name, response)
    _broadcast_subscribers.add(response)

    # Send the endpoint URL as the first SSE event (MCP convention).
    scheme = request.headers.get("X-Forwarded-Proto", request.scheme)
    host = request.headers.get("X-Forwarded-Host", request.host)
    endpoint = f"{scheme}://{host}/mcp/message?session_id={session_id}"
    await _send_sse(response, "endpoint", endpoint)

    # Nudge the client to re-fetch tools/list. Cheap for clients already
    # in sync (one redundant fetch); essential for clients whose cached
    # schema is from before a daemon reload.
    try:
        await _push_tools_list_changed(response)
    except Exception:
        _log.debug("legacy SSE: list_changed push failed", exc_info=True)

    _log.info("MCP SSE connected: worker=%s session=%s", worker_name, session_id)

    try:
        async for msg in request.content:
            pass
    except Exception:
        _log.debug("legacy SSE stream ended: worker=%s", worker_name, exc_info=True)
    finally:
        _sessions.pop(session_id, None)
        _broadcast_subscribers.discard(response)
        _log.info("MCP SSE disconnected: worker=%s", worker_name)

    return response


async def handle_message(request: web.Request) -> web.Response:
    """Handle a JSON-RPC message from the legacy MCP SSE client."""
    session_id = request.query.get("session_id", "")
    if not session_id or session_id not in _sessions:
        return web.json_response({"error": "invalid or missing session_id"}, status=400)

    worker_name, sse_response = _sessions[session_id]

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)

    method = body.get("method", "")
    msg_id = body.get("id")
    params = body.get("params", {})

    result = _dispatch(request, worker_name, method, params)

    rpc_response: dict[str, Any] = {"jsonrpc": "2.0", "id": msg_id}
    if isinstance(result, dict) and "error" in result:
        rpc_response["error"] = result["error"]
    else:
        rpc_response["result"] = result

    await _send_sse(sse_response, "message", json.dumps(rpc_response))
    return web.Response(status=202, text="Accepted")


# ---------------------------------------------------------------------------
# JSON-RPC method dispatch
# ---------------------------------------------------------------------------


def _dispatch(
    request: web.Request,
    worker_name: str,
    method: str,
    params: dict[str, Any],
) -> Any:
    """Dispatch a JSON-RPC method call."""
    from swarm.server.helpers import get_daemon

    daemon = get_daemon(request)

    if method == "initialize":
        return _handle_initialize()
    if method == "tools/list":
        return _handle_tools_list()
    if method == "tools/call":
        return _handle_tools_call(daemon, worker_name, params)
    if method == "ping":
        return {}

    return {"error": {"code": -32601, "message": f"Method not found: {method}"}}


def _handle_initialize() -> dict[str, Any]:
    # Advertise tools.listChanged so clients know they can subscribe to
    # server-initiated refresh notifications. When the daemon reloads, any
    # previously-cached schema on the client side goes stale (observed in
    # the wild: Claude Code sessions holding a pre-reload ``tools/list``
    # result even after the MCP connection cycled). Pairing this with the
    # SSE-connect push of ``notifications/tools/list_changed`` below lets
    # conformant clients re-fetch without restarting their session.
    return {
        "protocolVersion": _PROTOCOL_VERSION,
        "serverInfo": {"name": _SERVER_NAME, "version": _SERVER_VERSION},
        "capabilities": {"tools": {"listChanged": True}},
    }


def _handle_tools_list() -> dict[str, Any]:
    return {"tools": TOOLS}


def _handle_tools_call(
    daemon: SwarmDaemon,
    worker_name: str,
    params: dict[str, Any],
) -> dict[str, Any]:
    tool_name = params.get("name", "")
    arguments = params.get("arguments", {})
    content = handle_tool_call(daemon, worker_name, tool_name, arguments)
    return {"content": content}


# ---------------------------------------------------------------------------
# SSE helpers
# ---------------------------------------------------------------------------


async def _send_sse(response: web.StreamResponse, event: str, data: str) -> None:
    """Send a single SSE event."""
    payload = f"event: {event}\ndata: {data}\n\n"
    await response.write(payload.encode("utf-8"))


async def _push_tools_list_changed(response: web.StreamResponse) -> None:
    """Push an MCP ``notifications/tools/list_changed`` JSON-RPC message.

    Sent once per SSE connection open. Clients that subscribe to the stream
    after a daemon reload get prompted to re-fetch ``tools/list`` instead
    of silently serving their stale cache (the exact failure mode that
    hid task #169's schema change until the operator reconnected).
    """
    notification = json.dumps({"jsonrpc": "2.0", "method": "notifications/tools/list_changed"})
    await _send_sse(response, "message", notification)


async def broadcast_tools_list_changed() -> int:
    """Push ``notifications/tools/list_changed`` to every connected MCP client.

    Complements the "push on connect" behaviour: that handles clients
    joining AFTER a registry change; this handles clients already
    subscribed when the registry changes at runtime. Today swarm's
    ``TOOLS`` is built at import time, so the main caller is
    :meth:`SwarmDaemon.start` right after daemon startup — a defensive
    broadcast for any session that raced connect with tool registration.
    Future hot-reload-of-tools paths can call this whenever they mutate
    the registry.

    Returns the number of subscribers that successfully received the
    notification. Dead / closed streams are pruned from
    ``_broadcast_subscribers`` in place so repeat calls don't keep
    retrying them.
    """
    if not _broadcast_subscribers:
        return 0
    sent = 0
    dead: list[web.StreamResponse] = []
    for response in list(_broadcast_subscribers):
        try:
            await _push_tools_list_changed(response)
        except Exception:
            _log.debug("broadcast list_changed: push failed, pruning", exc_info=True)
            dead.append(response)
            continue
        sent += 1
    for response in dead:
        _broadcast_subscribers.discard(response)
    if sent:
        _log.info("broadcast list_changed to %d MCP subscriber(s)", sent)
    return sent
