"""Tests for NotificationBus — initialization, backend management, debouncing,
severity routing, helper methods, and concurrency."""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import MagicMock

from swarm.config import NotifyConfig
from swarm.notify.bus import (
    _DEFAULT_SEVERITY,
    EventType,
    NotificationBus,
    NotifyEvent,
    Severity,
)

# ---------------------------------------------------------------------------
# 1. Initialization with config
# ---------------------------------------------------------------------------


class TestInitialization:
    def test_default_debounce(self) -> None:
        bus = NotificationBus()
        assert bus._debounce == 5.0
        assert bus._backends == []
        assert bus._last_sent == {}

    def test_custom_debounce(self) -> None:
        bus = NotificationBus(debounce_seconds=2.5)
        assert bus._debounce == 2.5

    def test_zero_debounce(self) -> None:
        bus = NotificationBus(debounce_seconds=0)
        assert bus._debounce == 0

    def test_config_debounce_value_matches(self) -> None:
        """NotifyConfig.debounce_seconds should be usable to create a bus."""
        cfg = NotifyConfig(debounce_seconds=7.0)
        bus = NotificationBus(debounce_seconds=cfg.debounce_seconds)
        assert bus._debounce == 7.0

    def test_notify_config_defaults(self) -> None:
        cfg = NotifyConfig()
        assert cfg.terminal_bell is True
        assert cfg.desktop is True
        assert cfg.debounce_seconds == 5.0


# ---------------------------------------------------------------------------
# 2. Adding and removing backends
# ---------------------------------------------------------------------------


class TestBackendManagement:
    def test_add_single_backend(self) -> None:
        bus = NotificationBus(debounce_seconds=0)
        backend = MagicMock()
        bus.add_backend(backend)
        assert len(bus._backends) == 1
        assert bus._backends[0] is backend

    def test_add_multiple_backends(self) -> None:
        bus = NotificationBus(debounce_seconds=0)
        b1, b2, b3 = MagicMock(), MagicMock(), MagicMock()
        bus.add_backend(b1)
        bus.add_backend(b2)
        bus.add_backend(b3)
        assert len(bus._backends) == 3

        bus.emit_worker_idle("w1")
        b1.assert_called_once()
        b2.assert_called_once()
        b3.assert_called_once()

    def test_remove_backend_via_list(self) -> None:
        """Backends are stored in a plain list; removing is possible via _backends."""
        bus = NotificationBus(debounce_seconds=0)
        b1 = MagicMock()
        b2 = MagicMock()
        bus.add_backend(b1)
        bus.add_backend(b2)

        bus._backends.remove(b1)
        bus.emit_worker_idle("w1")
        b1.assert_not_called()
        b2.assert_called_once()

    def test_no_backends_emit_does_not_raise(self) -> None:
        """Emit with zero backends should silently succeed."""
        bus = NotificationBus(debounce_seconds=0)
        bus.emit(
            NotifyEvent(
                event_type=EventType.WORKER_IDLE,
                title="idle",
                message="",
            )
        )
        # No exception means success

    def test_backend_exception_does_not_block_others(self) -> None:
        bus = NotificationBus(debounce_seconds=0)
        failing = MagicMock(side_effect=RuntimeError("boom"))
        succeeding = MagicMock()
        bus.add_backend(failing)
        bus.add_backend(succeeding)

        bus.emit_worker_idle("api")
        failing.assert_called_once()
        succeeding.assert_called_once()


# ---------------------------------------------------------------------------
# 3. Debounce prevents rapid duplicate notifications
# ---------------------------------------------------------------------------


