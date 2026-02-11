"""Window/pane layout calculations."""

from __future__ import annotations

from swarm.config import WorkerConfig
from swarm.tmux.cell import run_tmux


def plan_layout(workers: list[WorkerConfig], panes_per_window: int = 9) -> list[list[WorkerConfig]]:
    """Group workers into windows, each with at most panes_per_window panes.

    Default is 9 (3×3 grid).
    """
    panes_per_window = max(1, panes_per_window)
    windows: list[list[WorkerConfig]] = []
    for i in range(0, len(workers), panes_per_window):
        windows.append(workers[i : i + panes_per_window])
    return windows


async def apply_tiled_layout(
    session_name: str,
    window_index: str,
    worker_paths: list[str],
) -> list[str]:
    """Create panes and apply tmux's tiled layout, return pane IDs in order.

    For up to 9 panes this produces a roughly equal grid (e.g. 3×3).
    Use Alt+z (zoom) to fullscreen any individual pane.

    Layout examples::

        2 panes     3 panes      4 panes       9 panes (3×3)
        ┌───┬───┐   ┌───┬───┐   ┌───┬───┐   ┌───┬───┬───┐
        │   │   │   │   │ 2 │   │ 1 │ 2 │   │ 1 │ 2 │ 3 │
        │ 1 │ 2 │   │ 1 ├───┤   ├───┼───┤   ├───┼───┼───┤
        │   │   │   │   │ 3 │   │ 3 │ 4 │   │ 4 │ 5 │ 6 │
        └───┴───┘   └───┴───┘   └───┴───┘   ├───┼───┼───┤
                                              │ 7 │ 8 │ 9 │
                                              └───┴───┴───┘
    """
    n = len(worker_paths)
    if n == 0:
        return []

    # The first pane already exists as {session_name}:{window_index}.0
    focus_target = f"{session_name}:{window_index}.0"
    focus_id = await run_tmux(
        "display-message",
        "-p",
        "-t",
        focus_target,
        "#{pane_id}",
    )

    pane_ids = [focus_id]
    window_target = f"{session_name}:{window_index}"

    # Create additional panes by splitting.
    # After each split, re-apply tiled layout so tmux redistributes space
    # evenly — prevents "no space for new pane" when splitting from a
    # pane that has been shrunk by previous splits.
    for i in range(1, n):
        new_id = await run_tmux(
            "split-window",
            "-t",
            focus_id,
            "-c",
            worker_paths[i],
            "-P",
            "-F",
            "#{pane_id}",
        )
        pane_ids.append(new_id)
        # Redistribute space after each split so the next split has room
        await run_tmux("select-layout", "-t", window_target, "tiled")

    # Select the first pane
    await run_tmux("select-pane", "-t", focus_id)

    return pane_ids
