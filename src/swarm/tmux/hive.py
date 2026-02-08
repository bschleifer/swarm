"""Tmux session (hive) management: create, attach, kill, discover."""

from __future__ import annotations

import asyncio
import re

from swarm.logging import get_logger
from swarm.tmux.cell import TmuxError, _run_tmux
from swarm.worker.worker import Worker, WorkerState

_log = get_logger("tmux.hive")


async def session_exists(session_name: str) -> bool:
    proc = await asyncio.create_subprocess_exec(
        "tmux",
        "has-session",
        "-t",
        session_name,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    await proc.communicate()
    return proc.returncode == 0


async def create_session(session_name: str, first_worker_name: str, first_worker_path: str) -> None:
    """Create a new tmux session with the first pane."""
    import os

    # Pass terminal size so detached sessions don't default to 80x24
    try:
        cols, rows = os.get_terminal_size()
    except OSError:
        cols, rows = 200, 50
    await _run_tmux(
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
    await _run_tmux("set", "-t", session_name, "automatic-rename", "off")
    await _run_tmux("set", "-t", session_name, "allow-rename", "off")


async def add_pane(session_name: str, window_index: str, worker_path: str) -> str:
    """Split a new pane in the given window, return the new pane ID."""
    result = await _run_tmux(
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
    result = await _run_tmux(
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
    await _run_tmux("set", "-p", "-t", pane_id, option, value)


async def get_pane_option(pane_id: str, option: str) -> str:
    """Get a user option from a pane."""
    return await _run_tmux("show", "-p", "-t", pane_id, "-v", option)


async def kill_session(session_name: str) -> None:
    await _run_tmux("kill-session", "-t", session_name)


async def find_swarm_session() -> str | None:
    """Find a running tmux session that has swarm pane metadata."""
    try:
        raw = await _run_tmux("list-sessions", "-F", "#{session_name}")
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
            panes = await _run_tmux(
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
        raw = await _run_tmux(
            "list-panes",
            "-s",
            "-t",
            session_name,
            "-F",
            "#{pane_id}\t#{window_index}\t#{pane_index}\t#{@swarm_name}\t#{pane_current_path}",
        )
    except TmuxError:
        return []
    workers = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) < 5:
            _log.debug("skipping malformed pane line: %s", line)
            continue
        pane_id, win_idx, pane_idx, name, path = parts
        if not name:
            name = f"pane-{win_idx}.{pane_idx}"
        workers.append(
            Worker(
                name=name,
                path=path,
                pane_id=pane_id,
            )
        )
    return workers


async def update_window_names(session_name: str, workers: list[Worker]) -> None:  # noqa: C901
    """Update window names with "(N idle)" suffix based on worker states."""
    try:
        raw = await _run_tmux(
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
        pane_raw = await _run_tmux(
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

        # Strip any existing "(N idle)" suffix to get the base name
        base_name = re.sub(r"\s*\(\d+ idle\)$", "", win_name)

        # Count idle workers in this window
        pane_ids = panes_by_window.get(win_idx, [])
        idle_count = sum(
            1
            for pid in pane_ids
            if pid in worker_by_pane and worker_by_pane[pid].state == WorkerState.RESTING
        )

        if idle_count > 0:
            new_name = f"{base_name} ({idle_count} idle)"
        else:
            new_name = base_name

        if new_name != win_name:
            try:
                await _run_tmux("rename-window", "-t", f"{session_name}:{win_idx}", new_name)
            except TmuxError:
                pass  # Window may have been closed
