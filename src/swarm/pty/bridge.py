"""PtyBridge — WebSocket-to-PTY bridge for the web terminal.

Each WebSocket connects to a specific worker's process, receiving output
in real time and forwarding input directly.
"""

from __future__ import annotations

import hmac
import json
from typing import TYPE_CHECKING

from aiohttp import web

from swarm.logging import get_logger
from swarm.pty.process import ProcessError

if TYPE_CHECKING:
    from swarm.pty.process import WorkerProcess

_log = get_logger("pty.bridge")

_MAX_TERMINAL_SESSIONS = 20


def _check_auth(request: web.Request) -> web.Response | None:
    """Return a 401 Response if auth fails, or None if auth passes."""
    from swarm.server.api import _get_api_password
    from swarm.server.helpers import get_daemon

    daemon = get_daemon(request)
    password = _get_api_password(daemon)
    if password:
        token = request.query.get("token", "")
        if not hmac.compare_digest(token, password):
            return web.Response(status=401, text="Unauthorized")
    return None


async def _handle_ws_message(msg: web.WSMessage, proc: WorkerProcess) -> bool:
    """Process a single WS message.  Returns False to break the loop."""
    if msg.type == web.WSMsgType.BINARY:
        try:
            proc.mark_user_input()
            await proc.send_keys(msg.data.decode("utf-8", errors="replace"), enter=False)
        except ProcessError:
            return False
    elif msg.type == web.WSMsgType.TEXT:
        try:
            payload = json.loads(msg.data)
            if payload.get("action") == "resize" or "cols" in payload:
                cols = max(1, min(500, int(payload.get("cols") or 80)))
                rows = max(1, min(500, int(payload.get("rows") or 24)))
                await proc.resize(cols, rows)
        except (ValueError, KeyError, ProcessError):
            pass
    elif msg.type in (web.WSMsgType.CLOSE, web.WSMsgType.ERROR):
        return False
    return True


async def _send_initial_view(ws: web.WebSocketResponse, proc: WorkerProcess) -> None:
    """Send raw buffer snapshot so the client sees existing output."""
    # Atomic subscription and snapshot to avoid data loss/duplication
    snapshot = proc.subscribe_and_snapshot(ws)
    if snapshot:
        # Reset terminal attributes before the snapshot — the buffer may
        # start after a color/style sequence that set state we can't see.
        await ws.send_bytes(b"\x1b[0m" + snapshot)


def _validate_terminal_request(request: web.Request) -> tuple | web.Response:
    """Validate auth, concurrency, and worker.  Returns (daemon, worker, sessions) or Response."""
    from swarm.server.helpers import get_daemon as _get_daemon

    auth_err = _check_auth(request)
    if auth_err is not None:
        return auth_err

    daemon = _get_daemon(request)

    sessions: set = request.app.setdefault("_terminal_sessions", set())
    if len(sessions) >= _MAX_TERMINAL_SESSIONS:
        return web.json_response({"error": "Too many terminal sessions"}, status=503)

    worker_name = request.query.get("worker", "")
    if not worker_name:
        return web.json_response({"error": "Missing 'worker' query parameter"}, status=400)

    worker = daemon.get_worker(worker_name)
    if not worker:
        return web.json_response({"error": f"Worker '{worker_name}' not found"}, status=404)

    return daemon, worker, sessions


async def handle_terminal_ws(request: web.Request) -> web.WebSocketResponse:
    """WebSocket endpoint for interactive terminal access to a worker.

    Sends the raw buffer snapshot for immediate content, then subscribes
    to the live PTY output stream.
    """
    result = _validate_terminal_request(request)
    if isinstance(result, web.Response):
        return result
    daemon, worker, sessions = result

    session_key = f"pty-{worker.name}-{id(request)}"
    sessions.add(session_key)

    proc = worker.process
    if not proc:
        sessions.discard(session_key)
        return web.json_response({"error": "Worker has no active process"}, status=503)

    ws = web.WebSocketResponse(heartbeat=20.0)
    await ws.prepare(request)
    daemon.terminal_ws_clients.add(ws)
    proc.set_terminal_active(True)
    _log.info("terminal attach: worker=%s", worker.name)

    try:
        await _send_initial_view(ws, proc)

        async for msg in ws:
            if not await _handle_ws_message(msg, proc):
                break
    finally:
        proc.unsubscribe_ws(ws)
        if not proc._ws_subscribers:
            proc.set_terminal_active(False)
        daemon.terminal_ws_clients.discard(ws)
        sessions.discard(session_key)
        _log.info("terminal detached: worker=%s", worker.name)
        if not ws.closed:
            await ws.close()

    return ws
