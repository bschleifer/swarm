"""QueenCallQueue — FIFO queue with concurrency control for Queen subprocess calls."""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from typing import Any, TypedDict

from swarm.logging import get_logger

_log = get_logger("queen.queue")


class QueueStatus(TypedDict):
    running: int
    queued: int
    total: int
    running_keys: list[str]
    queued_keys: list[str]


@dataclass
class QueenCallRequest:
    """A single queued Queen call."""

    call_type: str  # "escalation" | "completion"
    coro_factory: Callable[[], Any]  # Creates the coroutine when ready
    worker_name: str | None  # For staleness check
    worker_state_at_enqueue: str | None  # Snapshot for staleness
    dedup_key: str  # e.g. "escalation:api" or "completion:api:t1"
    force: bool  # Bypass queue (user-initiated)
    future: asyncio.Future[Any] | None = field(default=None)


class QueenCallQueue:
    """FIFO queue limiting concurrent background Queen subprocess calls.

    - Background calls (escalations, completions) are limited to ``max_concurrent``.
    - User-initiated calls (``force=True``) bypass the queue entirely.
    - Dedup: only one call per ``dedup_key`` can be queued or running.
    - Staleness: calls are dropped at dequeue time if the worker's state changed.
    """

    def __init__(
        self,
        max_concurrent: int = 2,
        on_status_change: Callable[[QueueStatus], None] | None = None,
        get_worker_state: Callable[[str], str | None] | None = None,
    ) -> None:
        self._max_concurrent = max_concurrent
        self._on_status_change = on_status_change
        self._get_worker_state = get_worker_state
        self._queue: deque[QueenCallRequest] = deque()
        self._running: dict[str, QueenCallRequest] = {}  # dedup_key -> req
        self._all_keys: set[str] = set()  # queued + running dedup keys
        self._background_tasks: set[asyncio.Task[None]] = set()

    def _track_task(self, coro: Coroutine[Any, Any, None]) -> None:
        """Create a background task and prevent it from being GC'd."""
        task = asyncio.create_task(coro)
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    # -- Public API --

    @staticmethod
    def _init_future(req: QueenCallRequest) -> asyncio.Future[Any]:
        """Ensure the request has a future, creating one if needed."""
        if req.future is None:
            req.future = asyncio.get_running_loop().create_future()
        return req.future

    def has_pending(self, key: str) -> bool:
        """Check if a call with the given dedup_key is queued or running."""
        return key in self._all_keys

    async def submit(self, req: QueenCallRequest) -> None:
        """Fire-and-forget: enqueue or execute immediately."""
        fut = self._init_future(req)

        if req.force:
            self._all_keys.add(req.dedup_key)
            self._running[req.dedup_key] = req
            self._notify_status()
            self._track_task(self._execute(req))
            return

        if req.dedup_key in self._all_keys:
            _log.debug("dedup: dropping duplicate queen call %s", req.dedup_key)
            if not fut.done():
                fut.set_result(None)
            return

        self._all_keys.add(req.dedup_key)

        if len(self._running) < self._max_concurrent:
            self._running[req.dedup_key] = req
            self._notify_status()
            self._track_task(self._execute(req))
        else:
            self._queue.append(req)
            self._notify_status()

    async def submit_and_wait(self, req: QueenCallRequest) -> Any:
        """Enqueue and await the result."""
        await self.submit(req)
        assert req.future is not None, "submit() must initialize future"
        return await req.future

    def clear_worker(self, worker_name: str) -> None:
        """Remove all queued (not running) calls for a worker."""
        removed: list[QueenCallRequest] = []
        remaining: deque[QueenCallRequest] = deque()
        for req in self._queue:
            if req.worker_name == worker_name:
                removed.append(req)
            else:
                remaining.append(req)
        self._queue = remaining
        for req in removed:
            self._all_keys.discard(req.dedup_key)
            if req.future is not None and not req.future.done():
                req.future.set_result(None)
        if removed:
            _log.debug("cleared %d queued calls for worker %s", len(removed), worker_name)
            self._notify_status()

    def cancel_all(self) -> None:
        """Cancel all queued calls and mark running as cancelled. For shutdown."""
        for req in self._queue:
            self._all_keys.discard(req.dedup_key)
            if req.future is not None and not req.future.done():
                req.future.cancel()
        self._queue.clear()
        for _key, req in list(self._running.items()):
            if req.future is not None and not req.future.done():
                req.future.cancel()
        self._running.clear()
        self._all_keys.clear()
        _log.info("queen queue cancelled all calls")

    def status(self) -> QueueStatus:
        """Return current queue status."""
        return QueueStatus(
            running=len(self._running),
            queued=len(self._queue),
            total=len(self._running) + len(self._queue),
            running_keys=list(self._running.keys()),
            queued_keys=[r.dedup_key for r in self._queue],
        )

    # -- Internal --

    def _is_stale(self, req: QueenCallRequest) -> bool:
        """Check if a request is stale (worker state changed since enqueue)."""
        if req.worker_name is None or req.worker_state_at_enqueue is None:
            return False
        if self._get_worker_state is None:
            return False
        current = self._get_worker_state(req.worker_name)
        if current is None:
            return True  # Worker gone
        return current != req.worker_state_at_enqueue

    async def _execute(self, req: QueenCallRequest) -> None:
        """Execute a single queen call request."""
        fut = req.future
        try:
            result = await req.coro_factory()
            if fut is not None and not fut.done():
                fut.set_result(result)
        except asyncio.CancelledError:
            if fut is not None and not fut.done():
                fut.cancel()
        except Exception as exc:
            _log.error("queen call %s failed: %s", req.dedup_key, exc, exc_info=exc)
            if fut is not None and not fut.done():
                fut.set_exception(exc)
        finally:
            self._running.pop(req.dedup_key, None)
            self._all_keys.discard(req.dedup_key)
            self._drain_queue()
            self._notify_status()

    def _drain_queue(self) -> None:
        """Start the next queued call(s) if capacity allows."""
        while self._queue and len(self._running) < self._max_concurrent:
            req = self._queue.popleft()

            # Staleness check at dequeue time
            if self._is_stale(req):
                _log.info(
                    "dropping stale queen call %s (worker %s state changed)",
                    req.dedup_key,
                    req.worker_name,
                )
                self._all_keys.discard(req.dedup_key)
                if req.future is not None and not req.future.done():
                    req.future.set_result(None)
                continue

            self._running[req.dedup_key] = req
            self._track_task(self._execute(req))

    def _notify_status(self) -> None:
        """Notify the status change callback if registered."""
        if self._on_status_change is not None:
            try:
                self._on_status_change(self.status())
            except Exception:
                _log.debug("status change callback failed", exc_info=True)
