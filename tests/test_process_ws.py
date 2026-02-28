"""Tests for process.py WebSocket cleanup fixes (A4)."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from swarm.pty.process import WorkerProcess


def _make_fake_ws(*, closed: bool = False) -> MagicMock:
    """Create a fake aiohttp WebSocketResponse."""
    ws = MagicMock()
    ws.closed = closed
    ws.send_bytes = AsyncMock()
    return ws


@pytest.mark.asyncio
async def test_dead_sender_task_cleaned_up():
    """Dead sender tasks should be pruned on next feed_output call."""
    proc = WorkerProcess(name="ws-test", cwd="/tmp")

    # Create a subscriber with a done task — mark ws as closed so
    # feed_output won't re-create a sender task for it.
    ws = _make_fake_ws(closed=True)
    ws_id = id(ws)
    proc._ws_subscribers.add(ws)
    queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=50)
    proc._ws_queues[ws_id] = queue

    # Create a task that's already done
    done_task = asyncio.get_running_loop().create_future()
    done_task.set_result(None)
    wrapped = asyncio.ensure_future(done_task)
    await asyncio.sleep(0)
    proc._ws_tasks[ws_id] = wrapped

    assert wrapped.done()
    assert ws_id in proc._ws_tasks
    assert ws_id in proc._ws_queues

    # feed_output should prune the dead task (and the closed ws)
    proc.feed_output(b"hello")

    assert ws_id not in proc._ws_tasks
    assert ws_id not in proc._ws_queues


@pytest.mark.asyncio
async def test_cleanup_ws_removes_all():
    """cleanup_ws() should cancel all sender tasks and clear all WS state."""
    proc = WorkerProcess(name="ws-cleanup", cwd="/tmp")

    # Add multiple subscribers
    subs = []
    for _ in range(3):
        ws = _make_fake_ws()
        ws_id = id(ws)
        proc._ws_subscribers.add(ws)
        queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=50)
        proc._ws_queues[ws_id] = queue
        task = asyncio.get_running_loop().create_task(asyncio.sleep(9999))
        proc._ws_tasks[ws_id] = task
        subs.append((ws, ws_id, task))

    assert len(proc._ws_subscribers) == 3
    assert len(proc._ws_queues) == 3
    assert len(proc._ws_tasks) == 3

    proc.cleanup_ws()

    assert len(proc._ws_subscribers) == 0
    assert len(proc._ws_queues) == 0
    assert len(proc._ws_tasks) == 0

    # Let cancellation propagate
    await asyncio.sleep(0)

    # All tasks should be cancelled
    for _, _, task in subs:
        assert task.cancelled()
