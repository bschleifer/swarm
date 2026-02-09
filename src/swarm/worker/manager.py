"""Worker lifecycle management: spawn, kill, revive workers in tmux."""

from __future__ import annotations

from swarm.config import WorkerConfig
from swarm.logging import get_logger
from swarm.tmux import hive
from swarm.tmux.cell import TmuxError, get_pane_id, send_keys
from swarm.tmux.layout import apply_focus_layout, plan_layout
from swarm.tmux.style import (
    apply_session_style,
    bind_click_to_swap,
    bind_session_keys,
    setup_tmux_for_session,
)
from swarm.worker.worker import Worker, WorkerState

_log = get_logger("worker.manager")


async def launch_hive(
    session_name: str,
    workers: list[WorkerConfig],
    panes_per_window: int = 8,
) -> list[Worker]:
    """Launch all workers in a tmux session using the L-shaped focus layout."""
    if await hive.session_exists(session_name):
        # Warn if users are attached to the session we're about to kill
        try:
            from swarm.tmux.cell import run_tmux

            clients = await run_tmux("list-clients", "-t", session_name, "-F", "#{client_name}")
            if clients.strip():
                _log.warning(
                    "killing session '%s' with attached clients: %s",
                    session_name,
                    clients.strip().replace("\n", ", "),
                )
        except TmuxError:
            pass  # list-clients may fail if session has no clients
        await hive.kill_session(session_name)

    windows = plan_layout(workers, panes_per_window)
    launched: list[Worker] = []

    for win_idx, window_workers in enumerate(windows):
        first = window_workers[0]

        if win_idx == 0:
            await hive.create_session(session_name, first.name, str(first.resolved_path))
            await setup_tmux_for_session(session_name)
            current_window = "0"
        else:
            current_window = await hive.add_window(
                session_name,
                first.name,
                str(first.resolved_path),
            )

        # Build focus layout for all panes in this window
        worker_paths = [str(wc.resolved_path) for wc in window_workers]
        pane_ids = await apply_focus_layout(session_name, current_window, worker_paths)

        for wc, pane_id in zip(window_workers, pane_ids):
            await hive.set_pane_option(pane_id, "@swarm_name", wc.name)
            await hive.set_pane_option(pane_id, "@swarm_state", "BUZZING")
            await send_keys(pane_id, "claude", enter=True)
            launched.append(
                Worker(
                    name=wc.name,
                    path=str(wc.resolved_path),
                    pane_id=pane_id,
                    window_index=current_window,
                )
            )

    await apply_session_style(session_name)
    await bind_session_keys(session_name)
    await bind_click_to_swap(session_name)

    return launched


async def revive_worker(worker: Worker, session_name: str | None = None) -> None:
    """Revive a stung (exited) worker.

    If the pane still exists (natural exit), send ``claude --continue``.
    If the pane is gone (killed), create a new pane and start claude fresh.
    """
    from swarm.tmux.cell import pane_exists

    if await pane_exists(worker.pane_id):
        await send_keys(worker.pane_id, "claude --continue", enter=True)
    elif session_name:
        # Pane was killed — recreate it
        new_pane_id = await hive.add_pane(session_name, worker.window_index, worker.path)
        await hive.set_pane_option(new_pane_id, "@swarm_name", worker.name)
        await hive.set_pane_option(new_pane_id, "@swarm_state", "BUZZING")
        worker.pane_id = new_pane_id
        await send_keys(new_pane_id, "claude", enter=True)
        _log.info("revived %s with new pane %s", worker.name, new_pane_id)
    else:
        _log.warning("cannot revive %s — pane gone and no session_name", worker.name)


async def add_worker_live(
    session_name: str,
    worker_config: WorkerConfig,
    workers: list[Worker],
    panes_per_window: int = 8,
) -> Worker:
    """Add a new worker pane to a running session.

    Opens a shell at the project path (no auto-start claude).
    """
    # Find the last window and its pane count
    window_indices = await hive.list_window_indices(session_name)
    last_window = window_indices[-1] if window_indices else "0"
    pane_count = await hive.count_panes(session_name, last_window)

    path = str(worker_config.resolved_path)

    if pane_count >= panes_per_window:
        # New window
        win_idx = await hive.add_window(session_name, worker_config.name, path)
        pane_id = await get_pane_id(f"{session_name}:{win_idx}.0")
    else:
        # Split pane in last window
        win_idx = last_window
        pane_id = await hive.add_pane(session_name, last_window, path)

    await hive.set_pane_option(pane_id, "@swarm_name", worker_config.name)
    await hive.set_pane_option(pane_id, "@swarm_state", "RESTING")

    worker = Worker(
        name=worker_config.name,
        path=path,
        pane_id=pane_id,
        window_index=win_idx,
        state=WorkerState.RESTING,
    )
    workers.append(worker)
    _log.info("live-added worker %s at %s (pane %s)", worker_config.name, path, pane_id)
    return worker


async def kill_worker(worker: Worker) -> None:
    """Kill a specific worker pane."""
    from swarm.tmux.cell import send_interrupt

    try:
        await send_interrupt(worker.pane_id)
    except TmuxError:
        _log.debug("interrupt failed for %s (pane may be gone)", worker.name)
    try:
        await hive.kill_pane(worker.pane_id)
    except TmuxError:
        _log.debug("kill-pane failed for %s (pane may already be gone)", worker.name)
