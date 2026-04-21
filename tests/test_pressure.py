"""Tests for drones/pressure.py — PressureManager resource pressure response."""

from __future__ import annotations

import time

from swarm.drones.log import DroneLog, SystemAction
from swarm.drones.pressure import PressureManager
from swarm.resources.monitor import MemoryPressureLevel
from swarm.worker.worker import WorkerState
from tests.conftest import make_worker


def _make_pressure_manager(
    workers: list | None = None,
    log: DroneLog | None = None,
) -> tuple:
    """Create a PressureManager with test defaults."""
    if workers is None:
        workers = []
    if log is None:
        log = DroneLog()
    suspended: set[str] = set()
    suspended_at: dict[str, float] = {}
    events: list[str] = []

    def emit(event: str, *args: object) -> None:
        events.append(event)

    pm = PressureManager(
        workers=workers,
        log=log,
        pool=None,
        suspended=suspended,
        suspended_at=suspended_at,
        emit=emit,
    )
    return pm, log, suspended, suspended_at, events


def _make_sleeping_worker(name: str) -> object:
    """Create a worker that reports display_state as SLEEPING."""
    # RESTING with state_since far in the past → display_state == SLEEPING
    w = make_worker(name=name, state=WorkerState.RESTING)
    w.state_since = time.time() - 9999  # well past sleeping_threshold
    return w


def _make_resting_worker(name: str, resting_since: float | None = None) -> object:
    """Create a worker in RESTING that is NOT yet SLEEPING."""
    w = make_worker(name=name, state=WorkerState.RESTING)
    w.state_since = resting_since or time.time()  # recent → still RESTING
    return w


class TestPressureLevelTracking:
    """Verify pressure_level property updates correctly."""

    def test_initial_level_is_nominal(self) -> None:
        pm, *_ = _make_pressure_manager()
        assert pm.pressure_level == "nominal"

    def test_on_pressure_changed_updates_level(self) -> None:
        pm, *_ = _make_pressure_manager()
        pm.on_pressure_changed(MemoryPressureLevel.HIGH)
        assert pm.pressure_level == "high"

    def test_level_transitions_through_all_values(self) -> None:
        pm, *_ = _make_pressure_manager()
        for level in MemoryPressureLevel:
            pm.on_pressure_changed(level)
            assert pm.pressure_level == level.value


class TestSuspendWorkers:
    """Verify _suspend_workers bookkeeping."""

    def test_suspend_adds_to_sets(self) -> None:
        workers = [make_worker("a"), make_worker("b")]
        pm, log, suspended, suspended_at, _ = _make_pressure_manager(workers)

        count = pm._suspend_workers(["a"], "test")

        assert count == 1
        assert "a" in suspended
        assert "a" in suspended_at
        assert "a" in pm._suspended_for_pressure

    def test_suspend_skips_already_suspended(self) -> None:
        workers = [make_worker("a")]
        pm, log, suspended, suspended_at, _ = _make_pressure_manager(workers)

        pm._suspend_workers(["a"], "first")
        count = pm._suspend_workers(["a"], "second")

        assert count == 0

    def test_suspend_logs_to_drone_log(self) -> None:
        workers = [make_worker("a")]
        pm, log, *_ = _make_pressure_manager(workers)

        pm._suspend_workers(["a"], "HIGH")

        entries = [e for e in log.entries if e.action == SystemAction.SUSPENDED]
        assert len(entries) == 1
        assert entries[0].worker_name == "a"
        assert "HIGH" in entries[0].detail

    def test_suspend_multiple_workers(self) -> None:
        workers = [make_worker("a"), make_worker("b"), make_worker("c")]
        pm, log, suspended, *_ = _make_pressure_manager(workers)

        count = pm._suspend_workers(["a", "b", "c"], "test")

        assert count == 3
        assert suspended == {"a", "b", "c"}

    def test_suspend_records_timestamp(self) -> None:
        workers = [make_worker("a")]
        pm, _, _, suspended_at, _ = _make_pressure_manager(workers)

        before = time.time()
        pm._suspend_workers(["a"], "test")
        after = time.time()

        assert before <= suspended_at["a"] <= after


