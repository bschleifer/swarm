"""Tests for swarm.pty.bridge — WebSocket-to-PTY bridge functions."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import WSMessage, web

from swarm.config import HiveConfig
from swarm.pty.bridge import (
    _MAX_TERMINAL_SESSIONS,
    _check_auth,
    _handle_ws_message,
    _send_initial_view,
    _validate_terminal_request,
    handle_terminal_ws,
)
from swarm.worker.worker import Worker
from tests.fakes.process import FakeWorkerProcess

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_request(
    *,
    query: dict[str, str] | None = None,
    app: dict | None = None,
    daemon: MagicMock | None = None,
    headers: dict[str, str] | None = None,
) -> MagicMock:
    """Build a mock aiohttp request with the given query params and app dict."""
    request = MagicMock(spec=web.Request)
    request.query = query or {}
    request.headers = headers or {}
    if app is None:
        app = {}
    if daemon is not None:
        app["daemon"] = daemon
    # Use a real dict so setdefault works
    request.app = app
    return request


def _make_daemon(
    *,
    api_password: str | None = None,
    workers: list[Worker] | None = None,
) -> MagicMock:
    """Build a mock SwarmDaemon with optional password and workers."""
    daemon = MagicMock()
    daemon.config = HiveConfig(session_name="test", api_password=api_password)
    daemon.terminal_ws_clients = set()
    _workers = workers or []

    def _get_worker(name: str) -> Worker | None:
        for w in _workers:
            if w.name == name:
                return w
        return None

    daemon.get_worker = MagicMock(side_effect=_get_worker)
    return daemon


def _make_ws_msg(
    msg_type: web.WSMsgType,
    data: bytes | str | None = None,
) -> MagicMock:
    """Build a mock aiohttp WSMessage."""
    msg = MagicMock(spec=WSMessage)
    msg.type = msg_type
    msg.data = data
    return msg


# ---------------------------------------------------------------------------
# _check_auth
# ---------------------------------------------------------------------------


@patch("swarm.server.api._get_api_password")
@patch("swarm.server.helpers.get_daemon")
def test_check_auth_no_password(
    mock_get_daemon: MagicMock,
    mock_get_pw: MagicMock,
):
    """When the correct token is provided, _check_auth returns None (pass)."""
    daemon = _make_daemon(api_password="auto-token")
    mock_get_daemon.return_value = daemon
    mock_get_pw.return_value = "auto-token"

    request = _make_request(query={"token": "auto-token"}, daemon=daemon)
    result = _check_auth(request)
    assert result is None


def test_check_auth_passes_without_origin():
    """Without an Origin header, _check_auth returns None (pass).

    Token auth is now handled post-connect via first-message auth,
    so _check_auth only validates the origin header.
    """
    request = _make_request()
    result = _check_auth(request)
    assert result is None


def test_check_auth_ignores_token():
    """_check_auth no longer validates tokens — auth is post-connect."""
    request = _make_request(query={"token": "wrong"})
    result = _check_auth(request)
    assert result is None


# ---------------------------------------------------------------------------
# _handle_ws_message
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handle_ws_message_binary_forwards_to_proc():
    """Binary WS message should call send_keys on the process."""
    proc = FakeWorkerProcess(name="w1")
    msg = _make_ws_msg(web.WSMsgType.BINARY, data=b"hello")

    result = await _handle_ws_message(msg, None, proc)

    assert result is True
    assert "hello" in proc.keys_sent


@pytest.mark.asyncio
async def test_handle_ws_message_binary_marks_user_input():
    """Binary WS message should mark user input timestamp."""
    proc = FakeWorkerProcess(name="w1")
    msg = _make_ws_msg(web.WSMsgType.BINARY, data=b"x")

    old_ts = proc._last_user_input
    await _handle_ws_message(msg, None, proc)
    assert proc._last_user_input > old_ts


@pytest.mark.asyncio
async def test_handle_ws_message_text_resize():
    """Text WS message with resize action should call proc.resize."""
    proc = FakeWorkerProcess(name="w1")
    payload = json.dumps({"action": "resize", "cols": 120, "rows": 40})
    msg = _make_ws_msg(web.WSMsgType.TEXT, data=payload)

    result = await _handle_ws_message(msg, None, proc)

    assert result is True
    assert proc.cols == 120
    assert proc.rows == 40


@pytest.mark.asyncio
async def test_handle_ws_message_text_resize_via_cols_key():
    """Text WS message with 'cols' key (no action) should also trigger resize."""
    proc = FakeWorkerProcess(name="w1")
    payload = json.dumps({"cols": 100, "rows": 30})
    msg = _make_ws_msg(web.WSMsgType.TEXT, data=payload)

    result = await _handle_ws_message(msg, None, proc)

    assert result is True
    assert proc.cols == 100
    assert proc.rows == 30


@pytest.mark.asyncio
async def test_handle_ws_message_text_resize_clamps_values():
    """Resize values should be clamped to [1, 500]."""
    proc = FakeWorkerProcess(name="w1")
    payload = json.dumps({"action": "resize", "cols": -5, "rows": 9999})
    msg = _make_ws_msg(web.WSMsgType.TEXT, data=payload)

    await _handle_ws_message(msg, None, proc)

    assert proc.cols == 1
    assert proc.rows == 500


@pytest.mark.asyncio
async def test_handle_ws_message_close_returns_false():
    """WS CLOSE message should return False to break the loop."""
    proc = FakeWorkerProcess(name="w1")
    msg = _make_ws_msg(web.WSMsgType.CLOSE)

    result = await _handle_ws_message(msg, None, proc)

    assert result is False


@pytest.mark.asyncio
async def test_handle_ws_message_error_returns_false():
    """WS ERROR message should return False to break the loop."""
    proc = FakeWorkerProcess(name="w1")
    msg = _make_ws_msg(web.WSMsgType.ERROR)

    result = await _handle_ws_message(msg, None, proc)

    assert result is False


@pytest.mark.asyncio
async def test_handle_ws_message_text_invalid_json_ignored():
    """Invalid JSON in text message should be silently ignored."""
    proc = FakeWorkerProcess(name="w1")
    msg = _make_ws_msg(web.WSMsgType.TEXT, data="not json{{{")

    result = await _handle_ws_message(msg, None, proc)

    assert result is True
    # Cols/rows unchanged from defaults
    assert proc.cols == 200
    assert proc.rows == 50


# ---------------------------------------------------------------------------
# _validate_terminal_request
# ---------------------------------------------------------------------------


@patch(
    "swarm.pty.bridge._check_auth",
    return_value=web.Response(status=401, text="Unauthorized"),
)
def test_validate_terminal_request_auth_failure(
    mock_auth: MagicMock,
):
    """Auth failure returns 401 response."""
    request = _make_request()
    result = _validate_terminal_request(request)
    assert isinstance(result, web.Response)
    assert result.status == 401


@patch("swarm.pty.bridge._check_auth", return_value=None)
@patch("swarm.server.helpers.get_daemon")
def test_validate_terminal_request_concurrency_limit(
    mock_get_daemon: MagicMock,
    mock_auth: MagicMock,
):
    """When sessions are at max, return 503."""
    daemon = _make_daemon()
    mock_get_daemon.return_value = daemon

    sessions = {f"session-{i}" for i in range(_MAX_TERMINAL_SESSIONS)}
    app: dict = {
        "daemon": daemon,
        "_terminal_sessions": sessions,
    }
    request = _make_request(app=app, daemon=daemon)

    result = _validate_terminal_request(request)
    assert isinstance(result, web.Response)
    assert result.status == 503


@patch("swarm.pty.bridge._check_auth", return_value=None)
@patch("swarm.server.helpers.get_daemon")
def test_validate_terminal_request_missing_worker_param(
    mock_get_daemon: MagicMock,
    mock_auth: MagicMock,
):
    """Missing worker query param returns 400."""
    daemon = _make_daemon()
    mock_get_daemon.return_value = daemon

    request = _make_request(
        app={"daemon": daemon},
        daemon=daemon,
    )
    result = _validate_terminal_request(request)
    assert isinstance(result, web.Response)
    assert result.status == 400


@patch("swarm.pty.bridge._check_auth", return_value=None)
@patch("swarm.server.helpers.get_daemon")
def test_validate_terminal_request_unknown_worker(
    mock_get_daemon: MagicMock,
    mock_auth: MagicMock,
):
    """Unknown worker name returns 404."""
    daemon = _make_daemon(workers=[])
    mock_get_daemon.return_value = daemon

    request = _make_request(
        query={"worker": "missing"},
        app={"daemon": daemon},
        daemon=daemon,
    )
    result = _validate_terminal_request(request)
    assert isinstance(result, web.Response)
    assert result.status == 404


@patch("swarm.pty.bridge._check_auth", return_value=None)
@patch("swarm.server.helpers.get_daemon")
def test_validate_terminal_request_success(
    mock_get_daemon: MagicMock,
    mock_auth: MagicMock,
):
    """Valid request returns (daemon, worker, sessions) tuple."""
    worker = Worker(
        name="api",
        path="/tmp/api",
        process=FakeWorkerProcess(name="api"),
    )
    daemon = _make_daemon(workers=[worker])
    mock_get_daemon.return_value = daemon

    request = _make_request(
        query={"worker": "api"},
        app={"daemon": daemon},
        daemon=daemon,
    )
    result = _validate_terminal_request(request)

    assert isinstance(result, tuple)
    returned_daemon, returned_worker, returned_sessions = result
    assert returned_daemon is daemon
    assert returned_worker is worker
    assert isinstance(returned_sessions, set)


# ---------------------------------------------------------------------------
# handle_terminal_ws — session cleanup
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handle_terminal_ws_cleanup_removes_session_key():
    """After WS disconnects, session key should be removed from sessions set."""
    fake_proc = FakeWorkerProcess(name="api")
    worker = Worker(
        name="api",
        path="/tmp/api",
        process=fake_proc,
    )
    daemon = _make_daemon(workers=[worker])
    sessions: set[str] = set()

    # Mock _validate_terminal_request to return success tuple
    with patch(
        "swarm.pty.bridge._validate_terminal_request",
        return_value=(daemon, worker, sessions),
    ):
        # Mock WebSocketResponse
        ws = AsyncMock(spec=web.WebSocketResponse)
        ws.prepare = AsyncMock()
        ws.send_bytes = AsyncMock()
        ws.closed = False
        ws.close = AsyncMock()
        # Simulate immediate close (empty async iterator)
        stop = AsyncMock(side_effect=StopAsyncIteration)
        ws.__aiter__ = MagicMock(
            return_value=AsyncMock(__anext__=stop),
        )

        with patch(
            "swarm.pty.bridge._send_initial_view",
            new_callable=AsyncMock,
        ):
            with patch(
                "swarm.pty.bridge.web.WebSocketResponse",
                return_value=ws,
            ):
                request = _make_request(
                    app={"daemon": daemon},
                    daemon=daemon,
                )
                await handle_terminal_ws(request)

    # Session key should have been added then removed
    assert len(sessions) == 0


@pytest.mark.asyncio
async def test_handle_terminal_ws_no_process_returns_503():
    """If worker has no process, return 503 and clean up session."""
    worker = Worker(
        name="api",
        path="/tmp/api",
        process=None,
    )
    daemon = _make_daemon(workers=[worker])
    sessions: set[str] = set()

    with patch(
        "swarm.pty.bridge._validate_terminal_request",
        return_value=(daemon, worker, sessions),
    ):
        request = _make_request(
            app={"daemon": daemon},
            daemon=daemon,
        )
        result = await handle_terminal_ws(request)

    assert result.status == 503
    # Session key should have been cleaned up
    assert len(sessions) == 0


# ---------------------------------------------------------------------------
# _check_auth — origin validation
# ---------------------------------------------------------------------------


@patch("swarm.server.api._get_api_password")
@patch("swarm.server.helpers.get_daemon")
@patch("swarm.server.api._is_same_origin", return_value=False)
def test_check_auth_rejects_cross_origin(
    mock_same_origin: MagicMock,
    mock_get_daemon: MagicMock,
    mock_get_pw: MagicMock,
):
    """When origin doesn't match, _check_auth returns 403 before checking password."""
    daemon = _make_daemon()
    mock_get_daemon.return_value = daemon
    mock_get_pw.return_value = None

    request = _make_request(daemon=daemon)
    request.headers = {"Origin": "http://evil.com"}
    result = _check_auth(request)
    assert result is not None
    assert result.status == 403


