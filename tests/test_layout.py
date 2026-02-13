"""Tests for tmux/layout.py — window/pane layout calculations."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from swarm.config import WorkerConfig
from swarm.tmux.layout import apply_tiled_layout, plan_layout


def _workers(n: int) -> list[WorkerConfig]:
    return [WorkerConfig(name=f"w{i}", path=f"/tmp/w{i}") for i in range(n)]


class TestPlanLayout:
    def test_single_worker_one_window(self):
        windows = plan_layout(_workers(1))
        assert len(windows) == 1
        assert len(windows[0]) == 1

    def test_four_workers_one_window(self):
        windows = plan_layout(_workers(4))
        assert len(windows) == 1
        assert len(windows[0]) == 4

    def test_nine_workers_one_window(self):
        """Default is 9 (3x3 grid), so 9 workers fit in one window."""
        windows = plan_layout(_workers(9))
        assert len(windows) == 1
        assert len(windows[0]) == 9

    def test_ten_workers_two_windows(self):
        windows = plan_layout(_workers(10))
        assert len(windows) == 2
        assert len(windows[0]) == 9
        assert len(windows[1]) == 1

    def test_custom_panes_per_window(self):
        windows = plan_layout(_workers(6), panes_per_window=2)
        assert len(windows) == 3
        assert all(len(w) == 2 for w in windows)

    def test_empty_workers(self):
        windows = plan_layout([])
        assert windows == []

    def test_preserves_worker_order(self):
        workers = _workers(15)
        windows = plan_layout(workers)
        flat = [w for window in windows for w in window]
        assert [w.name for w in flat] == [w.name for w in workers]

    def test_zero_panes_per_window_clamped(self):
        workers = _workers(3)
        result = plan_layout(workers, panes_per_window=0)
        # Should clamp to 1, giving 3 windows
        assert len(result) == 3


class TestApplyTiledLayout:
    """Tests for apply_tiled_layout()."""

    @pytest.mark.asyncio
    async def test_empty_paths(self):
        result = await apply_tiled_layout("s", "0", [])
        assert result == []

    @pytest.mark.asyncio
    async def test_single_pane(self):
        mock = AsyncMock(return_value="%0")
        with patch("swarm.tmux.layout.run_tmux", mock):
            result = await apply_tiled_layout("s", "0", ["/tmp/w1"])
        assert result == ["%0"]
        # display-message + select-pane (no split)
        assert mock.await_count == 2

    @pytest.mark.asyncio
    async def test_multiple_panes(self):
        call_count = 0

        async def fake_run(*args):
            nonlocal call_count
            call_count += 1
            if args[0] == "display-message":
                return "%0"
            if args[0] == "split-window":
                return f"%{call_count}"
            return ""

        with patch("swarm.tmux.layout.run_tmux", side_effect=fake_run):
            result = await apply_tiled_layout("s", "0", ["/a", "/b", "/c"])
        assert len(result) == 3
        assert result[0] == "%0"