class TestPressureSuspendedWorkers:
    """Verify pressure_suspended_workers property."""

    def test_initially_empty(self) -> None:
        pm, *_ = _make_pressure_manager()
        assert pm.pressure_suspended_workers == []

    def test_returns_sorted_names(self) -> None:
        workers = [make_worker("c"), make_worker("a"), make_worker("b")]
        pm, *_ = _make_pressure_manager(workers)
        pm._suspend_workers(["c", "a", "b"], "test")
        assert pm.pressure_suspended_workers == ["a", "b", "c"]


class TestHighPressure:
    """Verify _suspend_on_high_pressure targets 60% active, SLEEPING only."""

    def test_suspends_sleeping_workers_to_60_percent(self) -> None:
        # 5 workers total → ceil(5*0.6) = 3 target active → suspend 2
        sleeping = [_make_sleeping_worker(f"s{i}") for i in range(3)]
        buzzing = [make_worker(f"b{i}") for i in range(2)]
        workers = sleeping + buzzing
        pm, log, suspended, _, events = _make_pressure_manager(workers)

        pm._suspend_on_high_pressure()

        assert len(pm._suspended_for_pressure) == 2
        assert "workers_changed" in events

    def test_only_suspends_sleeping_not_resting(self) -> None:
        # 3 workers: 1 sleeping, 1 resting, 1 buzzing
        # ceil(3*0.6) = 2 target → suspend 1, but only SLEEPING eligible
        sleeping = [_make_sleeping_worker("sleepy")]
        resting = [_make_resting_worker("restful")]
        buzzing = [make_worker("busy")]
        workers = sleeping + resting + buzzing
        pm, _, suspended, *_ = _make_pressure_manager(workers)

        pm._suspend_on_high_pressure()

        # Only the sleeping worker should be suspended
        assert "sleepy" in pm._suspended_for_pressure
        assert "restful" not in pm._suspended_for_pressure

    def test_no_sleeping_workers_no_suspension(self) -> None:
        workers = [make_worker(f"b{i}") for i in range(3)]
        pm, _, _, _, events = _make_pressure_manager(workers)

        pm._suspend_on_high_pressure()

        assert len(pm._suspended_for_pressure) == 0
        assert "workers_changed" not in events

    def test_single_worker_no_suspension(self) -> None:
        # 1 worker → ceil(1*0.6)=1 target → suspend 0
        workers = [_make_sleeping_worker("only")]
        pm, *_ = _make_pressure_manager(workers)

        pm._suspend_on_high_pressure()

        assert len(pm._suspended_for_pressure) == 0

    def test_emits_workers_changed_only_when_suspended(self) -> None:
        workers = [make_worker("b1")]  # no sleeping workers
        pm, _, _, _, events = _make_pressure_manager(workers)

        pm._suspend_on_high_pressure()
        assert "workers_changed" not in events

    def test_suspends_longest_sleeping_first(self) -> None:
        # 4 workers → ceil(4*0.6)=3 target → suspend 1
        # The one sleeping longest should be suspended
        s1 = _make_sleeping_worker("old_sleeper")
        s1.state_since = time.time() - 99999  # very long duration
        s2 = _make_sleeping_worker("new_sleeper")
        s2.state_since = time.time() - 2000  # shorter duration
        workers = [s1, s2, make_worker("b1"), make_worker("b2")]
        pm, *_ = _make_pressure_manager(workers)

        pm._suspend_on_high_pressure()

        # Sorted by -(state_duration), so longest sleeping first
        assert len(pm._suspended_for_pressure) == 1
        assert "old_sleeper" in pm._suspended_for_pressure

    def test_no_workers_no_crash(self) -> None:
        pm, *_ = _make_pressure_manager(workers=[])
        pm._suspend_on_high_pressure()  # should not raise
        assert len(pm._suspended_for_pressure) == 0


