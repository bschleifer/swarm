"""Tmux session (hive) management: create, attach, kill, discover."""

from __future__ import annotations

import re

from swarm.logging import get_logger
from swarm.tmux.cell import TmuxError, run_tmux
from swarm.worker.worker import Worker, WorkerState

_log = get_logger("tmux.hive")


async def session_exists(session_name: str) -> bool:
    try:
        await run_tmux("has-session", "-t", session_name)
        return True
    except TmuxError:
        return False


async def create_session(session_name: str, first_worker_name: str, first_worker_path: str) -> None:
    """Create a new tmux session with the first pane."""
    import os

    # Pass terminal size so detached sessions don't default to 80x24
    try:
        cols, rows = os.get_terminal_size()
    except OSError:
        cols, rows = 200, 50
    await run_tmux(
        "new-session",
        "-d",
        "-s",
        session_name,
        "-x",
        str(cols),
        "-y",
        str(rows),
        "-n",
        first_worker_name,
        "-c",
        first_worker_path,
    )
    # Prevent tmux from overwriting custom window/pane names
    await run_tmux("set", "-t", session_name, "automatic-rename", "off")
    await run_tmux("set", "-t", session_name, "allow-rename", "off")


async def add_pane(session_name: str, window_index: str, worker_path: str) -> str:
    """Split a new pane in the given window, return the new pane ID."""
    result = await run_tmux(
        "split-window",
        "-t",
        f"{session_name}:{window_index}",
        "-c",
        worker_path,
        "-P",
        "-F",
        "#{pane_id}",
    )
    return result


async def add_window(session_name: str, window_name: str, worker_path: str) -> str:
    """Create a new window in the session, return the window index."""
    result = await run_tmux(
        "new-window",
        "-t",
        session_name,
        "-n",
        window_name,
        "-c",
        worker_path,
        "-P",
        "-F",
        "#{window_index}",
    )
    return result


async def set_pane_option(pane_id: str, option: str, value: str) -> None:
    """Set a user option on a pane."""
    await run_tmux("set", "-p", "-t", pane_id, option, value)


async def get_pane_option(pane_id: str, option: str) -> str:
    """Get a user option from a pane."""
    return await run_tmux("show", "-p", "-t", pane_id, "-v", option)


async def list_window_indices(session_name: str) -> list[str]:
    """List all window indices in a session."""
    raw = await run_tmux("list-windows", "-t", session_name, "-F", "#{window_index}")
    return [line.strip() for line in raw.splitlines() if line.strip()]


async def count_panes(session_name: str, window_index: str) -> int:
    """Count panes in a specific window."""
    raw = await run_tmux("list-panes", "-t", f"{session_name}:{window_index}", "-F", "#{pane_id}")
    return len([line for line in raw.splitlines() if line.strip()])


async def kill_pane(pane_id: str) -> None:
    """Kill a specific tmux pane."""
    await run_tmux("kill-pane", "-t", pane_id)


async def kill_session(session_name: str) -> None:
    await run_tmux("kill-session", "-t", session_name)


async def find_swarm_session() -> str | None:
    """Find a running tmux session that has swarm pane metadata."""
    try:
        raw = await run_tmux("list-sessions", "-F", "#{session_name}")
    except TmuxError:
        return None
    if not raw:
        return None
    for session in raw.splitlines():
        session = session.strip()
        if not session:
            continue
        # Check if any pane in this session has @swarm_name set
        try:
            panes = await run_tmux(
                "list-panes",
                "-s",
                "-t",
                session,
                "-F",
                "#{@swarm_name}",
            )
        except TmuxError:
            continue
        for line in panes.splitlines():
            if line.strip():
                return session
    return None


async def discover_workers(session_name: str) -> list[Worker]:
    """Discover all panes in a swarm session and return Worker objects."""
    if not await session_exists(session_name):
        return []

    try:
        raw = await run_tmux(
            "list-panes",
            "-s",
            "-t",
            session_name,
            "-F",
            "#{pane_id}\t#{window_index}\t#{pane_index}\t#{@swarm_name}\t#{pane_current_path}\t#{@swarm_state}",
        )
    except TmuxError:
        return []

    _STATE_MAP = {s.value: s for s in WorkerState if s != WorkerState.SLEEPING}
    workers = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t", maxsplit=5)
        if len(parts) < 5:
            _log.debug("skipping malformed pane line: %s", line)
            continue
        pane_id = parts[0]
        win_idx = parts[1]
        pane_idx = parts[2]
        name = parts[3]
        path = parts[4]
        tmux_state = parts[5] if len(parts) > 5 else ""
        if not name:
            name = f"pane-{win_idx}.{pane_idx}"
        # Map tmux state back to internal WorkerState.
        # SLEEPING is display-only — map it back to RESTING.
        if tmux_state == "SLEEPING":
            state = WorkerState.RESTING
        else:
            state = _STATE_MAP.get(tmux_state, WorkerState.BUZZING)
        workers.append(
            Worker(
                name=name,
                path=path,
                pane_id=pane_id,
                state=state,
            )
        )
    return workers


async def update_window_names(session_name: str, workers: list[Worker]) -> None:  # noqa: C901
    """Update window names with "(N idle)" suffix based on worker states."""
    try:
        raw = await run_tmux(
            "list-windows",
            "-t",
            session_name,
            "-F",
            "#{window_index}\t#{window_name}",
        )
    except TmuxError:
        return
    if not raw:
        return

    # Map pane_id → worker for quick lookup
    worker_by_pane: dict[str, Worker] = {w.pane_id: w for w in workers}

    # Get pane→window mapping
    try:
        pane_raw = await run_tmux(
            "list-panes",
            "-s",
            "-t",
            session_name,
            "-F",
            "#{pane_id}\t#{window_index}",
        )
    except TmuxError:
        return
    panes_by_window: dict[str, list[str]] = {}
    for line in pane_raw.splitlines():
        if "\t" not in line:
            continue
        pid, widx = line.split("\t", 1)
        panes_by_window.setdefault(widx, []).append(pid)

    for line in raw.splitlines():
        if "\t" not in line:
            continue
        win_idx, win_name = line.split("\t", 1)

        # Strip any existing "(N idle)" or "(N waiting)" suffix to get the base name
        base_name = re.sub(r"\s*\(\d+ (?:idle|waiting)\)$", "", win_name)

        # Count idle and waiting workers in this window
        # Use display_state so SLEEPING workers also count as idle
        pane_ids = panes_by_window.get(win_idx, [])
        idle_count = sum(
            1
            for pid in pane_ids
            if pid in worker_by_pane
            and worker_by_pane[pid].display_state in (WorkerState.RESTING, WorkerState.SLEEPING)
        )
        waiting_count = sum(
            1
            for pid in pane_ids
            if pid in worker_by_pane and worker_by_pane[pid].display_state == WorkerState.WAITING
        )

        if waiting_count > 0:
            new_name = f"{base_name} ({waiting_count} waiting)"
        elif idle_count > 0:
            new_name = f"{base_name} ({idle_count} idle)"
        else:
            new_name = base_name

        if new_name != win_name:
            try:
                await run_tmux("rename-window", "-t", f"{session_name}:{win_idx}", new_name)
            except TmuxError:
                pass  # Window may have been closed
