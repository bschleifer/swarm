"""Tests for tunnel.py — TunnelManager lifecycle and URL parsing."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from swarm.tunnel import _RESTART_MARKER, TunnelManager, TunnelState


@pytest.fixture
def mgr():
    """Create a TunnelManager for testing."""
    return TunnelManager(port=9090)


def test_initial_state(mgr: TunnelManager) -> None:
    assert mgr.state == TunnelState.STOPPED
    assert mgr.url == ""
    assert not mgr.is_running


def test_to_dict_stopped(mgr: TunnelManager) -> None:
    d = mgr.to_dict()
    assert d["running"] is False
    assert d["state"] == "stopped"
    assert d["url"] == ""


@pytest.mark.asyncio
async def test_start_no_cloudflared(mgr: TunnelManager) -> None:
    """Start raises RuntimeError when cloudflared is not installed."""
    with patch("swarm.tunnel.shutil.which", return_value=None):
        with pytest.raises(RuntimeError, match="not installed"):
            await mgr.start()
    assert mgr.state == TunnelState.ERROR
    assert "not installed" in mgr.error


@pytest.mark.asyncio
async def test_start_parses_url(mgr: TunnelManager) -> None:
    """Start extracts the URL from cloudflared stderr."""
    stderr_lines = [
        b"2024-01-01 INFO Starting tunnel\n",
        b"2024-01-01 INFO +-------------------------------------------+\n",
        b"2024-01-01 INFO |  https://foo-bar-baz.trycloudflare.com    |\n",
        b"2024-01-01 INFO +-------------------------------------------+\n",
    ]

    mock_stderr = AsyncMock()
    line_iter = iter(stderr_lines)

    async def readline():
        try:
            return next(line_iter)
        except StopIteration:
            return b""

    mock_stderr.readline = readline

    mock_proc = MagicMock()
    mock_proc.stderr = mock_stderr
    mock_proc.returncode = None

    # Make wait() hang (process running)
    never_done = asyncio.Future()
    mock_proc.wait = AsyncMock(return_value=never_done)

    with (
        patch("swarm.tunnel.shutil.which", return_value="/usr/bin/cloudflared"),
        patch("asyncio.create_subprocess_exec", return_value=mock_proc),
    ):
        url = await mgr.start()

    assert url == "https://foo-bar-baz.trycloudflare.com"
    assert mgr.state == TunnelState.RUNNING
    assert mgr.is_running
    assert mgr.url == url

    # Clean up
    mock_proc.returncode = 0
    mock_proc.terminate = MagicMock()
    mock_proc.kill = MagicMock()
    mock_proc.wait = AsyncMock(return_value=0)
    await mgr.stop()


@pytest.mark.asyncio
async def test_start_already_running(mgr: TunnelManager) -> None:
    """Calling start when already running returns the existing URL."""
    mgr._state = TunnelState.RUNNING
    mgr._url = "https://existing.trycloudflare.com"
    url = await mgr.start()
    assert url == "https://existing.trycloudflare.com"


@pytest.mark.asyncio
async def test_stop_cleans_up(mgr: TunnelManager) -> None:
    """Stop terminates the process and resets state."""
    mock_proc = MagicMock()
    mock_proc.returncode = None
    mock_proc.terminate = MagicMock()
    mock_proc.wait = AsyncMock(return_value=0)

    mgr._process = mock_proc
    mgr._state = TunnelState.RUNNING
    mgr._url = "https://test.trycloudflare.com"

    await mgr.stop()

    mock_proc.terminate.assert_called_once()
    assert mgr.state == TunnelState.STOPPED
    assert mgr.url == ""
    assert not mgr.is_running


@pytest.mark.asyncio
async def test_stop_already_stopped(mgr: TunnelManager) -> None:
    """Stop on an already-stopped manager is a no-op."""
    await mgr.stop()
    assert mgr.state == TunnelState.STOPPED


@pytest.mark.asyncio
async def test_state_change_callback() -> None:
    """State change callback is invoked on start and stop."""
    calls: list[tuple[TunnelState, str]] = []

    def on_change(state: TunnelState, detail: str) -> None:
        calls.append((state, detail))

    mgr = TunnelManager(port=9090, on_state_change=on_change)

    stderr_lines = [
        b"INFO https://cb-test.trycloudflare.com\n",
    ]
    mock_stderr = AsyncMock()
    line_iter = iter(stderr_lines)

    async def readline():
        try:
            return next(line_iter)
        except StopIteration:
            return b""

    mock_stderr.readline = readline

    mock_proc = MagicMock()
    mock_proc.stderr = mock_stderr
    mock_proc.returncode = None
    never_done = asyncio.Future()
    mock_proc.wait = AsyncMock(return_value=never_done)

    with (
        patch("swarm.tunnel.shutil.which", return_value="/usr/bin/cloudflared"),
        patch("asyncio.create_subprocess_exec", return_value=mock_proc),
    ):
        await mgr.start()

    # Should have: STARTING, RUNNING
    assert (TunnelState.STARTING, "") in calls
    assert any(s == TunnelState.RUNNING for s, _ in calls)

    mock_proc.returncode = 0
    mock_proc.terminate = MagicMock()
    mock_proc.kill = MagicMock()
    mock_proc.wait = AsyncMock(return_value=0)
    await mgr.stop()

    # Should have STOPPED callback
    assert any(s == TunnelState.STOPPED for s, _ in calls)


@pytest.mark.asyncio
async def test_start_process_spawn_fails(mgr: TunnelManager) -> None:
    """Start raises RuntimeError if subprocess creation fails."""
    with (
        patch("swarm.tunnel.shutil.which", return_value="/usr/bin/cloudflared"),
        patch("asyncio.create_subprocess_exec", side_effect=OSError("exec failed")),
    ):
        with pytest.raises(RuntimeError, match="exec failed"):
            await mgr.start()
    assert mgr.state == TunnelState.ERROR


@pytest.mark.asyncio
async def test_start_timeout_no_url(mgr: TunnelManager) -> None:
    """Start raises RuntimeError if URL is not found before timeout."""
    mock_stderr = AsyncMock()

    async def readline():
        # Never return a URL, just keep returning non-URL lines
        await asyncio.sleep(0.01)
        return b""  # EOF

    mock_stderr.readline = readline

    mock_proc = MagicMock()
    mock_proc.stderr = mock_stderr
    mock_proc.returncode = None
    mock_proc.terminate = MagicMock()
    mock_proc.kill = MagicMock()
    mock_proc.wait = AsyncMock(return_value=0)

    with (
        patch("swarm.tunnel.shutil.which", return_value="/usr/bin/cloudflared"),
        patch("asyncio.create_subprocess_exec", return_value=mock_proc),
    ):
        with pytest.raises(RuntimeError, match="Timed out waiting for tunnel URL"):
            await mgr.start()

    assert mgr.state == TunnelState.ERROR


# --- Restart marker ---


@pytest.fixture(autouse=True)
def _clean_marker():
    """Ensure the restart marker doesn't leak between tests."""
    _RESTART_MARKER.unlink(missing_ok=True)
    yield
    _RESTART_MARKER.unlink(missing_ok=True)