class TestDebounce:
    def test_same_event_debounced(self) -> None:
        bus = NotificationBus(debounce_seconds=60.0)
        received: list[NotifyEvent] = []
        bus.add_backend(received.append)

        bus.emit_worker_idle("api")
        bus.emit_worker_idle("api")
        bus.emit_worker_idle("api")

        assert len(received) == 1

    def test_debounce_key_includes_worker_name(self) -> None:
        """Same event type but different worker names should not debounce."""
        bus = NotificationBus(debounce_seconds=60.0)
        received: list[NotifyEvent] = []
        bus.add_backend(received.append)

        bus.emit_worker_idle("api")
        bus.emit_worker_idle("web")
        bus.emit_worker_idle("tests")

        assert len(received) == 3

    def test_debounce_key_includes_event_type(self) -> None:
        """Different event types for the same worker should not debounce."""
        bus = NotificationBus(debounce_seconds=60.0)
        received: list[NotifyEvent] = []
        bus.add_backend(received.append)

        bus.emit_worker_idle("api")
        bus.emit_worker_stung("api")
        bus.emit_escalation("api", "stuck")

        assert len(received) == 3

    def test_debounce_expires(self) -> None:
        """After the debounce window passes, the same event should be emitted again."""
        bus = NotificationBus(debounce_seconds=0.05)
        received: list[NotifyEvent] = []
        bus.add_backend(received.append)

        bus.emit_worker_idle("api")
        assert len(received) == 1

        time.sleep(0.1)  # Wait for debounce to expire

        bus.emit_worker_idle("api")
        assert len(received) == 2

    def test_debounce_zero_allows_all(self) -> None:
        bus = NotificationBus(debounce_seconds=0)
        received: list[NotifyEvent] = []
        bus.add_backend(received.append)

        for _ in range(10):
            bus.emit_worker_idle("api")

        assert len(received) == 10

    def test_debounce_key_for_event_without_worker(self) -> None:
        """Events without a worker_name should use empty string in the key."""
        bus = NotificationBus(debounce_seconds=60.0)
        received: list[NotifyEvent] = []
        bus.add_backend(received.append)

        bus.emit(
            NotifyEvent(
                event_type=EventType.DRONE_ACTION,
                title="action1",
                message="",
                worker_name=None,
            )
        )
        bus.emit(
            NotifyEvent(
                event_type=EventType.DRONE_ACTION,
                title="action2",
                message="",
                worker_name=None,
            )
        )
        # Same key (drone_action:) — second should be debounced
        assert len(received) == 1


# ---------------------------------------------------------------------------
# 4. Severity routing (only escalations trigger desktop)
# ---------------------------------------------------------------------------


class TestSeverityRouting:
    def test_default_severities(self) -> None:
        """Verify the default severity map for all event types."""
        assert _DEFAULT_SEVERITY[EventType.WORKER_IDLE] == Severity.INFO
        assert _DEFAULT_SEVERITY[EventType.WORKER_STUNG] == Severity.WARNING
        assert _DEFAULT_SEVERITY[EventType.WORKER_ESCALATED] == Severity.URGENT
        assert _DEFAULT_SEVERITY[EventType.DRONE_ACTION] == Severity.INFO
        assert _DEFAULT_SEVERITY[EventType.QUEEN_RESPONSE] == Severity.INFO
        assert _DEFAULT_SEVERITY[EventType.TASK_ASSIGNED] == Severity.INFO
        assert _DEFAULT_SEVERITY[EventType.TASK_COMPLETED] == Severity.INFO

    def test_escalation_has_urgent_severity(self) -> None:
        bus = NotificationBus(debounce_seconds=0)
        received: list[NotifyEvent] = []
        bus.add_backend(received.append)

        bus.emit_escalation("api", "worker stuck")
        assert received[0].severity == Severity.URGENT

    def test_idle_has_info_severity(self) -> None:
        bus = NotificationBus(debounce_seconds=0)
        received: list[NotifyEvent] = []
        bus.add_backend(received.append)

        bus.emit_worker_idle("api")
        assert received[0].severity == Severity.INFO

    def test_stung_has_warning_severity(self) -> None:
        bus = NotificationBus(debounce_seconds=0)
        received: list[NotifyEvent] = []
        bus.add_backend(received.append)

        bus.emit_worker_stung("api")
        assert received[0].severity == Severity.WARNING

    def test_severity_filtering_backend(self) -> None:
        """A backend that only acts on WARNING+ should ignore INFO events."""
        bus = NotificationBus(debounce_seconds=0)
        desktop_events: list[NotifyEvent] = []

        def desktop_backend(event: NotifyEvent) -> None:
            if event.severity in (Severity.WARNING, Severity.URGENT):
                desktop_events.append(event)

        bus.add_backend(desktop_backend)

        bus.emit_worker_idle("api")  # INFO — should be ignored
        bus.emit_task_assigned("api", "Fix bug")  # INFO — should be ignored
        bus.emit_worker_stung("api")  # WARNING — should trigger
        bus.emit_escalation("api", "stuck")  # URGENT — should trigger

        assert len(desktop_events) == 2
        assert desktop_events[0].severity == Severity.WARNING
        assert desktop_events[1].severity == Severity.URGENT


