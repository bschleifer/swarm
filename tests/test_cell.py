"""Tests for tmux/cell.py — low-level tmux pane operations."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from swarm.tmux.cell import TmuxError, run_tmux


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
