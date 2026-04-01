"""MCP SSE server — JSON-RPC over Server-Sent Events.

Implements the MCP transport protocol:
- GET /mcp/sse — SSE stream for server→client messages
- POST /mcp/message — client→server JSON-RPC requests

Claude Code connects as an MCP client and calls tools defined in tools.py.
"""

from __future__ import annotations

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


def register(app: web.Application) -> None:
    """Register MCP SSE endpoints on the aiohttp application."""
    app.router.add_get("/mcp/sse", handle_sse)
    app.router.add_post("/mcp/message", handle_message)


# ---------------------------------------------------------------------------
# SSE connection management
# ---------------------------------------------------------------------------

# Active SSE connections: session_id → (worker_name, response)
_sessions: dict[str, tuple[str, web.StreamResponse]] = {}


async def handle_sse(request: web.Request) -> web.StreamResponse:
    """SSE endpoint — maintains a persistent connection for server→client msgs.

    The client sends JSON-RPC requests via POST /mcp/message with a
    session_id header. Responses are pushed back over this SSE stream.
    """
    worker_name = request.headers.get("X-Swarm-Worker", "unknown")
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

    # Send the endpoint URL as the first SSE event (MCP convention)
    endpoint = f"/mcp/message?session_id={session_id}"
    await _send_sse(response, "endpoint", endpoint)

    _sessions[session_id] = (worker_name, response)
    _log.info("MCP SSE connected: worker=%s session=%s", worker_name, session_id)

    try:
        # Keep connection alive until client disconnects
        async for msg in request.content:
            pass  # SSE is server→client; client sends via POST
    except Exception:
        pass
    finally:
        _sessions.pop(session_id, None)
        _log.info("MCP SSE disconnected: worker=%s", worker_name)

    return response


async def handle_message(request: web.Request) -> web.Response:
    """Handle a JSON-RPC message from the MCP client."""
    session_id = request.query.get("session_id", "")
    if not session_id or session_id not in _sessions:
        return web.json_response({"error": "invalid or missing session_id"}, status=400)

    worker_name, sse_response = _sessions[session_id]

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)

    # Route JSON-RPC methods
    method = body.get("method", "")
    msg_id = body.get("id")
    params = body.get("params", {})

    result = _dispatch(request, worker_name, method, params)

    # Send response via SSE
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
    return {
        "protocolVersion": _PROTOCOL_VERSION,
        "serverInfo": {"name": _SERVER_NAME, "version": _SERVER_VERSION},
        "capabilities": {"tools": {}},
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