# ---------------------------------------------------------------------------
# 5. emit_escalation sends notification
# ---------------------------------------------------------------------------


class TestEmitEscalation:
    def test_escalation_event_fields(self) -> None:
        bus = NotificationBus(debounce_seconds=0)
        received: list[NotifyEvent] = []
        bus.add_backend(received.append)

        bus.emit_escalation("web", "too many failures")
        event = received[0]

        assert event.event_type == EventType.WORKER_ESCALATED
        assert event.severity == Severity.URGENT
        assert event.worker_name == "web"
        assert "web" in event.title
        assert "escalated" in event.title
        assert "too many failures" in event.message

    def test_escalation_reaches_all_backends(self) -> None:
        bus = NotificationBus(debounce_seconds=0)
        r1: list[NotifyEvent] = []
        r2: list[NotifyEvent] = []
        bus.add_backend(r1.append)
        bus.add_backend(r2.append)

        bus.emit_escalation("api", "stuck")
        assert len(r1) == 1
        assert len(r2) == 1
        assert r1[0].event_type == EventType.WORKER_ESCALATED
        assert r2[0].event_type == EventType.WORKER_ESCALATED

    def test_escalation_timestamp_set(self) -> None:
        before = time.time()
        bus = NotificationBus(debounce_seconds=0)
        received: list[NotifyEvent] = []
        bus.add_backend(received.append)

        bus.emit_escalation("api", "reason")
        after = time.time()

        assert before <= received[0].timestamp <= after


# ---------------------------------------------------------------------------
# 6. emit_task_assigned sends notification
# ---------------------------------------------------------------------------


class TestEmitTaskAssigned:
    def test_task_assigned_event_fields(self) -> None:
        bus = NotificationBus(debounce_seconds=0)
        received: list[NotifyEvent] = []
        bus.add_backend(received.append)

        bus.emit_task_assigned("api", "Implement login")
        event = received[0]

        assert event.event_type == EventType.TASK_ASSIGNED
        assert event.severity == Severity.INFO
        assert event.worker_name == "api"
        assert "api" in event.title
        assert "Implement login" in event.message

    def test_task_assigned_title_format(self) -> None:
        bus = NotificationBus(debounce_seconds=0)
        received: list[NotifyEvent] = []
        bus.add_backend(received.append)

        bus.emit_task_assigned("web", "Fix CSS")
        # Title format is "Task -> worker_name"
        assert received[0].title == "Task \u2192 web"

    def test_task_completed_event_fields(self) -> None:
        bus = NotificationBus(debounce_seconds=0)
        received: list[NotifyEvent] = []
        bus.add_backend(received.append)

        bus.emit_task_completed("api", "Deploy hotfix")
        event = received[0]

        assert event.event_type == EventType.TASK_COMPLETED
        assert event.severity == Severity.INFO
        assert event.worker_name == "api"
        assert "Deploy hotfix" in event.title
        assert "api" in event.message


