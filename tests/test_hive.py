"""Tests for tmux/hive.py — session management and discovery."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from swarm.tmux.hive import discover_workers


@pytest.mark.asyncio
async def test_discover_workers_extra_tabs_in_path():
    """Paths containing tabs should not crash discover_workers."""
    # The path has a tab character in it (e.g. from a weird directory name)
    lines = "%0\t0\t0\tapi\t/home/user/my\tproject\n%1\t0\t1\tweb\t/home/user/normal\n"
    with (
        patch("swarm.tmux.hive.session_exists", new_callable=AsyncMock, return_value=True),
        patch("swarm.tmux.hive.run_tmux", new_callable=AsyncMock, return_value=lines),
    ):
        workers = await discover_workers("test")
    assert len(workers) == 2
    assert workers[0].name == "api"
    # The tab in the path should be preserved (not split into a 6th column)
    assert "\t" in workers[0].path
    assert workers[1].name == "web"
    assert workers[1].path == "/home/user/normal"
