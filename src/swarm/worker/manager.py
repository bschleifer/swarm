"""Worker lifecycle management: spawn, kill, revive workers in tmux."""

from __future__ import annotations

from swarm.config import WorkerConfig
from swarm.logging import get_logger
from swarm.tmux import hive
from swarm.tmux.cell import TmuxError, send_keys
from swarm.tmux.layout import plan_layout
from swarm.tmux.style import apply_session_style, bind_session_keys
from swarm.worker.worker import Worker

_log = get_logger("worker.manager")


async def launch_hive(session_name: str, workers: list[WorkerConfig], panes_per_window: int = 4) -> list[Worker]:
    """Launch all workers in a tmux session, return Worker objects."""
    if await hive.session_exists(session_name):
        await hive.kill_session(session_name)

    windows = plan_layout(workers, panes_per_window)
    launched: list[Worker] = []

    for win_idx, window_workers in enumerate(windows):
        first = window_workers[0]

        if win_idx == 0:
            # Create session with first pane
            await hive.create_session(session_name, first.name, str(first.resolved_path))
            pane_id = f"{session_name}:0.0"
            # Set pane metadata BEFORE sending claude command
            await hive.set_pane_option(pane_id, "@swarm_name", first.name)
            await hive.set_pane_option(pane_id, "@swarm_state", "BUZZING")
            await send_keys(pane_id, "claude", enter=True)
            launched.append(Worker(name=first.name, path=str(first.resolved_path), pane_id=pane_id))
            remaining = window_workers[1:]
            current_window = "0"
        else:
            # New window
            current_window = await hive.add_window(session_name, first.name, str(first.resolved_path))
            pane_id = f"{session_name}:{current_window}.0"
            # Set pane metadata BEFORE sending claude command
            await hive.set_pane_option(pane_id, "@swarm_name", first.name)
            await hive.set_pane_option(pane_id, "@swarm_state", "BUZZING")
            await send_keys(pane_id, "claude", enter=True)
            launched.append(Worker(name=first.name, path=str(first.resolved_path), pane_id=pane_id))
            remaining = window_workers[1:]

        for worker in remaining:
            new_pane_id = await hive.add_pane(session_name, current_window, str(worker.resolved_path))
            # Set pane metadata BEFORE sending claude command
            await hive.set_pane_option(new_pane_id, "@swarm_name", worker.name)
            await hive.set_pane_option(new_pane_id, "@swarm_state", "BUZZING")
            await send_keys(new_pane_id, "claude", enter=True)
            launched.append(Worker(name=worker.name, path=str(worker.resolved_path), pane_id=new_pane_id))

    await apply_session_style(session_name)
    await bind_session_keys(session_name)

    return launched


async def revive_worker(worker: Worker) -> None:
    """Revive a stung (exited) worker by running claude --continue."""
    await send_keys(worker.pane_id, "claude --continue", enter=True)


async def kill_worker(worker: Worker) -> None:
    """Kill a specific worker pane."""
    from swarm.tmux.cell import _run_tmux, send_interrupt
    try:
        await send_interrupt(worker.pane_id)
    except TmuxError:
        _log.debug("interrupt failed for %s (pane may be gone)", worker.name)
    try:
        await _run_tmux("kill-pane", "-t", worker.pane_id)
    except TmuxError:
        _log.debug("kill-pane failed for %s (pane may already be gone)", worker.name)
