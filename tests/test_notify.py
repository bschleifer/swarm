"""Tests for notify/bus.py — notification event bus."""


from swarm.notify.bus import EventType, NotificationBus, NotifyEvent, Severity


class TestNotificationBus:
    def test_emit_calls_backend(self):
        bus = NotificationBus(debounce_seconds=0)
        received = []
        bus.add_backend(lambda e: received.append(e))

        bus.emit(NotifyEvent(
            event_type=EventType.WORKER_IDLE,
            title="test",
            message="hello",
        ))
        assert len(received) == 1
        assert received[0].title == "test"

    def test_debounce(self):
        bus = NotificationBus(debounce_seconds=10.0)
        received = []
        bus.add_backend(lambda e: received.append(e))

        bus.emit(NotifyEvent(
            event_type=EventType.WORKER_IDLE,
            title="first",
            message="",
            worker_name="api",
        ))
        bus.emit(NotifyEvent(
            event_type=EventType.WORKER_IDLE,
            title="second",
            message="",
            worker_name="api",
        ))
        # Second should be debounced
        assert len(received) == 1

    def test_different_events_not_debounced(self):
        bus = NotificationBus(debounce_seconds=10.0)
        received = []
        bus.add_backend(lambda e: received.append(e))

        bus.emit(NotifyEvent(
            event_type=EventType.WORKER_IDLE,
            title="idle",
            message="",
            worker_name="api",
        ))
        bus.emit(NotifyEvent(
            event_type=EventType.WORKER_STUNG,
            title="stung",
            message="",
            worker_name="api",
        ))
        assert len(received) == 2

    def test_helper_methods(self):
        bus = NotificationBus(debounce_seconds=0)
        received = []
        bus.add_backend(lambda e: received.append(e))

        bus.emit_worker_idle("api")
        bus.emit_worker_stung("web")
        bus.emit_escalation("tests", "stuck")
        bus.emit_task_assigned("api", "Fix bug")
        bus.emit_task_completed("api", "Fix bug")

        assert len(received) == 5
        assert received[0].event_type == EventType.WORKER_IDLE
        assert received[1].event_type == EventType.WORKER_STUNG
        assert received[1].severity == Severity.WARNING
        assert received[2].event_type == EventType.WORKER_ESCALATED
        assert received[2].severity == Severity.URGENT
        assert received[3].event_type == EventType.TASK_ASSIGNED
        assert received[4].event_type == EventType.TASK_COMPLETED

    def test_backend_error_doesnt_crash(self):
        bus = NotificationBus(debounce_seconds=0)
        bus.add_backend(lambda e: 1 / 0)  # Will raise ZeroDivisionError
        received = []
        bus.add_backend(lambda e: received.append(e))

        # Should not raise
        bus.emit_worker_idle("api")
        # Second backend should still receive
        assert len(received) == 1

    def test_multiple_backends(self):
        bus = NotificationBus(debounce_seconds=0)
        r1, r2 = [], []
        bus.add_backend(lambda e: r1.append(e))
        bus.add_backend(lambda e: r2.append(e))

        bus.emit_worker_idle("api")
        assert len(r1) == 1
        assert len(r2) == 1
