"""Tests for tmux/layout.py — window/pane layout calculations."""

from swarm.config import WorkerConfig
from swarm.tmux.layout import compute_l_shape, _equal_split_pcts, plan_layout


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

    def test_five_workers_one_window(self):
        # Default is now 8, so 5 workers fit in one window
        windows = plan_layout(_workers(5))
        assert len(windows) == 1
        assert len(windows[0]) == 5

    def test_eight_workers_one_window(self):
        windows = plan_layout(_workers(8))
        assert len(windows) == 1
        assert len(windows[0]) == 8

    def test_nine_workers_two_windows(self):
        windows = plan_layout(_workers(9))
        assert len(windows) == 2
        assert len(windows[0]) == 8
        assert len(windows[1]) == 1

    def test_custom_panes_per_window(self):
        windows = plan_layout(_workers(6), panes_per_window=2)
        assert len(windows) == 3
        assert all(len(w) == 2 for w in windows)

    def test_empty_workers(self):
        windows = plan_layout([])
        assert windows == []

    def test_preserves_worker_order(self):
        workers = _workers(10)
        windows = plan_layout(workers)
        flat = [w for window in windows for w in window]
        assert [w.name for w in flat] == [w.name for w in workers]


class TestComputeLShape:
    def test_single_pane(self):
        assert compute_l_shape(1) == (0, 0)

    def test_two_panes(self):
        assert compute_l_shape(2) == (1, 0)

    def test_three_panes(self):
        assert compute_l_shape(3) == (2, 0)

    def test_four_panes(self):
        assert compute_l_shape(4) == (3, 0)

    def test_five_panes(self):
        right, bottom = compute_l_shape(5)
        assert right == 2
        assert bottom == 2

    def test_six_panes(self):
        right, bottom = compute_l_shape(6)
        assert right == 2
        assert bottom == 3

    def test_seven_panes(self):
        right, bottom = compute_l_shape(7)
        assert right == 3
        assert bottom == 3

    def test_eight_panes(self):
        right, bottom = compute_l_shape(8)
        assert right == 3
        assert bottom == 4

    def test_total_equals_n_minus_one(self):
        """Right + bottom should always equal n-1 (all panes minus focus)."""
        for n in range(1, 20):
            right, bottom = compute_l_shape(n)
            assert right + bottom == max(0, n - 1), f"Failed for n={n}"

    def test_zero_panes(self):
        assert compute_l_shape(0) == (0, 0)


class TestEqualSplitPcts:
    def test_one_part_no_splits(self):
        assert _equal_split_pcts(1) == []

    def test_two_parts(self):
        pcts = _equal_split_pcts(2)
        assert len(pcts) == 1
        assert pcts[0] == 50

    def test_three_parts(self):
        pcts = _equal_split_pcts(3)
        assert len(pcts) == 2
        assert pcts[0] == 67
        assert pcts[1] == 50

    def test_four_parts(self):
        pcts = _equal_split_pcts(4)
        assert len(pcts) == 3
        assert pcts[0] == 75
        assert pcts[1] == 67
        assert pcts[2] == 50

    def test_all_between_1_and_99(self):
        """All split percentages should be valid tmux percentages."""
        for n in range(2, 20):
            pcts = _equal_split_pcts(n)
            for pct in pcts:
                assert 1 <= pct <= 99, f"Bad pct {pct} for n={n}"
