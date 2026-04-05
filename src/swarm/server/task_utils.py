"""Shared helpers for fire-and-forget asyncio tasks."""

from __future__ import annotations

import asyncio

from swarm.logging import get_logger

_log = get_logger("server.task_utils")


def log_task_exception(task: asyncio.Task[object]) -> None:
    """Log unhandled exceptions from fire-and-forget tasks.

    Attach via ``task.add_done_callback(log_task_exception)`` so that
    exceptions in detached tasks surface in the logs instead of being
    silently dropped by the event loop.
    """
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        _log.error("fire-and-forget task failed: %s", exc, exc_info=exc)
