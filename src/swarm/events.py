"""EventEmitter mixin — lightweight pub/sub for internal components."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Callable

from swarm.logging import get_logger

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
