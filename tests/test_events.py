"""Direct tests for EventEmitter mixin."""

from __future__ import annotations

from swarm.events import EventEmitter


class _Emitter(EventEmitter):
    """Concrete subclass for testing."""

    def __init__(self) -> None:
        self.__init_emitter__()


# ---------------------------------------------------------------------------
# Registration & emission
# ---------------------------------------------------------------------------


class TestOnAndEmit:
    def test_callback_invoked_on_emit(self):
        e = _Emitter()
        results: list[str] = []
        e.on("ping", lambda: results.append("pong"))
        e.emit("ping")
        assert results == ["pong"]

    def test_multiple_listeners(self):
        e = _Emitter()
        results: list[int] = []
        e.on("evt", lambda: results.append(1))
        e.on("evt", lambda: results.append(2))
        e.emit("evt")
        assert results == [1, 2]

    def test_emit_passes_args(self):
        e = _Emitter()
        received: list[tuple] = []
        e.on("data", lambda x, y: received.append((x, y)))
        e.emit("data", "a", "b")
        assert received == [("a", "b")]

    def test_emit_passes_kwargs(self):
        e = _Emitter()
        received: list[dict] = []
        e.on("data", lambda **kw: received.append(kw))
        e.emit("data", key="val")
        assert received == [{"key": "val"}]

    def test_emit_unknown_event_is_noop(self):
        e = _Emitter()
        e.emit("nonexistent")  # should not raise

    def test_emit_without_init_is_noop(self):
        """Emit on an emitter that never called __init_emitter__."""
        e = EventEmitter()
        e.emit("whatever")  # should not raise


# ---------------------------------------------------------------------------
# Unregistration
# ---------------------------------------------------------------------------


class TestOff:
    def test_off_removes_callback(self):
        e = _Emitter()
        results: list[str] = []
        cb = lambda: results.append("x")  # noqa: E731
        e.on("evt", cb)
        e.off("evt", cb)
        e.emit("evt")
        assert results == []

    def test_off_unknown_callback_is_noop(self):
        e = _Emitter()
        e.off("evt", lambda: None)  # should not raise

    def test_off_unknown_event_is_noop(self):
        e = _Emitter()
        e.off("nonexistent", lambda: None)  # should not raise

    def test_off_without_init_is_noop(self):
        e = EventEmitter()
        e.off("evt", lambda: None)  # should not raise


# ---------------------------------------------------------------------------
# Lazy initialization
# ---------------------------------------------------------------------------


class TestLazyInit:
    def test_on_auto_inits(self):
        """Calling on() without __init_emitter__() should still work."""
        e = EventEmitter()
        results: list[int] = []
        e.on("x", lambda: results.append(1))
        e.emit("x")
        assert results == [1]


# ---------------------------------------------------------------------------
# Exception isolation
# ---------------------------------------------------------------------------


class TestExceptionIsolation:
    def test_bad_listener_does_not_break_others(self):
        e = _Emitter()
        results: list[str] = []
        e.on("evt", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
        e.on("evt", lambda: results.append("ok"))
        e.emit("evt")
        assert results == ["ok"]

    def test_bad_listener_logged_not_raised(self):
        """Emit should not propagate listener exceptions to the caller."""
        e = _Emitter()

        def _explode():
            raise ValueError("kaboom")

        e.on("evt", _explode)
        # Should not raise
        e.emit("evt")