# ---------------------------------------------------------------------------
# _send_initial_view — buffer snapshots
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_initial_view_uses_snapshot():
    """Initial view should send the full buffer snapshot for scrollback."""
    from swarm.config import TerminalConfig

    proc = FakeWorkerProcess(name="w1")
    proc.set_content("Hello world\n")

    ws = AsyncMock(spec=web.WebSocketResponse)
    ws.send_bytes = AsyncMock()
    ws.send_str = AsyncMock()

    cfg = TerminalConfig(replay_scrollback=True)
    await _send_initial_view(ws, proc, terminal_cfg=cfg)

    # Should have sent bytes (the rendered screen)
    ws.send_bytes.assert_called_once()
    sent = ws.send_bytes.call_args[0][0]
    assert b"Hello world" in sent


@pytest.mark.asyncio
async def test_send_initial_view_skips_replay_when_disabled():
    """When replay_scrollback=False, no bytes should be sent."""
    from swarm.config import TerminalConfig

    proc = FakeWorkerProcess(name="w1")
    proc.set_content("Some content\n")

    ws = AsyncMock(spec=web.WebSocketResponse)
    ws.send_bytes = AsyncMock()
    ws.send_str = AsyncMock()

    cfg = TerminalConfig(replay_scrollback=False)
    await _send_initial_view(ws, proc, terminal_cfg=cfg)

    # No binary frames — only the meta JSON frame via send_str
    ws.send_bytes.assert_not_called()