class TestCriticalPressure:
    """Verify _suspend_on_critical_pressure suspends all except most recent."""

    def test_suspends_all_except_most_recently_active(self) -> None:
        s1 = _make_sleeping_worker("old")
        s1.state_since = 100.0
        s2 = _make_sleeping_worker("recent")
        s2.state_since = 200.0  # more recent
        workers = [s1, s2, make_worker("buzzing")]
        pm, _, suspended, *_ = _make_pressure_manager(workers)

        pm._suspend_on_critical_pressure()

        # "recent" has highest state_since among candidates, so it's kept
        assert "old" in pm._suspended_for_pressure
        assert "recent" not in pm._suspended_for_pressure

    def test_includes_resting_workers(self) -> None:
        resting = _make_resting_worker("restful")
        resting.state_since = 100.0
        sleeping = _make_sleeping_worker("sleepy")
        sleeping.state_since = 200.0
        workers = [resting, sleeping]
        pm, *_ = _make_pressure_manager(workers)

        pm._suspend_on_critical_pressure()

        # sleeping has higher state_since → kept; resting suspended
        assert "restful" in pm._suspended_for_pressure
        assert "sleepy" not in pm._suspended_for_pressure

    def test_no_candidates_no_action(self) -> None:
        workers = [make_worker("b1"), make_worker("b2")]  # all BUZZING
        pm, _, _, _, events = _make_pressure_manager(workers)

        pm._suspend_on_critical_pressure()

        assert len(pm._suspended_for_pressure) == 0
        assert "workers_changed" not in events

    def test_single_candidate_not_suspended(self) -> None:
        workers = [_make_sleeping_worker("only"), make_worker("busy")]
        pm, *_ = _make_pressure_manager(workers)

        pm._suspend_on_critical_pressure()

        # Single candidate is the most recent — not suspended
        assert len(pm._suspended_for_pressure) == 0

    def test_emits_workers_changed_on_suspension(self) -> None:
        s1 = _make_sleeping_worker("a")
        s1.state_since = 100.0
        s2 = _make_sleeping_worker("b")
        s2.state_since = 200.0
        pm, _, _, _, events = _make_pressure_manager([s1, s2])

        pm._suspend_on_critical_pressure()

        assert "workers_changed" in events


class TestResumePressureSuspended:
    """Verify _resume_pressure_suspended clears pressure suspension."""

    def test_resumes_all_pressure_suspended(self) -> None:
        workers = [make_worker("a"), make_worker("b")]
        pm, log, suspended, suspended_at, events = _make_pressure_manager(workers)

        # Suspend first
        pm._suspend_workers(["a", "b"], "HIGH")
        assert len(suspended) == 2

        # Resume
        pm._resume_pressure_suspended()

        assert len(pm._suspended_for_pressure) == 0
        assert "a" not in suspended
        assert "b" not in suspended
        assert "a" not in suspended_at
        assert "b" not in suspended_at
        assert "workers_changed" in events

    def test_resume_logs_entries(self) -> None:
        workers = [make_worker("a")]
        pm, log, *_ = _make_pressure_manager(workers)
        pm._suspend_workers(["a"], "HIGH")

        pm._resume_pressure_suspended()

        resumed = [e for e in log.entries if e.action == SystemAction.RESUMED]
        assert len(resumed) == 1
        assert resumed[0].worker_name == "a"

    def test_resume_noop_when_none_suspended(self) -> None:
        pm, _, _, _, events = _make_pressure_manager()
        pm._resume_pressure_suspended()
        assert "workers_changed" not in events

    def test_resume_routes_through_wake_worker_callback(self) -> None:
        """Task #233: pressure RESUME must invoke ``wake_worker`` so the
        state tracker's content-fingerprint cache is cleared. Without
        this hook, a worker whose PTY state changed during the suspension
        (e.g. idle → actively running a Bash tool) kept its stale
        fingerprint, hit the RESTING short-circuit in state_tracker, and
        never re-classified as BUZZING — that's the dashboard "RESTING
        while demonstrably mid-turn" bug from the operator report.
        """
        workers = [make_worker("a"), make_worker("b")]
        pm, _, suspended, suspended_at, _ = _make_pressure_manager(workers)

        waked: list[str] = []

        def fake_wake(name: str) -> bool:
            waked.append(name)
            suspended.discard(name)
            suspended_at.pop(name, None)
            return True

        pm._wake_worker = fake_wake
        pm._suspend_workers(["a", "b"], "HIGH")

        pm._resume_pressure_suspended()

        # Both workers waked via the callback (not via direct
        # suspended-set discard, which would bypass fingerprint clear).
        assert sorted(waked) == ["a", "b"]
        # And the legacy state is tidy too — the shared ``suspended``
        # set still gets emptied (through the callback's discard path).
        assert suspended == set()
        assert suspended_at == {}
        assert pm._suspended_for_pressure == set()

    def test_resume_falls_back_to_direct_discard_when_no_callback(self) -> None:
        """Without a wire-up (legacy / test init), resume still clears the
        shared suspended set so we never leave workers stuck post-resume."""
        workers = [make_worker("a")]
        pm, _, suspended, suspended_at, _ = _make_pressure_manager(workers)
        # No _wake_worker callback set — default constructor path.
        assert pm._wake_worker is None
        pm._suspend_workers(["a"], "HIGH")

        pm._resume_pressure_suspended()

        assert "a" not in suspended
        assert "a" not in suspended_at


