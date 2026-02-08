"""Window/pane layout calculations."""

from __future__ import annotations

from swarm.config import WorkerConfig
from swarm.tmux.cell import _run_tmux


def plan_layout(workers: list[WorkerConfig], panes_per_window: int = 8) -> list[list[WorkerConfig]]:
    """Group workers into windows, each with at most panes_per_window panes."""
    panes_per_window = max(1, panes_per_window)
    windows: list[list[WorkerConfig]] = []
    for i in range(0, len(workers), panes_per_window):
        windows.append(workers[i : i + panes_per_window])
    return windows


def compute_l_shape(n: int) -> tuple[int, int]:
    """Return (right_count, bottom_count) for N total panes including the focus pane.

    | Workers | Right | Bottom |
    |---------|-------|--------|
    | 1       | 0     | 0      |
    | 2-4     | n-1   | 0      |
    | 5       | 2     | 2      |
    | 6       | 2     | 3      |
    | 7       | 3     | 3      |
    | 8       | 3     | 4      |
    """
    if n <= 1:
        return (0, 0)
    if n <= 4:
        return (n - 1, 0)
    # For 5+ panes: distribute between right column and bottom row.
    # Right column gets roughly half of the remaining panes (rounded down),
    # bottom row gets the rest.
    remaining = n - 1  # subtract focus pane
    right = remaining // 2
    bottom = remaining - right
    return (right, bottom)


def _equal_split_pcts(n: int) -> list[int]:
    """Return split percentages for subdividing a region into n equal parts.

    For N subdivisions, we need N-1 splits. Each split i (0-indexed) uses
    percentage = round(100 * (N-1-i) / (N-i)).
    """
    if n <= 1:
        return []
    return [round(100 * (n - 1 - i) / (n - i)) for i in range(n - 1)]


async def apply_focus_layout(
    session_name: str,
    window_index: str,
    worker_paths: list[str],
) -> list[str]:
    """Create the L-shaped focus layout via tmux splits, return pane IDs.

    Returns pane IDs in order: [focus, right_0, ..., right_N, bottom_0, ..., bottom_M].
    All panes start in their respective worker_paths directories.

    Layout:
    ┌──────────────────┬──────┐
    │                  │  R1  │
    │     FOCUS        ├──────┤
    │                  │  R2  │
    │                  ├──────┤
    │                  │  R3  │
    ├────┬────┬────┬───┴──────┤
    │ B1 │ B2 │ B3 │    B4    │
    └────┴────┴────┴──────────┘
    """
    n = len(worker_paths)
    if n == 0:
        return []

    right_count, bottom_count = compute_l_shape(n)

    # The focus pane already exists as {session_name}:{window_index}.0
    # Get its pane ID
    focus_target = f"{session_name}:{window_index}.0"
    focus_id = await _run_tmux(
        "display-message",
        "-p",
        "-t",
        focus_target,
        "#{pane_id}",
    )

    pane_ids = [focus_id]

    if n == 1:
        return pane_ids

    # Path index: focus=0, then right panes, then bottom panes
    path_idx = 1

    # --- Step 1: Create the bottom row (if needed) ---
    # Split focus pane vertically: top 75%, bottom 25% (full width)
    bottom_ids: list[str] = []
    if bottom_count > 0:
        bottom_first = await _run_tmux(
            "split-window",
            "-v",
            "-l",
            "25%",
            "-t",
            focus_id,
            "-c",
            worker_paths[path_idx + right_count],  # first bottom pane path
            "-P",
            "-F",
            "#{pane_id}",
        )
        bottom_ids.append(bottom_first)

        # Subdivide the bottom row horizontally into bottom_count equal panes
        pcts = _equal_split_pcts(bottom_count)
        for i, pct in enumerate(pcts):
            new_id = await _run_tmux(
                "split-window",
                "-h",
                "-l",
                f"{pct}%",
                "-t",
                bottom_ids[i],
                "-c",
                worker_paths[path_idx + right_count + i + 1],
                "-P",
                "-F",
                "#{pane_id}",
            )
            bottom_ids.append(new_id)

    # --- Step 2: Create the right column ---
    # Split the focus pane (top area) horizontally: left 75%, right 25%
    right_ids: list[str] = []
    if right_count > 0:
        right_first = await _run_tmux(
            "split-window",
            "-h",
            "-l",
            "25%",
            "-t",
            focus_id,
            "-c",
            worker_paths[path_idx],  # first right pane path
            "-P",
            "-F",
            "#{pane_id}",
        )
        right_ids.append(right_first)

        # Subdivide the right column vertically into right_count equal panes
        pcts = _equal_split_pcts(right_count)
        for i, pct in enumerate(pcts):
            new_id = await _run_tmux(
                "split-window",
                "-v",
                "-l",
                f"{pct}%",
                "-t",
                right_ids[i],
                "-c",
                worker_paths[path_idx + i + 1],
                "-P",
                "-F",
                "#{pane_id}",
            )
            right_ids.append(new_id)

    # --- Assemble final order: [focus, right..., bottom...] ---
    pane_ids.extend(right_ids)
    pane_ids.extend(bottom_ids)

    # Select focus pane so it's active
    await _run_tmux("select-pane", "-t", focus_id)

    return pane_ids
