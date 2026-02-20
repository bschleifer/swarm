"""PtyBridge — WebSocket-to-PTY bridge for the web terminal.

Replaces the old tmux grouped-session approach with direct PTY streaming.
Each WebSocket connects to a specific worker's process, receiving output
in real time and forwarding input directly.
"""

from __future__ import annotations

import hmac
import json

from aiohttp import web

from swarm.logging import get_logger
from swarm.pty.process import ProcessError

_log = get_logger("pty.bridge")

_MAX_TERMINAL_SESSIONS = 20


def _check_auth(request: web.Request) -> web.Response | None:
    """Return a 401 Response if auth fails, or None if auth passes."""
    from swarm.server.api import _get_api_password, _get_daemon

    daemon = _get_daemon(request)
    password = _get_api_password(daemon)
    if password:
        token = request.query.get("token", "")
        if not hmac.compare_digest(token, password):
            return web.Response(status=401, text="Unauthorized")
    return None


async def _handle_ws_message(msg: web.WSMessage, proc: object) -> bool:
    """Process a single WS message.  Returns False to break the loop."""
    if msg.type == web.WSMsgType.BINARY:
        try:
            await proc.send_keys(msg.data.decode("utf-8", errors="replace"), enter=False)
        except ProcessError:
            return False
    elif msg.type == web.WSMsgType.TEXT:
        try:
            payload = json.loads(msg.data)
            if payload.get("action") == "resize" or "cols" in payload:
                cols = int(payload.get("cols") or 80)
                rows = int(payload.get("rows") or 24)
                await proc.resize(cols, rows)
        except (ValueError, KeyError, ProcessError):
            pass
    elif msg.type in (web.WSMsgType.CLOSE, web.WSMsgType.ERROR):
        return False
    return True


async def handle_terminal_ws(request: web.Request) -> web.WebSocketResponse:
    """WebSocket endpoint for interactive terminal access to a worker.

    Query parameters:
        worker: Worker name (required for per-worker terminal).
        token: Auth token (when API password is set).

    If no ``worker`` is specified, returns 400.  The old full-session
    view is no longer supported (tmux grouped sessions removed).
    """
    from swarm.server.api import _get_daemon

    auth_err = _check_auth(request)
    if auth_err is not None:
        return auth_err

    daemon = _get_daemon(request)

    # --- Concurrency limit ---
    sessions: set = request.app.setdefault("_terminal_sessions", set())
    if len(sessions) >= _MAX_TERMINAL_SESSIONS:
        return web.json_response({"error": "Too many terminal sessions"}, status=503)

    worker_name = request.query.get("worker", "")
    if not worker_name:
        return web.json_response({"error": "Missing 'worker' query parameter"}, status=400)

    worker = daemon.get_worker(worker_name)
    if not worker:
        return web.json_response({"error": f"Worker '{worker_name}' not found"}, status=404)

    # Reserve slot before any await
    session_key = f"pty-{worker_name}-{id(request)}"
    sessions.add(session_key)

    ws = web.WebSocketResponse()
    await ws.prepare(request)
    daemon.terminal_ws_clients.add(ws)
    _log.info("terminal attach: worker=%s", worker_name)

    proc = worker.process

    try:
        # Send initial buffer snapshot so the client sees existing output
        snapshot = proc.buffer.snapshot()
        if snapshot:
            await ws.send_bytes(snapshot)

        # Subscribe for real-time output
        proc.subscribe_ws(ws)

        # I/O loop: forward WS messages to the worker's PTY
        async for msg in ws:
            if not await _handle_ws_message(msg, proc):
                break
    finally:
        proc.unsubscribe_ws(ws)
        daemon.terminal_ws_clients.discard(ws)
        sessions.discard(session_key)
        _log.info("terminal detached: worker=%s", worker_name)

        if not ws.closed:
            await ws.close()

    return ws
