"""EventEmitter mixin — lightweight pub/sub for internal components."""

from __future__ import annotations

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
        self._event_listeners: dict[str, list] = {}

    def on(self, event: str, callback) -> None:
        """Register a callback for *event*."""
        if not hasattr(self, "_event_listeners"):
            self.__init_emitter__()
        self._event_listeners.setdefault(event, []).append(callback)

    def emit(self, event: str, *args, **kwargs) -> None:
        """Fire all callbacks registered for *event*.

        Each callback is wrapped in try/except so one bad listener
        cannot break others.
        """
        if not hasattr(self, "_event_listeners"):
            return
        for cb in self._event_listeners.get(event, []):
            try:
                cb(*args, **kwargs)
            except Exception:
                _log.warning(
                    "listener for event %r failed", event, exc_info=True
                )
