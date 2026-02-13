"""Tests for tmux/cell.py — low-level tmux pane operations."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from swarm.tmux.cell import (
    PaneGoneError,
    TmuxError,
    _is_pane_gone,
    capture_pane,
    get_pane_command,
    get_pane_id,
    pane_exists,
    run_tmux,
    send_enter,
    send_escape,
    send_interrupt,
    send_keys,
)


# ---------- run_tmux ----------


@pytest.mark.asyncio
async def test_run_tmux_success():
    fake_proc = AsyncMock()
    fake_proc.communicate = AsyncMock(return_value=(b"hello\n", b""))
    fake_proc.returncode = 0
    with patch("asyncio.create_subprocess_exec", return_value=fake_proc):
        result = await run_tmux("display-message", "-p", "hello")
    assert result == "hello"


@pytest.mark.asyncio
async def test_run_tmux_nonzero_exit():
    fake_proc = AsyncMock()
    fake_proc.communicate = AsyncMock(return_value=(b"", b"error message\n"))
    fake_proc.returncode = 1
    with patch("asyncio.create_subprocess_exec", return_value=fake_proc):
        with pytest.raises(TmuxError, match="error message"):
            await run_tmux("bad-command")


@pytest.mark.asyncio
async def test_timeout_reaps_process():
    """After a timeout, proc.wait() must be called to avoid zombies."""
    fake_proc = AsyncMock()
    fake_proc.kill = MagicMock()
    fake_proc.wait = AsyncMock()
    fake_proc.communicate = AsyncMock(side_effect=asyncio.TimeoutError)

    with (
        patch("asyncio.create_subprocess_exec", return_value=fake_proc),
        patch("asyncio.wait_for", side_effect=asyncio.TimeoutError),
    ):
        with pytest.raises(TmuxError, match="timed out"):
            await run_tmux("list-panes")
        fake_proc.kill.assert_called_once()
        fake_proc.wait.assert_awaited_once()


# ---------- _is_pane_gone ----------


def test_is_pane_gone_true():
    assert _is_pane_gone(TmuxError("can't find pane: %99"))
    assert _is_pane_gone(TmuxError("no pane at index 5"))
    assert _is_pane_gone(TmuxError("session not found: swarm"))
    assert _is_pane_gone(TmuxError("no such window"))
    assert _is_pane_gone(TmuxError("can't find window: 0"))
    assert _is_pane_gone(TmuxError("pane index out of range"))


def test_is_pane_gone_false():
    assert not _is_pane_gone(TmuxError("server not found"))
    assert not _is_pane_gone(TmuxError("some random error"))


# ---------- pane_exists ----------


@pytest.mark.asyncio
async def test_pane_exists_true():
    with patch("swarm.tmux.cell.run_tmux", new_callable=AsyncMock, return_value="%5"):
        assert await pane_exists("%5") is True


@pytest.mark.asyncio
async def test_pane_exists_false():
    with patch(
        "swarm.tmux.cell.run_tmux",
        new_callable=AsyncMock,
        side_effect=TmuxError("no pane"),
    ):
        assert await pane_exists("%99") is False


@pytest.mark.asyncio
async def test_pane_exists_empty_result():
    with patch("swarm.tmux.cell.run_tmux", new_callable=AsyncMock, return_value="  "):
        assert await pane_exists("%5") is False


# ---------- get_pane_id ----------


@pytest.mark.asyncio
async def test_get_pane_id():
    with patch("swarm.tmux.cell.run_tmux", new_callable=AsyncMock, return_value="%7"):
        result = await get_pane_id("swarm:0.1")
    assert result == "%7"


# ---------- capture_pane ----------


@pytest.mark.asyncio
async def test_capture_pane_success():
    with patch(
        "swarm.tmux.cell.run_tmux",
        new_callable=AsyncMock,
        return_value="line1\nline2",
    ):
        text = await capture_pane("%0")
    assert text == "line1\nline2"


@pytest.mark.asyncio
async def test_capture_pane_gone():
    with patch(
        "swarm.tmux.cell.run_tmux",
        new_callable=AsyncMock,
        side_effect=TmuxError("can't find pane: %99"),
    ):
        with pytest.raises(PaneGoneError):
            await capture_pane("%99")


@pytest.mark.asyncio
async def test_capture_pane_other_error():
    with patch(
        "swarm.tmux.cell.run_tmux",
        new_callable=AsyncMock,
        side_effect=TmuxError("server crashed"),
    ):
        with pytest.raises(TmuxError, match="server crashed"):
            await capture_pane("%0")


# ---------- get_pane_command ----------


@pytest.mark.asyncio
async def test_get_pane_command_success():
    with patch("swarm.tmux.cell.run_tmux", new_callable=AsyncMock, return_value="bash"):
        cmd = await get_pane_command("%0")
    assert cmd == "bash"


@pytest.mark.asyncio
async def test_get_pane_command_gone():
    with patch(
        "swarm.tmux.cell.run_tmux",
        new_callable=AsyncMock,
        side_effect=TmuxError("no pane at index 0"),
    ):
        with pytest.raises(PaneGoneError):
            await get_pane_command("%99")


# ---------- send_keys ----------


@pytest.mark.asyncio
async def test_send_keys_with_enter():
    mock = AsyncMock()
    with (
        patch("swarm.tmux.cell.run_tmux", mock),
        patch("asyncio.sleep", new_callable=AsyncMock),
    ):
        await send_keys("%0", "hello")
    # Should call: copy-mode -q, send-keys -l, sleep, send-keys Enter
    assert mock.await_count == 3
    assert mock.call_args_list[1][0] == ("send-keys", "-t", "%0", "-l", "hello")
    assert mock.call_args_list[2][0] == ("send-keys", "-t", "%0", "Enter")


@pytest.mark.asyncio
async def test_send_keys_no_enter():
    mock = AsyncMock()
    with (
        patch("swarm.tmux.cell.run_tmux", mock),
        patch("asyncio.sleep", new_callable=AsyncMock),
    ):
        await send_keys("%0", "hello", enter=False)
    # Should call: copy-mode -q, send-keys -l (no Enter)
    assert mock.await_count == 2


# ---------- send_interrupt / send_enter / send_escape ----------


@pytest.mark.asyncio
async def test_send_interrupt():
    mock = AsyncMock()
    with patch("swarm.tmux.cell.run_tmux", mock):
        await send_interrupt("%0")
    assert mock.call_args_list[-1][0] == ("send-keys", "-t", "%0", "C-c")


@pytest.mark.asyncio
async def test_send_enter():
    mock = AsyncMock()
    with patch("swarm.tmux.cell.run_tmux", mock):
        await send_enter("%0")
    assert mock.call_args_list[-1][0] == ("send-keys", "-t", "%0", "Enter")


@pytest.mark.asyncio
async def test_send_escape():
    mock = AsyncMock()
    with patch("swarm.tmux.cell.run_tmux", mock):
        await send_escape("%0")
    mock.assert_awaited_once_with("send-keys", "-t", "%0", "Escape")
