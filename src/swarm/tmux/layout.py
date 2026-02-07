"""Window/pane layout calculations."""

from __future__ import annotations

from swarm.config import WorkerConfig


def plan_layout(workers: list[WorkerConfig], panes_per_window: int = 4) -> list[list[WorkerConfig]]:
    """Group workers into windows, each with at most panes_per_window panes."""
    windows: list[list[WorkerConfig]] = []
    for i in range(0, len(workers), panes_per_window):
        windows.append(workers[i:i + panes_per_window])
    return windows
