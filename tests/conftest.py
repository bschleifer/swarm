"""Shared test fixtures and helpers."""

from __future__ import annotations

from swarm.worker.worker import Worker, WorkerState


def make_worker(
    name: str = "api",
    state: WorkerState = WorkerState.BUZZING,
    pane_id: str | None = None,
    resting_since: float | None = None,
    revive_count: int = 0,
) -> Worker:
    """Create a Worker for testing.

    Parameters
    ----------
    name:
        Worker name (also used to derive a default ``pane_id``).
    state:
        Initial worker state.
    pane_id:
        Explicit tmux pane ID.  Defaults to ``%{name}``.
    resting_since:
        If set, overrides ``state_since`` (useful for escalation threshold tests).
    revive_count:
        Initial revive counter.
    """
    if pane_id is None:
        pane_id = f"%{name}"
    w = Worker(name=name, path="/tmp", pane_id=pane_id, state=state)
    if resting_since is not None:
        w.state_since = resting_since
    w.revive_count = revive_count
    return w