@pytest.mark.asyncio
async def test_send_initial_view_sends_meta():
    """Meta frame should always be sent after the buffer snapshot."""
    from swarm.config import TerminalConfig

    proc = FakeWorkerProcess(name="w1")
    proc.set_content("test\n")

    ws = AsyncMock(spec=web.WebSocketResponse)
    ws.send_bytes = AsyncMock()
    ws.send_str = AsyncMock()

    cfg = TerminalConfig(replay_scrollback=True)
    await _send_initial_view(ws, proc, terminal_cfg=cfg)

    # Meta frame sent via send_str with JSON containing alt screen info
    ws.send_str.assert_called_once()
    import json

    meta = json.loads(ws.send_str.call_args[0][0])
    assert meta["meta"] == "term"
    assert "alt" in meta


@pytest.mark.asyncio
async def test_send_initial_view_subscribes_after_sends():
    """Regression: subscribe_ws must be called AFTER async sends complete.

    Previously subscribe happened before the snapshot send, creating a race
    where feed_output could create a _ws_sender task that called
    ws.send_bytes() concurrently, silently dropping the subscriber.
    """
    from swarm.config import TerminalConfig

    proc = FakeWorkerProcess(name="w1")
    proc.set_content("snapshot data\n")

    call_order: list[str] = []

    ws = AsyncMock(spec=web.WebSocketResponse)

    async def _track_send_bytes(data: bytes) -> None:
        call_order.append("send_bytes")

    async def _track_send_str(data: str) -> None:
        call_order.append("send_str")

    ws.send_bytes = AsyncMock(side_effect=_track_send_bytes)
    ws.send_str = AsyncMock(side_effect=_track_send_str)

    original_subscribe = proc.subscribe_ws

    def _track_subscribe(ws_arg: object) -> None:
        call_order.append("subscribe_ws")
        original_subscribe(ws_arg)

    proc.subscribe_ws = _track_subscribe  # type: ignore[assignment]

    cfg = TerminalConfig(replay_scrollback=True)
    await _send_initial_view(ws, proc, terminal_cfg=cfg)

    # subscribe_ws must come AFTER both send_bytes and send_str
    assert "send_bytes" in call_order
    assert "send_str" in call_order
    assert "subscribe_ws" in call_order
    assert call_order.index("subscribe_ws") > call_order.index("send_bytes")
    assert call_order.index("subscribe_ws") > call_order.index("send_str")


