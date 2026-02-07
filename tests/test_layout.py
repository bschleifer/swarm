"""Tests for tmux/layout.py — window/pane layout calculations."""

from swarm.config import WorkerConfig
from swarm.tmux.layout import plan_layout


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

    def test_five_workers_two_windows(self):
        windows = plan_layout(_workers(5))
        assert len(windows) == 2
        assert len(windows[0]) == 4
        assert len(windows[1]) == 1

    def test_eight_workers_two_windows(self):
        windows = plan_layout(_workers(8))
        assert len(windows) == 2
        assert all(len(w) == 4 for w in windows)

    def test_custom_panes_per_window(self):
        windows = plan_layout(_workers(6), panes_per_window=2)
        assert len(windows) == 3
        assert all(len(w) == 2 for w in windows)

    def test_empty_workers(self):
        windows = plan_layout([])
        assert windows == []

    def test_preserves_worker_order(self):
        workers = _workers(5)
        windows = plan_layout(workers)
        flat = [w for window in windows for w in window]
        assert [w.name for w in flat] == [w.name for w in workers]