def test_save_restart_marker_when_running(mgr: TunnelManager) -> None:
    """Marker file is created when tunnel is running."""
    mgr._state = TunnelState.RUNNING
    mgr.save_restart_marker()
    assert _RESTART_MARKER.exists()


def test_save_restart_marker_when_stopped(mgr: TunnelManager) -> None:
    """Marker file is NOT created when tunnel is stopped."""
    mgr.save_restart_marker()
    assert not _RESTART_MARKER.exists()


def test_consume_restart_marker_present() -> None:
    """consume_restart_marker returns True and deletes the file."""
    _RESTART_MARKER.parent.mkdir(parents=True, exist_ok=True)
    _RESTART_MARKER.touch()
    assert TunnelManager.consume_restart_marker() is True
    assert not _RESTART_MARKER.exists()


def test_consume_restart_marker_absent() -> None:
    """consume_restart_marker returns False when no marker exists."""
    assert TunnelManager.consume_restart_marker() is False


def test_marker_round_trip(mgr: TunnelManager) -> None:
    """save + consume round-trip works end-to-end."""
    mgr._state = TunnelState.RUNNING
    mgr.save_restart_marker()
    assert TunnelManager.consume_restart_marker() is True
    # Second consume should return False (file deleted)
    assert TunnelManager.consume_restart_marker() is False