@pytest.mark.asyncio
async def test_send_initial_view_subscribes_after_meta_when_no_scrollback():
    """Regression: subscribe_ws must follow sends even without scrollback.

    When replay_scrollback=False there is no send_bytes, but subscribe must
    still happen after the meta send_str to avoid the same race.
    """
    from swarm.config import TerminalConfig

    proc = FakeWorkerProcess(name="w1")
    proc.set_content("ignored\n")

    call_order: list[str] = []

    ws = AsyncMock(spec=web.WebSocketResponse)

    async def _track_send_str(data: str) -> None:
        call_order.append("send_str")

    ws.send_bytes = AsyncMock()
    ws.send_str = AsyncMock(side_effect=_track_send_str)

    original_subscribe = proc.subscribe_ws

    def _track_subscribe(ws_arg: object) -> None:
        call_order.append("subscribe_ws")
        original_subscribe(ws_arg)

    proc.subscribe_ws = _track_subscribe  # type: ignore[assignment]

    cfg = TerminalConfig(replay_scrollback=False)
    await _send_initial_view(ws, proc, terminal_cfg=cfg)

    # No send_bytes (scrollback disabled), but subscribe still after send_str
    ws.send_bytes.assert_not_called()
    assert "send_str" in call_order
    assert "subscribe_ws" in call_order
    assert call_order.index("subscribe_ws") > call_order.index("send_str")
