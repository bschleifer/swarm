"""EventEmitter mixin — lightweight pub/sub for internal components."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Callable
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from swarm.logging import get_logger

if TYPE_CHECKING:
    from swarm.queen.oversight import OversightResult, OversightSignal
    from swarm.tasks.proposal import AssignmentProposal
    from swarm.tasks.task import SwarmTask
    from swarm.worker.worker import Worker


# ---------------------------------------------------------------------------
# Typed callback protocols — used by on_*() wrappers for static analysis.
# These are NOT enforced at runtime; the generic ``on(event, cb)`` still
# accepts ``Callable[..., None]``.
# ---------------------------------------------------------------------------


@runtime_checkable
class WorkerCallback(Protocol):
    """Callback receiving a single Worker (state_changed, etc.)."""

    def __call__(self, worker: Worker) -> None: ...


@runtime_checkable
class EscalateCallback(Protocol):
    """Callback for escalation events: (worker, reason)."""

    def __call__(self, worker: Worker, reason: str) -> None: ...


@runtime_checkable
class TaskAssignedCallback(Protocol):
    """Callback for task assignment: (worker, task, message)."""

    def __call__(self, worker: Worker, task: SwarmTask, message: str) -> None: ...


@runtime_checkable
class TaskDoneCallback(Protocol):
    """Callback for task completion: (worker, task, resolution)."""

    def __call__(self, worker: Worker, task: SwarmTask, resolution: str) -> None: ...


@runtime_checkable
class ProposalCallback(Protocol):
    """Callback for Queen proposals."""

    def __call__(self, proposal: AssignmentProposal) -> None: ...


@runtime_checkable
class VoidCallback(Protocol):
    """Callback with no arguments (hive_empty, hive_complete, workers_changed)."""

    def __call__(self) -> None: ...


@runtime_checkable
class OversightAlertCallback(Protocol):
    """Callback for oversight alerts: (worker, signal, result)."""

    def __call__(
        self, worker: Worker, signal: OversightSignal, result: OversightResult
    ) -> None: ...


_log = get_logger("events")


class EventEmitter:
    """Mixin that adds event registration and emission.

    Usage::

        class MyComponent(EventEmitter):
            def do_something(self):
                self.emit("changed", self)

        comp = MyComponent()
        comp.on("changed", lambda c: print(c))
    """

    def __init_emitter__(self) -> None:
        """Call from subclass __init__ to initialize the event store."""
        self._event_listeners: dict[str, list[Callable[..., None]]] = {}

    def on(self, event: str, callback: Callable[..., None]) -> None:
        """Register a callback for *event*."""
        if not hasattr(self, "_event_listeners"):
            self.__init_emitter__()
        self._event_listeners.setdefault(event, []).append(callback)

    def off(self, event: str, callback: Callable[..., None]) -> None:
        """Remove a callback for *event*. No-op if not registered."""
        if not hasattr(self, "_event_listeners"):
            return
        try:
            self._event_listeners.get(event, []).remove(callback)
        except ValueError:
            pass

    def remove_all_listeners(self, event: str | None = None) -> None:
        """Remove all listeners, or all for a specific *event*."""
        if not hasattr(self, "_event_listeners"):
            return
        if event is None:
            self._event_listeners.clear()
        else:
            self._event_listeners.pop(event, None)

    def emit(self, event: str, *args: object, **kwargs: object) -> None:
        """Fire all callbacks registered for *event*.

        Each callback is wrapped in try/except so one bad listener
        cannot break others.  If a callback returns a coroutine, it is
        scheduled on the running event loop.
        """
        if not hasattr(self, "_event_listeners"):
            return
        for cb in self._event_listeners.get(event, []):
            try:
                result = cb(*args, **kwargs)
                if inspect.isawaitable(result):
                    try:
                        task = asyncio.ensure_future(result)
                        task.add_done_callback(
                            lambda t: t.exception() if not t.cancelled() else None
                        )
                    except RuntimeError:
                        _log.debug("no event loop to schedule async listener for %r", event)
            except Exception:
                _log.warning("listener for event %r failed", event, exc_info=True)