# ---------------------------------------------------------------------------
# 7. Concurrent notifications handled correctly
# ---------------------------------------------------------------------------


class TestConcurrency:
    def test_concurrent_emits_no_crash(self) -> None:
        """Multiple threads emitting simultaneously should not raise."""
        bus = NotificationBus(debounce_seconds=0)
        received: list[NotifyEvent] = []
        bus.add_backend(received.append)

        def emit_batch(worker_name: str) -> None:
            for _ in range(20):
                bus.emit_worker_idle(worker_name)

        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = [executor.submit(emit_batch, f"worker-{i}") for i in range(4)]
            for f in futures:
                f.result()  # Raises if any thread crashed

        # Each worker emitted 20 times with debounce=0, so all should arrive.
        # With threads, list.append is GIL-protected, so count should be exact.
        assert len(received) == 80

    def test_concurrent_debounce_is_safe(self) -> None:
        """Debounce dict access from multiple threads should not corrupt state."""
        bus = NotificationBus(debounce_seconds=60.0)
        received: list[NotifyEvent] = []
        bus.add_backend(received.append)

        def emit_same_event() -> None:
            for _ in range(50):
                bus.emit_worker_idle("shared")

        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = [executor.submit(emit_same_event) for _ in range(4)]
            for f in futures:
                f.result()

        # With 60s debounce, only the very first emit should succeed.
        # Race conditions might allow a small number of extras, but not many.
        assert len(received) <= 4  # At most one per thread (race at start)
        assert len(received) >= 1  # At least one must have gotten through

    def test_concurrent_different_workers_independent(self) -> None:
        """Different workers should not interfere with each other's debounce."""
        bus = NotificationBus(debounce_seconds=60.0)
        received: list[NotifyEvent] = []
        bus.add_backend(received.append)

        def emit_for_worker(name: str) -> None:
            bus.emit_worker_idle(name)

        worker_names = [f"worker-{i}" for i in range(10)]
        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = [executor.submit(emit_for_worker, name) for name in worker_names]
            for f in futures:
                f.result()

        # Each unique worker should have exactly 1 notification
        assert len(received) == 10
        names = {e.worker_name for e in received}
        assert names == set(worker_names)


# ---------------------------------------------------------------------------
# Additional edge cases
# ---------------------------------------------------------------------------


class TestNotifyEventDataclass:
    def test_event_default_severity(self) -> None:
        event = NotifyEvent(
            event_type=EventType.WORKER_IDLE,
            title="test",
            message="msg",
        )
        assert event.severity == Severity.INFO

    def test_event_custom_severity(self) -> None:
        event = NotifyEvent(
            event_type=EventType.WORKER_IDLE,
            title="test",
            message="msg",
            severity=Severity.URGENT,
        )
        assert event.severity == Severity.URGENT

    def test_event_worker_name_default_none(self) -> None:
        event = NotifyEvent(
            event_type=EventType.DRONE_ACTION,
            title="action",
            message="msg",
        )
        assert event.worker_name is None

    def test_event_timestamp_auto_set(self) -> None:
        before = time.time()
        event = NotifyEvent(
            event_type=EventType.TASK_COMPLETED,
            title="done",
            message="finished",
        )
        after = time.time()
        assert before <= event.timestamp <= after


class TestEventTypeEnum:
    def test_all_event_types_have_default_severity(self) -> None:
        """Every EventType should have an entry in _DEFAULT_SEVERITY."""
        for et in EventType:
            assert et in _DEFAULT_SEVERITY, f"{et} missing from _DEFAULT_SEVERITY"

    def test_event_type_values_are_strings(self) -> None:
        for et in EventType:
            assert isinstance(et.value, str)

    def test_severity_values_are_strings(self) -> None:
        for s in Severity:
            assert isinstance(s.value, str)
