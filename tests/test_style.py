"""Tests for tmux/style.py — session styling and keybindings."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from swarm.tmux.cell import TmuxError
from swarm.tmux.style import (
    apply_session_style,
    bind_session_keys,
    set_terminal_title,
    setup_tmux_for_session,
    spinner_frame,
)


def test_spinner_frame_cycles():
    """Spinner frames cycle through characters."""
    frames = set()
    for i in range(20):
        frames.add(spinner_frame(i))
    assert len(frames) > 1


def test_spinner_frame_wraps():
    assert spinner_frame(0) == spinner_frame(10)


@pytest.mark.asyncio
async def test_setup_tmux_for_session():
    mock = AsyncMock()
    with patch("swarm.tmux.style.run_tmux", mock):
        await setup_tmux_for_session("swarm")
    # Should set many session options via gather
    assert mock.await_count >= 16
    # Verify first call sets mouse on
    first_call = mock.call_args_list[0]
    assert "mouse" in first_call[0]


@pytest.mark.asyncio
async def test_apply_session_style():
    calls = []

    async def fake_run(*args):
        calls.append(args)
        if args[0] == "list-windows":
            return "0\n1\n"
        return ""

    with patch("swarm.tmux.style.run_tmux", side_effect=fake_run):
        await apply_session_style("swarm")

    # Should have list-windows + session opts + window opts for 2 windows
    assert len(calls) > 10


@pytest.mark.asyncio
async def test_bind_session_keys():
    mock = AsyncMock()
    with patch("swarm.tmux.style.run_tmux", mock):
        await bind_session_keys("swarm")
    # Should bind multiple keys (Alt-c, Alt-y, etc.) + MouseDown1Pane
    assert mock.await_count >= 10
    # Verify bind-key is used
    assert all("bind-key" in call[0] for call in mock.call_args_list)


@pytest.mark.asyncio
async def test_set_terminal_title():
    mock = AsyncMock()
    with patch("swarm.tmux.style.run_tmux", mock):
        await set_terminal_title("swarm", "My Title")
    assert mock.await_count == 2


@pytest.mark.asyncio
async def test_set_terminal_title_error():
    with patch(
        "swarm.tmux.style.run_tmux",
        new_callable=AsyncMock,
        side_effect=TmuxError("no session"),
    ):
        await set_terminal_title("swarm", "Title")  # Should not raise