class TestOnPressureChanged:
    """Integration: verify on_pressure_changed routes to correct handler."""

    def test_nominal_resumes_workers(self) -> None:
        workers = [make_worker("a")]
        pm, _, suspended, *_ = _make_pressure_manager(workers)
        pm._suspend_workers(["a"], "test")

        pm.on_pressure_changed(MemoryPressureLevel.NOMINAL)

        assert len(pm._suspended_for_pressure) == 0
        assert "a" not in suspended

    def test_elevated_resumes_workers(self) -> None:
        workers = [make_worker("a")]
        pm, _, suspended, *_ = _make_pressure_manager(workers)
        pm._suspend_workers(["a"], "test")

        pm.on_pressure_changed(MemoryPressureLevel.ELEVATED)

        assert len(pm._suspended_for_pressure) == 0

    def test_high_suspends_sleeping_workers(self) -> None:
        sleeping = [_make_sleeping_worker(f"s{i}") for i in range(4)]
        buzzing = [make_worker("b1")]
        workers = sleeping + buzzing
        pm, *_ = _make_pressure_manager(workers)

        pm.on_pressure_changed(MemoryPressureLevel.HIGH)

        # 5 total, ceil(5*0.6)=3 target → 2 suspended
        assert len(pm._suspended_for_pressure) == 2

    def test_critical_suspends_all_but_one(self) -> None:
        s1 = _make_sleeping_worker("a")
        s1.state_since = 100.0
        s2 = _make_sleeping_worker("b")
        s2.state_since = 200.0
        s3 = _make_sleeping_worker("c")
        s3.state_since = 300.0
        pm, *_ = _make_pressure_manager([s1, s2, s3])

        pm.on_pressure_changed(MemoryPressureLevel.CRITICAL)

        # Keep the most recent (c), suspend a and b
        assert len(pm._suspended_for_pressure) == 2
        assert "c" not in pm._suspended_for_pressure

    def test_transition_high_to_nominal_resumes(self) -> None:
        sleeping = [_make_sleeping_worker(f"s{i}") for i in range(3)]
        buzzing = [make_worker("b1"), make_worker("b2")]
        workers = sleeping + buzzing
        pm, _, suspended, *_ = _make_pressure_manager(workers)

        pm.on_pressure_changed(MemoryPressureLevel.HIGH)
        assert len(pm._suspended_for_pressure) > 0

        pm.on_pressure_changed(MemoryPressureLevel.NOMINAL)
        assert len(pm._suspended_for_pressure) == 0
        # All workers should be unsuspended
        for w in workers:
            assert w.name not in suspended

    def test_transition_critical_to_elevated_resumes(self) -> None:
        s1 = _make_sleeping_worker("a")
        s1.state_since = 100.0
        s2 = _make_sleeping_worker("b")
        s2.state_since = 200.0
        pm, _, suspended, *_ = _make_pressure_manager([s1, s2])

        pm.on_pressure_changed(MemoryPressureLevel.CRITICAL)
        assert len(pm._suspended_for_pressure) == 1

        pm.on_pressure_changed(MemoryPressureLevel.ELEVATED)
        assert len(pm._suspended_for_pressure) == 0
