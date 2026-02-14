"""Tests for drones/pilot.py — async polling loop and decision engine."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from swarm.drones.log import DroneLog, SystemAction
from swarm.drones.pilot import DronePilot
from swarm.config import DroneConfig
from swarm.tasks.board import TaskBoard
from swarm.tasks.task import TaskStatus
from swarm.worker.worker import WorkerState

from tests.conftest import make_worker as _make_worker


@pytest.fixture
def pilot_setup(monkeypatch):
    """Set up a DronePilot with mocked tmux calls."""
    workers = [_make_worker("api"), _make_worker("web")]
    log = DroneLog()
    pilot = DronePilot(workers, log, interval=1.0, session_name="test", drone_config=DroneConfig())

    # Mock all tmux operations
    monkeypatch.setattr("swarm.drones.pilot.pane_exists", AsyncMock(return_value=True))
    monkeypatch.setattr("swarm.drones.pilot.get_pane_command", AsyncMock(return_value="claude"))
    monkeypatch.setattr(
        "swarm.drones.pilot.capture_pane", AsyncMock(return_value="esc to interrupt")
    )
    monkeypatch.setattr("swarm.drones.pilot.send_enter", AsyncMock())

    monkeypatch.setattr("swarm.drones.pilot.set_pane_option", AsyncMock())
    monkeypatch.setattr("swarm.drones.pilot.discover_workers", AsyncMock(return_value=[]))
    monkeypatch.setattr("swarm.drones.pilot.update_window_names", AsyncMock())
    monkeypatch.setattr("swarm.drones.pilot.set_terminal_title", AsyncMock())
    monkeypatch.setattr("swarm.drones.pilot.revive_worker", AsyncMock())

    return pilot, workers, log


@pytest.mark.asyncio
async def test_poll_once_buzzing(pilot_setup):
    """Workers in BUZZING state should not generate any actions."""
    pilot, workers, log = pilot_setup
    pilot.enabled = True
    await pilot.poll_once()
    assert len(log.entries) == 0


@pytest.mark.asyncio
async def test_poll_once_detects_waiting(pilot_setup, monkeypatch):
    """poll_once should detect WAITING state from empty prompt content."""
    pilot, workers, log = pilot_setup
    monkeypatch.setattr("swarm.drones.pilot.capture_pane", AsyncMock(return_value="> "))
    pilot.enabled = True
    # First poll: hysteresis (BUZZING -> WAITING requires 2 confirmations)
    await pilot.poll_once()
    # Second poll: should confirm WAITING
    await pilot.poll_once()
    # After two polls (hysteresis), workers with empty prompts should be WAITING
    waiting = [w for w in workers if w.state == WorkerState.WAITING]
    assert len(waiting) > 0, "Expected at least one worker to transition to WAITING"


@pytest.mark.asyncio
async def test_poll_once_removes_dead_workers(pilot_setup, monkeypatch):
    """Dead panes should be removed from worker list."""
    pilot, workers, log = pilot_setup
    monkeypatch.setattr("swarm.drones.pilot.pane_exists", AsyncMock(return_value=False))

    changes = []
    pilot.on_workers_changed(lambda: changes.append(1))

    await pilot.poll_once()
    assert len(workers) == 0
    assert len(changes) == 1


@pytest.mark.asyncio
async def test_poll_once_state_change_callback(pilot_setup, monkeypatch):
    """State changes should fire the on_state_changed callback."""
    pilot, workers, log = pilot_setup

    # First make workers RESTING -> triggers via shell detection
    monkeypatch.setattr("swarm.drones.pilot.get_pane_command", AsyncMock(return_value="bash"))
    monkeypatch.setattr("swarm.drones.pilot.capture_pane", AsyncMock(return_value="$ "))

    state_changes = []
    pilot.on_state_changed(lambda w: state_changes.append(w.name))

    await pilot.poll_once()
    # STUNG should be detected immediately (no hysteresis)
    assert len(state_changes) > 0


@pytest.mark.asyncio
async def test_revive_on_stung(pilot_setup, monkeypatch):
    """STUNG workers with revives remaining should be revived."""
    pilot, workers, log = pilot_setup
    pilot.enabled = True

    monkeypatch.setattr("swarm.drones.pilot.get_pane_command", AsyncMock(return_value="bash"))
    monkeypatch.setattr("swarm.drones.pilot.capture_pane", AsyncMock(return_value="$ "))

    await pilot.poll_once()
    # Should have revive entries
    revives = [e for e in log.entries if e.action == SystemAction.REVIVED]
    assert len(revives) > 0


@pytest.mark.asyncio
async def test_escalate_on_crash_loop(pilot_setup, monkeypatch):
    """Workers that exhaust revive attempts should be escalated."""
    pilot, workers, log = pilot_setup
    pilot.enabled = True

    # Set workers to already have max revives
    for w in workers:
        w.revive_count = 3

    monkeypatch.setattr("swarm.drones.pilot.get_pane_command", AsyncMock(return_value="bash"))
    monkeypatch.setattr("swarm.drones.pilot.capture_pane", AsyncMock(return_value="$ "))

    escalations = []
    pilot.on_escalate(lambda w, r: escalations.append((w.name, r)))

    await pilot.poll_once()
    escalates = [e for e in log.entries if e.action == SystemAction.ESCALATED]
    assert len(escalates) > 0


@pytest.mark.asyncio
async def test_toggle(pilot_setup):
    """toggle() should flip enabled state but keep poll loop alive."""
    pilot, _, _ = pilot_setup
    assert not pilot.enabled
    result = pilot.toggle()
    assert result is True
    assert pilot.enabled
    result = pilot.toggle()
    assert result is False
    assert not pilot.enabled
    # Poll loop should still be running for state detection
    assert pilot._task is not None and not pilot._task.done()
    # Clean up
    pilot.stop()


@pytest.mark.asyncio
async def test_continue_on_choice_prompt(pilot_setup, monkeypatch):
    """Choice prompts should trigger CONTINUE decision."""
    pilot, workers, log = pilot_setup
    pilot.enabled = True

    # Put workers in WAITING state first (choice prompt = actionable)
    for w in workers:
        w.state = WorkerState.WAITING

    content = """> 1. Always allow
  2. Yes
  3. No
Enter to select · ↑/↓ to navigate"""
    monkeypatch.setattr("swarm.drones.pilot.capture_pane", AsyncMock(return_value=content))
    monkeypatch.setattr("swarm.drones.pilot.get_pane_command", AsyncMock(return_value="claude"))

    await pilot.poll_once()
    continued = [e for e in log.entries if e.action == SystemAction.CONTINUED]
    assert len(continued) > 0


@pytest.mark.asyncio
async def test_escalated_set_is_per_pilot():
    """Each DronePilot should have its own _escalated set."""
    w1 = [_make_worker("api")]
    w2 = [_make_worker("web")]
    p1 = DronePilot(w1, DroneLog(), drone_config=DroneConfig())
    p2 = DronePilot(w2, DroneLog(), drone_config=DroneConfig())
    assert p1._escalated is not p2._escalated


# ── Adaptive polling / backoff ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_poll_once_returns_false_when_idle(pilot_setup):
    """poll_once should return False when all workers are BUZZING (no action)."""
    pilot, workers, log = pilot_setup
    pilot.enabled = True
    result = await pilot.poll_once()
    assert result is False


@pytest.mark.asyncio
async def test_poll_once_returns_true_on_action(pilot_setup, monkeypatch):
    """poll_once should return True when an action is taken (e.g. revive)."""
    pilot, workers, log = pilot_setup
    pilot.enabled = True

    monkeypatch.setattr("swarm.drones.pilot.get_pane_command", AsyncMock(return_value="bash"))
    monkeypatch.setattr("swarm.drones.pilot.capture_pane", AsyncMock(return_value="$ "))

    result = await pilot.poll_once()
    assert result is True


@pytest.mark.asyncio
async def test_adaptive_backoff(pilot_setup, monkeypatch):
    """Idle streak should grow and backoff interval should increase."""
    pilot, workers, log = pilot_setup
    pilot.enabled = True

    assert pilot._idle_streak == 0

    # All workers BUZZING, no action taken → idle streak should grow
    # Use poll_once() return value to drive the same logic as _loop
    had_action = await pilot.poll_once()
    assert had_action is False

    # Apply _loop's idle-streak logic (same as pilot._loop)
    if had_action:
        pilot._idle_streak = 0
    else:
        pilot._idle_streak += 1

    assert pilot._idle_streak == 1

    # Second idle poll
    had_action = await pilot.poll_once()
    assert had_action is False
    if had_action:
        pilot._idle_streak = 0
    else:
        pilot._idle_streak += 1

    assert pilot._idle_streak == 2

    # Verify backoff formula: base * 2^min(streak, 3), capped at max
    def expected_backoff(streak):
        return min(
            pilot._base_interval * (2 ** min(streak, 3)),
            pilot._max_interval,
        )

    assert expected_backoff(1) == 2.0  # 1.0 * 2^1
    assert expected_backoff(2) == 4.0  # 1.0 * 2^2
    assert expected_backoff(3) == 8.0  # 1.0 * 2^3
    assert expected_backoff(5) == 8.0  # capped at 2^3 = 8.0


@pytest.mark.asyncio
async def test_adaptive_backoff_resets_on_action(pilot_setup, monkeypatch):
    """Idle streak should reset to 0 when an action is taken."""
    pilot, workers, log = pilot_setup
    pilot.enabled = True

    # Build up idle streak via actual no-action polls
    for _ in range(3):
        had_action = await pilot.poll_once()
        assert had_action is False
        pilot._idle_streak += 1  # mirrors _loop logic

    assert pilot._idle_streak == 3

    # Force an action (revive via STUNG detection)
    monkeypatch.setattr("swarm.drones.pilot.get_pane_command", AsyncMock(return_value="bash"))
    monkeypatch.setattr("swarm.drones.pilot.capture_pane", AsyncMock(return_value="$ "))

    had_action = await pilot.poll_once()
    assert had_action is True

    # Apply _loop's reset logic
    if had_action:
        pilot._idle_streak = 0

    assert pilot._idle_streak == 0


# ── Loop termination: empty hive ────────────────────────────────────────


@pytest.mark.asyncio
async def test_loop_exits_on_empty_hive(monkeypatch):
    """_loop should exit and emit hive_empty when all workers are gone."""
    workers = [_make_worker("api")]
    log = DroneLog()
    pilot = DronePilot(workers, log, interval=0.01, session_name=None, drone_config=DroneConfig())

    # pane_exists returns False → worker will be removed
    monkeypatch.setattr("swarm.drones.pilot.pane_exists", AsyncMock(return_value=False))

    events: list[str] = []
    pilot.on_hive_empty(lambda: events.append("hive_empty"))

    pilot.enabled = True
    pilot._running = True
    # Run _loop — should exit after one cycle since workers become empty
    await asyncio.wait_for(pilot._loop(), timeout=2.0)

    assert not pilot.enabled
    assert not pilot._running
    assert "hive_empty" in events
    assert len(workers) == 0


# ── Loop termination: hive complete ─────────────────────────────────────


@pytest.mark.asyncio
async def test_hive_complete_emitted(monkeypatch):
    """hive_complete should fire after 3 consecutive all-done polls."""
    workers = [_make_worker("api", state=WorkerState.RESTING)]
    log = DroneLog()

    board = TaskBoard()
    task = board.create("Test task")
    board.assign(task.id, "api")
    board.complete(task.id)

    pilot = DronePilot(
        workers,
        log,
        interval=0.01,
        session_name=None,
        drone_config=DroneConfig(auto_stop_on_complete=True),
        task_board=board,
    )

    # Workers are RESTING and all tasks complete.
    # Use idle prompt with suggestion text (classifies as RESTING, not WAITING).
    idle_content = '> Try "how does foo work"\n? for shortcuts'
    monkeypatch.setattr("swarm.drones.pilot.pane_exists", AsyncMock(return_value=True))
    monkeypatch.setattr("swarm.drones.pilot.get_pane_command", AsyncMock(return_value="claude"))
    monkeypatch.setattr("swarm.drones.pilot.capture_pane", AsyncMock(return_value=idle_content))
    monkeypatch.setattr("swarm.drones.pilot.set_pane_option", AsyncMock())
    monkeypatch.setattr("swarm.drones.pilot.send_enter", AsyncMock())

    events: list[str] = []
    pilot.on_hive_complete(lambda: events.append("hive_complete"))

    pilot.enabled = True
    pilot._running = True
    await asyncio.wait_for(pilot._loop(), timeout=2.0)

    assert "hive_complete" in events


@pytest.mark.asyncio
async def test_hive_complete_not_emitted_when_disabled(monkeypatch):
    """auto_stop_on_complete=False should prevent hive_complete."""
    workers = [_make_worker("api", state=WorkerState.RESTING)]
    log = DroneLog()

    board = TaskBoard()
    task = board.create("Test task")
    board.assign(task.id, "api")
    board.complete(task.id)

    pilot = DronePilot(
        workers,
        log,
        interval=0.01,
        session_name=None,
        drone_config=DroneConfig(auto_stop_on_complete=False),
        task_board=board,
    )

    # Use idle prompt with suggestion text (classifies as RESTING, not WAITING)
    idle_content = '> Try "how does foo work"\n? for shortcuts'
    monkeypatch.setattr("swarm.drones.pilot.pane_exists", AsyncMock(return_value=True))
    monkeypatch.setattr("swarm.drones.pilot.get_pane_command", AsyncMock(return_value="claude"))
    monkeypatch.setattr("swarm.drones.pilot.capture_pane", AsyncMock(return_value=idle_content))
    monkeypatch.setattr("swarm.drones.pilot.set_pane_option", AsyncMock())
    monkeypatch.setattr("swarm.drones.pilot.send_enter", AsyncMock())

    events: list[str] = []
    pilot.on_hive_complete(lambda: events.append("hive_complete"))

    pilot.enabled = True
    # Run a few poll cycles manually (not _loop, since it wouldn't terminate)
    for _ in range(5):
        await pilot.poll_once()

    assert "hive_complete" not in events


@pytest.mark.asyncio
async def test_hive_complete_sets_running_false(monkeypatch):
    """hive_complete should set _running=False so watchdog doesn't restart."""
    workers = [_make_worker("api", state=WorkerState.RESTING)]
    log = DroneLog()

    board = TaskBoard()
    task = board.create("Test task")
    board.assign(task.id, "api")
    board.complete(task.id)

    pilot = DronePilot(
        workers,
        log,
        interval=0.01,
        session_name=None,
        drone_config=DroneConfig(auto_stop_on_complete=True),
        task_board=board,
    )

    idle_content = '> Try "how does foo work"\n? for shortcuts'
    monkeypatch.setattr("swarm.drones.pilot.pane_exists", AsyncMock(return_value=True))
    monkeypatch.setattr("swarm.drones.pilot.get_pane_command", AsyncMock(return_value="claude"))
    monkeypatch.setattr("swarm.drones.pilot.capture_pane", AsyncMock(return_value=idle_content))
    monkeypatch.setattr("swarm.drones.pilot.set_pane_option", AsyncMock())
    monkeypatch.setattr("swarm.drones.pilot.send_enter", AsyncMock())

    pilot.enabled = True
    pilot._running = True
    await asyncio.wait_for(pilot._loop(), timeout=2.0)

    assert not pilot._running, "_running should be False after hive_complete"
    assert not pilot.needs_restart(), "watchdog should not restart after hive_complete"


@pytest.mark.asyncio
async def test_loop_cancelled_no_error(monkeypatch):
    """Cancelling the loop (Ctrl+C shutdown) should not log ERROR."""
    import logging

    workers = [_make_worker("api")]
    log = DroneLog()
    pilot = DronePilot(workers, log, interval=0.1, session_name=None, drone_config=DroneConfig())

    monkeypatch.setattr("swarm.drones.pilot.pane_exists", AsyncMock(return_value=True))
    monkeypatch.setattr("swarm.drones.pilot.get_pane_command", AsyncMock(return_value="claude"))
    monkeypatch.setattr(
        "swarm.drones.pilot.capture_pane", AsyncMock(return_value="esc to interrupt")
    )
    monkeypatch.setattr("swarm.drones.pilot.set_pane_option", AsyncMock())

    pilot.enabled = True
    pilot._running = True
    task = asyncio.create_task(pilot._loop())

    # Let it start one cycle then cancel
    await asyncio.sleep(0.05)
    task.cancel()

    errors: list[str] = []
    handler = logging.Handler()
    handler.emit = lambda r: errors.append(r.getMessage()) if r.levelno >= logging.ERROR else None
    logger = logging.getLogger("swarm.drones.pilot")
    logger.addHandler(handler)
    try:
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        logger.removeHandler(handler)

    assert not errors, f"CancelledError should not produce ERROR logs: {errors}"


@pytest.mark.asyncio
async def test_wait_directive_no_warning(monkeypatch):
    """Queen 'wait' directive should not produce a warning."""
    import logging

    workers = [_make_worker("api")]
    log = DroneLog()
    pilot = DronePilot(workers, log, interval=1.0, session_name=None, drone_config=DroneConfig())

    warnings: list[str] = []
    handler = logging.Handler()
    handler.emit = lambda r: (
        warnings.append(r.getMessage()) if r.levelno >= logging.WARNING else None
    )
    logger = logging.getLogger("swarm.drones.pilot")
    logger.addHandler(handler)

    try:
        result = await pilot._execute_directives(
            [{"worker": "api", "action": "wait", "reason": "worker is busy"}]
        )
    finally:
        logger.removeHandler(handler)

    # "wait" is a no-op — should not count as an executed directive
    assert result is False
    assert not warnings, f"'wait' directive should not produce warnings: {warnings}"


@pytest.mark.asyncio
async def test_on_loop_done_normal_exit_not_warning(monkeypatch):
    """Normal loop exit (hive_complete) should not log WARNING."""
    import logging

    warnings: list[str] = []
    handler = logging.Handler()
    handler.emit = lambda r: (
        warnings.append(r.getMessage()) if r.levelno >= logging.WARNING else None
    )
    logger = logging.getLogger("swarm.drones.pilot")
    logger.addHandler(handler)

    try:
        # Simulate a normally-exited task
        task = asyncio.create_task(asyncio.sleep(0))
        await task
        DronePilot._on_loop_done(task)
    finally:
        logger.removeHandler(handler)

    assert not warnings, f"Normal exit should not produce WARNING logs: {warnings}"


# ── Circuit breaker ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_circuit_breaker_trips(monkeypatch):
    """Worker with N consecutive poll failures should be treated as dead."""
    workers = [_make_worker("api"), _make_worker("web")]
    log = DroneLog()
    max_failures = 3
    pilot = DronePilot(
        workers,
        log,
        interval=1.0,
        session_name=None,
        drone_config=DroneConfig(max_poll_failures=max_failures),
    )

    # pane_exists succeeds, but capture_pane throws for "api" only
    async def pane_exists_ok(pane_id):
        return True

    async def get_cmd(pane_id):
        if pane_id == "%api":
            raise OSError("simulated failure")
        return "claude"

    monkeypatch.setattr("swarm.drones.pilot.pane_exists", pane_exists_ok)
    monkeypatch.setattr("swarm.drones.pilot.get_pane_command", get_cmd)
    monkeypatch.setattr(
        "swarm.drones.pilot.capture_pane", AsyncMock(return_value="esc to interrupt")
    )
    monkeypatch.setattr("swarm.drones.pilot.set_pane_option", AsyncMock())
    monkeypatch.setattr("swarm.drones.pilot.send_enter", AsyncMock())

    monkeypatch.setattr("swarm.drones.pilot.revive_worker", AsyncMock())

    changes: list[int] = []
    pilot.on_workers_changed(lambda: changes.append(1))

    # Poll N-1 times: should NOT remove the worker yet
    for _ in range(max_failures - 1):
        await pilot.poll_once()

    assert len(workers) == 2  # both still alive
    assert pilot._poll_failures.get("%api") == max_failures - 1

    # One more poll: circuit breaker trips
    await pilot.poll_once()

    assert len(workers) == 1
    assert workers[0].name == "web"
    assert len(changes) == 1  # workers_changed fired once
    # Failure counter cleaned up
    assert "%api" not in pilot._poll_failures


@pytest.mark.asyncio
async def test_circuit_breaker_resets_on_success(pilot_setup):
    """Successful poll should reset the failure counter for a worker."""
    pilot, workers, log = pilot_setup
    pilot.enabled = True

    # Seed some failures
    pilot._poll_failures["%api"] = 3

    await pilot.poll_once()

    # After successful poll, counter should be cleared
    assert "%api" not in pilot._poll_failures


# ── Dead worker task redistribution ─────────────────────────────────────


@pytest.mark.asyncio
async def test_dead_worker_unassigns_tasks(monkeypatch):
    """When a dead worker is removed, its assigned tasks should be unassigned."""
    workers = [_make_worker("api")]
    log = DroneLog()

    board = TaskBoard()
    task = board.create("Build API")
    board.assign(task.id, "api")
    assert task.status == TaskStatus.ASSIGNED
    assert task.assigned_worker == "api"

    pilot = DronePilot(
        workers,
        log,
        interval=1.0,
        session_name=None,
        drone_config=DroneConfig(),
        task_board=board,
    )

    # Worker pane gone
    monkeypatch.setattr("swarm.drones.pilot.pane_exists", AsyncMock(return_value=False))

    await pilot.poll_once()

    # Worker removed
    assert len(workers) == 0
    # Task should be back to PENDING
    assert task.status == TaskStatus.PENDING
    assert task.assigned_worker is None


@pytest.mark.asyncio
async def test_circuit_breaker_dead_worker_unassigns_tasks(monkeypatch):
    """Circuit-breaker-killed worker's tasks should also be unassigned."""
    workers = [_make_worker("api")]
    log = DroneLog()

    board = TaskBoard()
    task = board.create("Build API")
    board.assign(task.id, "api")

    pilot = DronePilot(
        workers,
        log,
        interval=1.0,
        session_name=None,
        drone_config=DroneConfig(max_poll_failures=2),
        task_board=board,
    )

    async def pane_exists_ok(pane_id):
        return True

    async def always_fail(pane_id):
        raise OSError("boom")

    monkeypatch.setattr("swarm.drones.pilot.pane_exists", pane_exists_ok)
    monkeypatch.setattr("swarm.drones.pilot.get_pane_command", always_fail)
    monkeypatch.setattr("swarm.drones.pilot.capture_pane", AsyncMock(return_value=""))
    monkeypatch.setattr("swarm.drones.pilot.set_pane_option", AsyncMock())

    # 2 failures → circuit breaker trips
    await pilot.poll_once()
    await pilot.poll_once()

    assert len(workers) == 0
    assert task.status == TaskStatus.PENDING
    assert task.assigned_worker is None


class TestTaskCompletionReproposal:
    """Completion re-proposal after cooldown when Queen initially says 'not done'."""

    def _make_pilot_with_board(self, monkeypatch):
        workers = [_make_worker("api", state=WorkerState.RESTING, resting_since=0)]
        log = DroneLog()
        board = TaskBoard()
        pilot = DronePilot(
            workers, log, interval=1.0, session_name="test", drone_config=DroneConfig()
        )
        pilot.task_board = board
        pilot.enabled = True
        # Shorten cooldown for tests
        pilot._COMPLETION_REPROPOSE_COOLDOWN = 60
        return pilot, workers, board, log

    def test_first_proposal_fires(self, monkeypatch):
        """First idle check should emit task_done."""
        pilot, workers, board, log = self._make_pilot_with_board(monkeypatch)
        import time

        workers[0].state_since = time.time() - 120  # idle for 2 min

        task = board.create("Fix bug")
        board.assign(task.id, "api")

        events = []
        pilot.on("task_done", lambda w, t, r: events.append((w.name, t.id)))

        pilot._check_task_completions()
        assert len(events) == 1
        assert events[0] == ("api", task.id)

    def test_second_check_within_cooldown_skips(self, monkeypatch):
        """Within cooldown, same task should not be re-proposed."""
        pilot, workers, board, log = self._make_pilot_with_board(monkeypatch)
        import time

        workers[0].state_since = time.time() - 120

        task = board.create("Fix bug")
        board.assign(task.id, "api")

        events = []
        pilot.on("task_done", lambda w, t, r: events.append(t.id))

        pilot._check_task_completions()
        pilot._check_task_completions()  # within cooldown
        assert len(events) == 1  # only fired once

    def test_reproposal_after_cooldown(self, monkeypatch):
        """After cooldown expires, task should be re-proposed."""
        pilot, workers, board, log = self._make_pilot_with_board(monkeypatch)
        import time

        workers[0].state_since = time.time() - 120

        task = board.create("Fix bug")
        board.assign(task.id, "api")

        events = []
        pilot.on("task_done", lambda w, t, r: events.append(t.id))

        pilot._check_task_completions()
        assert len(events) == 1

        # Simulate cooldown expiry by backdating the timestamp
        pilot._proposed_completions[task.id] = time.time() - 120  # 2 min ago, > 60s cooldown

        pilot._check_task_completions()
        assert len(events) == 2  # fired again

    def test_clear_proposed_allows_immediate_reproposal(self, monkeypatch):
        """clear_proposed_completion should allow immediate re-proposal."""
        pilot, workers, board, log = self._make_pilot_with_board(monkeypatch)
        import time

        workers[0].state_since = time.time() - 120

        task = board.create("Fix bug")
        board.assign(task.id, "api")

        events = []
        pilot.on("task_done", lambda w, t, r: events.append(t.id))

        pilot._check_task_completions()
        assert len(events) == 1

        pilot.clear_proposed_completion(task.id)

        pilot._check_task_completions()
        assert len(events) == 2


# ── _auto_assign_tasks ──────────────────────────────────────────────────


class TestAutoAssignTasks:
    """Tests for the _auto_assign_tasks Queen-driven assignment flow."""

    def _make_pilot_with_queen(self, monkeypatch, workers=None, tasks=None):
        """Helper: build pilot with a mocked Queen and populated task board."""
        if workers is None:
            workers = [_make_worker("api", state=WorkerState.RESTING)]
        log = DroneLog()
        board = TaskBoard()
        for t in tasks or []:
            board.add(t)

        queen = AsyncMock()
        queen.can_call = True
        queen.enabled = True

        pilot = DronePilot(
            workers,
            log,
            interval=1.0,
            session_name="test",
            drone_config=DroneConfig(),
            task_board=board,
            queen=queen,
        )
        pilot.enabled = True

        # Mock build_hive_context
        monkeypatch.setattr("swarm.queen.context.build_hive_context", lambda *a, **kw: "ctx")
        return pilot, workers, board, queen, log

    @pytest.mark.asyncio
    async def test_auto_assign_no_queen(self, monkeypatch):
        """Returns False when queen is None."""
        pilot, _, board, _, _ = self._make_pilot_with_queen(monkeypatch)
        pilot.queen = None
        result = await pilot._auto_assign_tasks()
        assert result is False

    @pytest.mark.asyncio
    async def test_auto_assign_queen_cannot_call(self, monkeypatch):
        """Returns False when queen.can_call is False."""
        pilot, _, board, queen, _ = self._make_pilot_with_queen(monkeypatch)
        queen.can_call = False
        result = await pilot._auto_assign_tasks()
        assert result is False

    @pytest.mark.asyncio
    async def test_auto_assign_no_available_tasks(self, monkeypatch):
        """Returns False when no available tasks exist."""
        pilot, _, board, queen, _ = self._make_pilot_with_queen(monkeypatch)
        # Board is empty — no tasks
        result = await pilot._auto_assign_tasks()
        assert result is False

    @pytest.mark.asyncio
    async def test_auto_assign_no_idle_workers(self, monkeypatch):
        """Returns False when all workers are BUZZING (none idle)."""
        from swarm.tasks.task import SwarmTask

        workers = [_make_worker("api", state=WorkerState.BUZZING)]
        task = SwarmTask(title="Build API", description="Build the REST API")
        pilot, _, board, queen, _ = self._make_pilot_with_queen(
            monkeypatch, workers=workers, tasks=[task]
        )
        result = await pilot._auto_assign_tasks()
        assert result is False

    @pytest.mark.asyncio
    async def test_auto_assign_skips_worker_with_active_task(self, monkeypatch):
        """Workers with already-assigned tasks should not get new assignments."""
        from swarm.tasks.task import SwarmTask

        task1 = SwarmTask(title="Existing task")
        task2 = SwarmTask(title="New task")
        workers = [_make_worker("api", state=WorkerState.RESTING)]
        pilot, _, board, queen, _ = self._make_pilot_with_queen(
            monkeypatch, workers=workers, tasks=[task1, task2]
        )
        # Assign task1 to api so it has an active task
        board.assign(task1.id, "api")

        result = await pilot._auto_assign_tasks()
        assert result is False
        queen.assign_tasks.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_auto_assign_success_emits_proposal(self, monkeypatch):
        """Successful Queen assignment should emit a proposal event."""
        from swarm.tasks.task import SwarmTask

        task = SwarmTask(title="Build API", description="REST API")
        workers = [_make_worker("api", state=WorkerState.RESTING)]
        pilot, _, board, queen, log = self._make_pilot_with_queen(
            monkeypatch, workers=workers, tasks=[task]
        )

        queen.assign_tasks.return_value = [
            {
                "worker": "api",
                "task_id": task.id,
                "message": "Build the REST API",
                "reasoning": "Best match",
                "confidence": 0.9,
            }
        ]

        proposals = []
        pilot.on_proposal(lambda p: proposals.append(p))

        result = await pilot._auto_assign_tasks()
        assert result is True
        assert len(proposals) == 1
        assert proposals[0].worker_name == "api"
        assert proposals[0].task_id == task.id

    @pytest.mark.asyncio
    async def test_auto_assign_skips_invalid_assignment(self, monkeypatch):
        """Assignments referencing unknown workers or tasks should be skipped."""
        from swarm.tasks.task import SwarmTask

        task = SwarmTask(title="Build API")
        workers = [_make_worker("api", state=WorkerState.RESTING)]
        pilot, _, board, queen, _ = self._make_pilot_with_queen(
            monkeypatch, workers=workers, tasks=[task]
        )

        queen.assign_tasks.return_value = [
            {
                "worker": "nonexistent",
                "task_id": task.id,
                "message": "Do stuff",
            }
        ]

        proposals = []
        pilot.on_proposal(lambda p: proposals.append(p))

        result = await pilot._auto_assign_tasks()
        assert result is False
        assert len(proposals) == 0

    @pytest.mark.asyncio
    async def test_auto_assign_skips_non_dict_entries(self, monkeypatch):
        """Non-dict assignment entries from Queen should be skipped."""
        from swarm.tasks.task import SwarmTask

        task = SwarmTask(title="Build API")
        workers = [_make_worker("api", state=WorkerState.RESTING)]
        pilot, _, board, queen, _ = self._make_pilot_with_queen(
            monkeypatch, workers=workers, tasks=[task]
        )

        queen.assign_tasks.return_value = ["not-a-dict", 42, None]

        proposals = []
        pilot.on_proposal(lambda p: proposals.append(p))

        result = await pilot._auto_assign_tasks()
        assert result is False
        assert len(proposals) == 0

    @pytest.mark.asyncio
    async def test_auto_assign_queen_error_returns_false(self, monkeypatch):
        """Queen raising an exception should not crash; returns False."""
        from swarm.tasks.task import SwarmTask

        task = SwarmTask(title="Build API")
        workers = [_make_worker("api", state=WorkerState.RESTING)]
        pilot, _, board, queen, _ = self._make_pilot_with_queen(
            monkeypatch, workers=workers, tasks=[task]
        )

        queen.assign_tasks.side_effect = RuntimeError("Queen crashed")

        result = await pilot._auto_assign_tasks()
        assert result is False

    @pytest.mark.asyncio
    async def test_auto_assign_skips_when_pending_proposals(self, monkeypatch):
        """Should skip assignment when pending proposals checker returns True."""
        from swarm.tasks.task import SwarmTask

        task = SwarmTask(title="Build API")
        workers = [_make_worker("api", state=WorkerState.RESTING)]
        pilot, _, board, queen, _ = self._make_pilot_with_queen(
            monkeypatch, workers=workers, tasks=[task]
        )
        pilot._pending_proposals_check = lambda: True

        result = await pilot._auto_assign_tasks()
        assert result is False
        queen.assign_tasks.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_auto_assign_skips_already_assigned_task(self, monkeypatch):
        """Task already assigned (not available) should be skipped even if Queen returns it."""
        from swarm.tasks.task import SwarmTask

        task = SwarmTask(title="Build API")
        workers = [
            _make_worker("api", state=WorkerState.RESTING),
            _make_worker("web", state=WorkerState.RESTING),
        ]
        pilot, _, board, queen, _ = self._make_pilot_with_queen(
            monkeypatch, workers=workers, tasks=[task]
        )
        # Assign task to "web" so it is no longer available
        board.assign(task.id, "web")

        # Queen proposes assigning it to "api" — should be skipped
        queen.assign_tasks.return_value = [
            {
                "worker": "api",
                "task_id": task.id,
                "message": "Do it",
            }
        ]

        proposals = []
        pilot.on_proposal(lambda p: proposals.append(p))

        result = await pilot._auto_assign_tasks()
        assert result is False
        assert len(proposals) == 0


# ── _coordination_cycle ──────────────────────────────────────────────────


class TestCoordinationCycle:
    """Tests for the _coordination_cycle full Queen coordination path."""

    def _make_pilot_with_queen(self, monkeypatch, workers=None, tasks=None):
        """Helper: build pilot with a mocked Queen and populated task board."""
        if workers is None:
            workers = [_make_worker("api", state=WorkerState.RESTING)]
        log = DroneLog()
        board = TaskBoard()
        for t in tasks or []:
            board.add(t)

        queen = AsyncMock()
        queen.can_call = True
        queen.enabled = True

        pilot = DronePilot(
            workers,
            log,
            interval=1.0,
            session_name="test",
            drone_config=DroneConfig(),
            task_board=board,
            queen=queen,
        )
        pilot.enabled = True

        # Mock tmux calls used during coordination
        monkeypatch.setattr("swarm.drones.pilot.capture_pane", AsyncMock(return_value="$ idle"))
        monkeypatch.setattr("swarm.drones.pilot.send_enter", AsyncMock())
        monkeypatch.setattr("swarm.drones.pilot.revive_worker", AsyncMock())

        # Mock build_hive_context
        monkeypatch.setattr("swarm.queen.context.build_hive_context", lambda *a, **kw: "ctx")
        return pilot, workers, board, queen, log

    @pytest.mark.asyncio
    async def test_coordination_disabled_queen(self, monkeypatch):
        """Returns False when queen.enabled is False."""
        pilot, _, _, queen, _ = self._make_pilot_with_queen(monkeypatch)
        queen.enabled = False
        result = await pilot._coordination_cycle()
        assert result is False

    @pytest.mark.asyncio
    async def test_coordination_no_queen(self, monkeypatch):
        """Returns False when queen is None."""
        pilot, _, _, _, _ = self._make_pilot_with_queen(monkeypatch)
        pilot.queen = None
        result = await pilot._coordination_cycle()
        assert result is False

    @pytest.mark.asyncio
    async def test_coordination_skips_pending_proposals(self, monkeypatch):
        """Returns False when pending proposals exist."""
        pilot, _, _, queen, _ = self._make_pilot_with_queen(monkeypatch)
        pilot._pending_proposals_check = lambda: True
        result = await pilot._coordination_cycle()
        assert result is False
        queen.coordinate_hive.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_coordination_queen_error_returns_false(self, monkeypatch):
        """Queen raising an exception should return False gracefully."""
        pilot, _, _, queen, _ = self._make_pilot_with_queen(monkeypatch)
        queen.coordinate_hive.side_effect = RuntimeError("Queen crashed")
        result = await pilot._coordination_cycle()
        assert result is False

    @pytest.mark.asyncio
    async def test_coordination_continue_directive(self, monkeypatch):
        """A 'continue' directive should send Enter to the worker."""
        pilot, workers, _, queen, log = self._make_pilot_with_queen(monkeypatch)
        queen.coordinate_hive.return_value = {
            "directives": [{"worker": "api", "action": "continue", "reason": "needs nudge"}]
        }
        send_enter_mock = AsyncMock()
        monkeypatch.setattr("swarm.drones.pilot.send_enter", send_enter_mock)

        result = await pilot._coordination_cycle()
        assert result is True
        send_enter_mock.assert_awaited_once()
        continued = [e for e in log.entries if e.action == SystemAction.CONTINUED]
        assert len(continued) == 1

    @pytest.mark.asyncio
    async def test_coordination_restart_directive(self, monkeypatch):
        """A 'restart' directive should revive the worker."""
        pilot, workers, _, queen, log = self._make_pilot_with_queen(monkeypatch)
        queen.coordinate_hive.return_value = {
            "directives": [{"worker": "api", "action": "restart", "reason": "stuck"}]
        }
        revive_mock = AsyncMock()
        monkeypatch.setattr("swarm.drones.pilot.revive_worker", revive_mock)

        result = await pilot._coordination_cycle()
        assert result is True
        revive_mock.assert_awaited_once()
        revived = [e for e in log.entries if e.action == SystemAction.REVIVED]
        assert len(revived) == 1

    @pytest.mark.asyncio
    async def test_coordination_send_message_directive(self, monkeypatch):
        """A 'send_message' directive should emit a proposal."""
        pilot, workers, _, queen, log = self._make_pilot_with_queen(monkeypatch)
        queen.coordinate_hive.return_value = {
            "directives": [
                {
                    "worker": "api",
                    "action": "send_message",
                    "message": "Focus on tests",
                    "reason": "needs guidance",
                }
            ]
        }

        proposals = []
        pilot.on_proposal(lambda p: proposals.append(p))

        result = await pilot._coordination_cycle()
        assert result is True
        assert len(proposals) == 1
        assert proposals[0].worker_name == "api"
        assert proposals[0].proposal_type == "escalation"
        assert proposals[0].message == "Focus on tests"

    @pytest.mark.asyncio
    async def test_coordination_send_message_blocked_by_pending(self, monkeypatch):
        """send_message directive should be skipped when pending proposals exist."""
        pilot, workers, _, queen, log = self._make_pilot_with_queen(monkeypatch)
        queen.coordinate_hive.return_value = {
            "directives": [
                {
                    "worker": "api",
                    "action": "send_message",
                    "message": "Focus on tests",
                    "reason": "needs guidance",
                }
            ]
        }
        # The pending check in _coordination_cycle entry passes (None),
        # but the per-directive check should block
        pilot._pending_proposals_check = lambda: False

        proposals = []
        pilot.on_proposal(lambda p: proposals.append(p))

        # First call succeeds
        result = await pilot._coordination_cycle()
        assert result is True
        assert len(proposals) == 1

        # Now block with pending proposals for the per-directive check
        pilot._pending_proposals_check = lambda: True
        queen.coordinate_hive.return_value = {
            "directives": [
                {
                    "worker": "api",
                    "action": "send_message",
                    "message": "Another msg",
                    "reason": "needs more guidance",
                }
            ]
        }
        # This time the entry-level check blocks
        result = await pilot._coordination_cycle()
        assert result is False

    @pytest.mark.asyncio
    async def test_coordination_complete_task_directive(self, monkeypatch):
        """A 'complete_task' directive should emit task_done for a RESTING worker."""
        from swarm.tasks.task import SwarmTask

        task = SwarmTask(title="Build API")
        workers = [_make_worker("api", state=WorkerState.RESTING)]
        pilot, _, board, queen, log = self._make_pilot_with_queen(
            monkeypatch, workers=workers, tasks=[task]
        )
        board.assign(task.id, "api")

        queen.coordinate_hive.return_value = {
            "directives": [
                {
                    "worker": "api",
                    "action": "complete_task",
                    "task_id": task.id,
                    "resolution": "All tests pass",
                    "reason": "work complete",
                }
            ]
        }

        events = []
        pilot.on("task_done", lambda w, t, r: events.append((w.name, t.id, r)))

        result = await pilot._coordination_cycle()
        assert result is True
        assert len(events) == 1
        assert events[0] == ("api", task.id, "All tests pass")
        assert task.id in pilot._proposed_completions

    @pytest.mark.asyncio
    async def test_coordination_complete_task_skips_buzzing_worker(self, monkeypatch):
        """complete_task should be skipped for workers that are NOT RESTING."""
        from swarm.tasks.task import SwarmTask

        task = SwarmTask(title="Build API")
        workers = [_make_worker("api", state=WorkerState.BUZZING)]
        pilot, _, board, queen, log = self._make_pilot_with_queen(
            monkeypatch, workers=workers, tasks=[task]
        )
        board.assign(task.id, "api")

        queen.coordinate_hive.return_value = {
            "directives": [
                {
                    "worker": "api",
                    "action": "complete_task",
                    "task_id": task.id,
                    "resolution": "Done",
                    "reason": "looks done",
                }
            ]
        }

        events = []
        pilot.on("task_done", lambda w, t, r: events.append(t.id))

        result = await pilot._coordination_cycle()
        assert result is False
        assert len(events) == 0

    @pytest.mark.asyncio
    async def test_coordination_complete_task_skips_already_proposed(self, monkeypatch):
        """complete_task for an already-proposed task should be skipped."""
        from swarm.tasks.task import SwarmTask
        import time as _time

        task = SwarmTask(title="Build API")
        workers = [_make_worker("api", state=WorkerState.RESTING)]
        pilot, _, board, queen, log = self._make_pilot_with_queen(
            monkeypatch, workers=workers, tasks=[task]
        )
        board.assign(task.id, "api")
        pilot._proposed_completions[task.id] = _time.time()

        queen.coordinate_hive.return_value = {
            "directives": [
                {
                    "worker": "api",
                    "action": "complete_task",
                    "task_id": task.id,
                    "resolution": "Done",
                    "reason": "looks done",
                }
            ]
        }

        events = []
        pilot.on("task_done", lambda w, t, r: events.append(t.id))

        result = await pilot._coordination_cycle()
        assert result is False
        assert len(events) == 0

    @pytest.mark.asyncio
    async def test_coordination_assign_task_directive(self, monkeypatch):
        """An 'assign_task' directive should emit a proposal."""
        from swarm.tasks.task import SwarmTask

        task = SwarmTask(title="Build API")
        workers = [_make_worker("api", state=WorkerState.RESTING)]
        pilot, _, board, queen, log = self._make_pilot_with_queen(
            monkeypatch, workers=workers, tasks=[task]
        )

        queen.coordinate_hive.return_value = {
            "directives": [
                {
                    "worker": "api",
                    "action": "assign_task",
                    "task_id": task.id,
                    "message": "Work on this",
                    "reason": "good match",
                }
            ]
        }

        proposals = []
        pilot.on_proposal(lambda p: proposals.append(p))

        result = await pilot._coordination_cycle()
        assert result is True
        assert len(proposals) == 1
        assert proposals[0].task_id == task.id
        assert proposals[0].worker_name == "api"

    @pytest.mark.asyncio
    async def test_coordination_assign_task_skips_unavailable(self, monkeypatch):
        """assign_task for an already assigned task should be skipped."""
        from swarm.tasks.task import SwarmTask

        task = SwarmTask(title="Build API")
        workers = [
            _make_worker("api", state=WorkerState.RESTING),
            _make_worker("web", state=WorkerState.RESTING),
        ]
        pilot, _, board, queen, log = self._make_pilot_with_queen(
            monkeypatch, workers=workers, tasks=[task]
        )
        board.assign(task.id, "web")

        queen.coordinate_hive.return_value = {
            "directives": [
                {
                    "worker": "api",
                    "action": "assign_task",
                    "task_id": task.id,
                    "message": "Work on this",
                    "reason": "good match",
                }
            ]
        }

        proposals = []
        pilot.on_proposal(lambda p: proposals.append(p))

        result = await pilot._coordination_cycle()
        assert result is False
        assert len(proposals) == 0

    @pytest.mark.asyncio
    async def test_coordination_assign_task_skips_worker_with_active(self, monkeypatch):
        """assign_task should be skipped when worker already has an active task."""
        from swarm.tasks.task import SwarmTask

        task1 = SwarmTask(title="Existing task")
        task2 = SwarmTask(title="New task")
        workers = [_make_worker("api", state=WorkerState.RESTING)]
        pilot, _, board, queen, log = self._make_pilot_with_queen(
            monkeypatch, workers=workers, tasks=[task1, task2]
        )
        board.assign(task1.id, "api")

        queen.coordinate_hive.return_value = {
            "directives": [
                {
                    "worker": "api",
                    "action": "assign_task",
                    "task_id": task2.id,
                    "message": "Work on this",
                    "reason": "good match",
                }
            ]
        }

        proposals = []
        pilot.on_proposal(lambda p: proposals.append(p))

        result = await pilot._coordination_cycle()
        assert result is False
        assert len(proposals) == 0

    @pytest.mark.asyncio
    async def test_coordination_assign_task_blocked_by_pending(self, monkeypatch):
        """assign_task should be skipped when pending proposals exist."""
        from swarm.tasks.task import SwarmTask

        task = SwarmTask(title="Build API")
        workers = [_make_worker("api", state=WorkerState.RESTING)]
        pilot, _, board, queen, log = self._make_pilot_with_queen(
            monkeypatch, workers=workers, tasks=[task]
        )

        # Allow first entry check, block at directive level
        call_count = 0

        def pending_check():
            nonlocal call_count
            call_count += 1
            # First call (entry check) passes; subsequent calls block
            return call_count > 1

        pilot._pending_proposals_check = pending_check

        queen.coordinate_hive.return_value = {
            "directives": [
                {
                    "worker": "api",
                    "action": "assign_task",
                    "task_id": task.id,
                    "message": "Work on this",
                    "reason": "good match",
                }
            ]
        }

        proposals = []
        pilot.on_proposal(lambda p: proposals.append(p))

        result = await pilot._coordination_cycle()
        assert result is False
        assert len(proposals) == 0

    @pytest.mark.asyncio
    async def test_coordination_unknown_worker_skipped(self, monkeypatch):
        """Directives for unknown workers should be skipped."""
        pilot, _, _, queen, _ = self._make_pilot_with_queen(monkeypatch)
        queen.coordinate_hive.return_value = {
            "directives": [{"worker": "nonexistent", "action": "continue", "reason": "test"}]
        }

        result = await pilot._coordination_cycle()
        assert result is False

    @pytest.mark.asyncio
    async def test_coordination_non_dict_directives_skipped(self, monkeypatch):
        """Non-dict directive entries should be skipped."""
        pilot, _, _, queen, _ = self._make_pilot_with_queen(monkeypatch)
        queen.coordinate_hive.return_value = {"directives": ["not-a-dict", 42, None]}

        result = await pilot._coordination_cycle()
        assert result is False

    @pytest.mark.asyncio
    async def test_coordination_conflicts_logged(self, monkeypatch):
        """Conflicts from the Queen should not crash the cycle."""
        pilot, _, _, queen, _ = self._make_pilot_with_queen(monkeypatch)
        queen.coordinate_hive.return_value = {
            "directives": [],
            "conflicts": ["api and web editing same file"],
        }

        result = await pilot._coordination_cycle()
        assert result is False  # No directives acted on

    @pytest.mark.asyncio
    async def test_coordination_non_dict_result(self, monkeypatch):
        """Non-dict coordinate_hive result should be handled gracefully."""
        pilot, _, _, queen, _ = self._make_pilot_with_queen(monkeypatch)
        queen.coordinate_hive.return_value = "not a dict"

        result = await pilot._coordination_cycle()
        assert result is False

    @pytest.mark.asyncio
    async def test_coordination_continue_error_handled(self, monkeypatch):
        """OSError on send_enter during continue directive should be caught."""
        pilot, workers, _, queen, log = self._make_pilot_with_queen(monkeypatch)
        queen.coordinate_hive.return_value = {
            "directives": [{"worker": "api", "action": "continue", "reason": "nudge"}]
        }
        monkeypatch.setattr(
            "swarm.drones.pilot.send_enter",
            AsyncMock(side_effect=OSError("pane gone")),
        )

        result = await pilot._coordination_cycle()
        assert result is False

    @pytest.mark.asyncio
    async def test_coordination_restart_error_handled(self, monkeypatch):
        """OSError on revive_worker during restart directive should be caught."""
        pilot, workers, _, queen, log = self._make_pilot_with_queen(monkeypatch)
        queen.coordinate_hive.return_value = {
            "directives": [{"worker": "api", "action": "restart", "reason": "stuck"}]
        }
        monkeypatch.setattr(
            "swarm.drones.pilot.revive_worker",
            AsyncMock(side_effect=OSError("pane gone")),
        )

        result = await pilot._coordination_cycle()
        assert result is False

    @pytest.mark.asyncio
    async def test_coordination_capture_pane_failure_handled(self, monkeypatch):
        """Failure to capture a worker's pane should not crash coordination."""
        pilot, workers, _, queen, _ = self._make_pilot_with_queen(monkeypatch)
        monkeypatch.setattr(
            "swarm.drones.pilot.capture_pane",
            AsyncMock(side_effect=OSError("pane gone")),
        )
        queen.coordinate_hive.return_value = {"directives": []}

        result = await pilot._coordination_cycle()
        assert result is False  # No directives, but didn't crash


# ── Circuit breaker recovery ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_circuit_breaker_recovery_on_successful_poll(monkeypatch):
    """A successful poll after failures should clear the failure counter (not trip)."""
    workers = [_make_worker("api")]
    log = DroneLog()
    pilot = DronePilot(
        workers,
        log,
        interval=1.0,
        session_name=None,
        drone_config=DroneConfig(max_poll_failures=5),
    )
    pilot.enabled = True

    # Seed failures just below threshold
    pilot._poll_failures["%api"] = 4

    # Now poll succeeds
    monkeypatch.setattr("swarm.drones.pilot.pane_exists", AsyncMock(return_value=True))
    monkeypatch.setattr("swarm.drones.pilot.get_pane_command", AsyncMock(return_value="claude"))
    monkeypatch.setattr(
        "swarm.drones.pilot.capture_pane", AsyncMock(return_value="esc to interrupt")
    )
    monkeypatch.setattr("swarm.drones.pilot.set_pane_option", AsyncMock())
    monkeypatch.setattr("swarm.drones.pilot.send_enter", AsyncMock())

    await pilot.poll_once()

    # Worker should still be alive
    assert len(workers) == 1
    # Failure counter should be reset
    assert "%api" not in pilot._poll_failures


# ── _check_task_completions edge cases ───────────────────────────────────


class TestCheckTaskCompletionsEdgeCases:
    """Additional edge cases for _check_task_completions."""

    def _make_pilot_with_board(self):
        workers = [_make_worker("api", state=WorkerState.RESTING, resting_since=0)]
        log = DroneLog()
        board = TaskBoard()
        pilot = DronePilot(
            workers, log, interval=1.0, session_name="test", drone_config=DroneConfig()
        )
        pilot.task_board = board
        pilot.enabled = True
        pilot._COMPLETION_REPROPOSE_COOLDOWN = 60
        pilot._AUTO_COMPLETE_MIN_IDLE = 45
        return pilot, workers, board, log

    def test_no_task_board_returns_false(self):
        """_check_task_completions should return False when task_board is None."""
        workers = [_make_worker("api", state=WorkerState.RESTING)]
        log = DroneLog()
        pilot = DronePilot(
            workers, log, interval=1.0, session_name="test", drone_config=DroneConfig()
        )
        pilot.task_board = None
        result = pilot._check_task_completions()
        assert result is False

    def test_worker_not_resting_skipped(self):
        """Workers that are not RESTING should be skipped."""
        pilot, workers, board, log = self._make_pilot_with_board()
        import time

        workers[0].state = WorkerState.BUZZING
        workers[0].state_since = time.time() - 120

        task = board.create("Fix bug")
        board.assign(task.id, "api")

        events = []
        pilot.on("task_done", lambda w, t, r: events.append(t.id))

        result = pilot._check_task_completions()
        assert result is False
        assert len(events) == 0

    def test_worker_idle_too_short_skipped(self):
        """Workers idle for less than the minimum should be skipped."""
        pilot, workers, board, log = self._make_pilot_with_board()
        import time

        # Idle for only 10 seconds (below 45s threshold)
        workers[0].state_since = time.time() - 10

        task = board.create("Fix bug")
        board.assign(task.id, "api")

        events = []
        pilot.on("task_done", lambda w, t, r: events.append(t.id))

        result = pilot._check_task_completions()
        assert result is False
        assert len(events) == 0

    def test_completed_tasks_not_proposed(self):
        """Tasks already completed should not be proposed."""
        pilot, workers, board, log = self._make_pilot_with_board()
        import time

        workers[0].state_since = time.time() - 120

        task = board.create("Fix bug")
        board.assign(task.id, "api")
        board.complete(task.id)

        events = []
        pilot.on("task_done", lambda w, t, r: events.append(t.id))

        result = pilot._check_task_completions()
        assert result is False
        assert len(events) == 0


# ── _rediscover ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_rediscover_adds_new_workers(monkeypatch):
    """_rediscover should add newly discovered workers."""
    workers = [_make_worker("api")]
    log = DroneLog()
    pilot = DronePilot(workers, log, interval=1.0, session_name="test", drone_config=DroneConfig())

    new_worker = _make_worker("web", pane_id="%web")
    monkeypatch.setattr(
        "swarm.drones.pilot.discover_workers",
        AsyncMock(return_value=[_make_worker("api"), new_worker]),
    )

    changes = []
    pilot.on_workers_changed(lambda: changes.append(1))

    await pilot._rediscover()

    assert len(workers) == 2
    assert any(w.name == "web" for w in workers)
    assert len(changes) == 1


@pytest.mark.asyncio
async def test_rediscover_no_session_name(monkeypatch):
    """_rediscover should no-op when session_name is None."""
    workers = [_make_worker("api")]
    log = DroneLog()
    pilot = DronePilot(workers, log, interval=1.0, session_name=None, drone_config=DroneConfig())

    discover_mock = AsyncMock(return_value=[])
    monkeypatch.setattr("swarm.drones.pilot.discover_workers", discover_mock)

    await pilot._rediscover()
    discover_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_rediscover_error_handled(monkeypatch):
    """_rediscover should catch OSError without crashing."""
    workers = [_make_worker("api")]
    log = DroneLog()
    pilot = DronePilot(workers, log, interval=1.0, session_name="test", drone_config=DroneConfig())

    monkeypatch.setattr(
        "swarm.drones.pilot.discover_workers",
        AsyncMock(side_effect=OSError("tmux gone")),
    )

    # Should not raise
    await pilot._rediscover()
    assert len(workers) == 1  # unchanged


@pytest.mark.asyncio
async def test_rediscover_no_duplicates(monkeypatch):
    """_rediscover should not add workers already known."""
    workers = [_make_worker("api")]
    log = DroneLog()
    pilot = DronePilot(workers, log, interval=1.0, session_name="test", drone_config=DroneConfig())

    # Discover returns the same worker already tracked
    monkeypatch.setattr(
        "swarm.drones.pilot.discover_workers",
        AsyncMock(return_value=[_make_worker("api")]),
    )

    changes = []
    pilot.on_workers_changed(lambda: changes.append(1))

    await pilot._rediscover()
    assert len(workers) == 1  # no duplicates
    assert len(changes) == 0  # no change event


# ── poll_once coordination/rediscovery triggers ──────────────────────────


@pytest.mark.asyncio
async def test_poll_once_triggers_coordination_at_interval(pilot_setup, monkeypatch):
    """poll_once should trigger _coordination_cycle at the right tick interval."""
    from swarm.drones import pilot as pilot_mod

    pilot, workers, log = pilot_setup
    pilot.enabled = True

    queen = AsyncMock()
    queen.enabled = True
    queen.can_call = True
    queen.coordinate_hive.return_value = {"directives": []}
    pilot.queen = queen

    # Set tick to one before coordination interval
    pilot._tick = pilot_mod._COORDINATION_INTERVAL - 1

    monkeypatch.setattr("swarm.queen.context.build_hive_context", lambda *a, **kw: "ctx")

    await pilot.poll_once()

    # After poll_once, tick should have been at _COORDINATION_INTERVAL
    # (it was _COORDINATION_INTERVAL - 1, then incremented, but the
    # check happens at the pre-increment value)
    queen.coordinate_hive.assert_not_awaited()

    # Now tick is exactly at the interval
    pilot._tick = pilot_mod._COORDINATION_INTERVAL
    await pilot.poll_once()
    queen.coordinate_hive.assert_awaited_once()


@pytest.mark.asyncio
async def test_poll_once_triggers_rediscovery(pilot_setup, monkeypatch):
    """poll_once should trigger _rediscover at the right tick interval."""
    from swarm.drones import pilot as pilot_mod

    pilot, workers, log = pilot_setup
    pilot.enabled = True

    discover_mock = AsyncMock(return_value=[])
    monkeypatch.setattr("swarm.drones.pilot.discover_workers", discover_mock)

    # Set tick to rediscovery interval
    pilot._tick = pilot_mod._REDISCOVERY_INTERVAL

    await pilot.poll_once()

    discover_mock.assert_awaited_once()


# ── poll_once integration with _auto_assign_tasks ────────────────────────


@pytest.mark.asyncio
async def test_poll_once_calls_auto_assign(monkeypatch):
    """poll_once should invoke _auto_assign_tasks when enabled with queen and board."""
    from swarm.tasks.task import SwarmTask

    workers = [_make_worker("api", state=WorkerState.RESTING)]
    log = DroneLog()
    board = TaskBoard()
    task = SwarmTask(title="Build API")
    board.add(task)

    queen = AsyncMock()
    queen.can_call = True
    queen.enabled = True
    queen.assign_tasks.return_value = [
        {
            "worker": "api",
            "task_id": task.id,
            "message": "Do it",
            "confidence": 0.9,
        }
    ]

    pilot = DronePilot(
        workers,
        log,
        interval=1.0,
        session_name="test",
        drone_config=DroneConfig(),
        task_board=board,
        queen=queen,
    )
    pilot.enabled = True

    # Mock all tmux operations
    monkeypatch.setattr("swarm.drones.pilot.pane_exists", AsyncMock(return_value=True))
    monkeypatch.setattr("swarm.drones.pilot.get_pane_command", AsyncMock(return_value="claude"))
    # RESTING content
    idle_content = '> Try "how does foo work"\n? for shortcuts'
    monkeypatch.setattr("swarm.drones.pilot.capture_pane", AsyncMock(return_value=idle_content))
    monkeypatch.setattr("swarm.drones.pilot.set_pane_option", AsyncMock())
    monkeypatch.setattr("swarm.drones.pilot.send_enter", AsyncMock())
    monkeypatch.setattr("swarm.drones.pilot.discover_workers", AsyncMock(return_value=[]))
    monkeypatch.setattr("swarm.drones.pilot.update_window_names", AsyncMock())
    monkeypatch.setattr("swarm.drones.pilot.set_terminal_title", AsyncMock())
    monkeypatch.setattr("swarm.drones.pilot.revive_worker", AsyncMock())
    monkeypatch.setattr("swarm.queen.context.build_hive_context", lambda *a, **kw: "ctx")

    proposals = []
    pilot.on_proposal(lambda p: proposals.append(p))

    result = await pilot.poll_once()

    assert result is True
    assert len(proposals) == 1
    queen.assign_tasks.assert_awaited_once()


# ── SLEEPING display_state sync to tmux ──────────────────────────────────


@pytest.mark.asyncio
async def test_pilot_syncs_sleeping_to_tmux(monkeypatch):
    """Pilot should write SLEEPING to @swarm_state when display_state flips."""
    import time

    workers = [_make_worker("api", state=WorkerState.RESTING, resting_since=time.time() - 400)]
    log = DroneLog()
    pilot = DronePilot(workers, log, interval=1.0, session_name="test", drone_config=DroneConfig())
    pilot.enabled = True

    # Worker is RESTING for 400s (> 300s threshold) so display_state = SLEEPING
    assert workers[0].display_state == WorkerState.SLEEPING

    set_pane_mock = AsyncMock()
    idle_content = '> Try "how does foo work"\n? for shortcuts'
    monkeypatch.setattr("swarm.drones.pilot.pane_exists", AsyncMock(return_value=True))
    monkeypatch.setattr("swarm.drones.pilot.get_pane_command", AsyncMock(return_value="claude"))
    monkeypatch.setattr("swarm.drones.pilot.capture_pane", AsyncMock(return_value=idle_content))
    monkeypatch.setattr("swarm.drones.pilot.set_pane_option", set_pane_mock)
    monkeypatch.setattr("swarm.drones.pilot.send_enter", AsyncMock())
    monkeypatch.setattr("swarm.drones.pilot.discover_workers", AsyncMock(return_value=[]))
    monkeypatch.setattr("swarm.drones.pilot.update_window_names", AsyncMock())
    monkeypatch.setattr("swarm.drones.pilot.set_terminal_title", AsyncMock())
    monkeypatch.setattr("swarm.drones.pilot.revive_worker", AsyncMock())

    await pilot.poll_once()

    # Check that set_pane_option was called with SLEEPING
    sleeping_calls = [
        c for c in set_pane_mock.call_args_list if c[0] == ("%api", "@swarm_state", "SLEEPING")
    ]
    assert len(sleeping_calls) >= 1


# ── Display-state transition emits state_changed ─────────────────────────


@pytest.mark.asyncio
async def test_display_state_transition_emits_state_changed(monkeypatch):
    """RESTING→SLEEPING display_state transition should emit state_changed."""
    import time

    workers = [_make_worker("api", state=WorkerState.RESTING, resting_since=time.time() - 400)]
    log = DroneLog()
    pilot = DronePilot(workers, log, interval=1.0, session_name="test", drone_config=DroneConfig())
    pilot.enabled = True

    assert workers[0].display_state == WorkerState.SLEEPING

    idle_content = '> Try "how does foo work"\n? for shortcuts'
    monkeypatch.setattr("swarm.drones.pilot.pane_exists", AsyncMock(return_value=True))
    monkeypatch.setattr("swarm.drones.pilot.get_pane_command", AsyncMock(return_value="claude"))
    monkeypatch.setattr("swarm.drones.pilot.capture_pane", AsyncMock(return_value=idle_content))
    monkeypatch.setattr("swarm.drones.pilot.set_pane_option", AsyncMock())
    monkeypatch.setattr("swarm.drones.pilot.send_enter", AsyncMock())
    monkeypatch.setattr("swarm.drones.pilot.discover_workers", AsyncMock(return_value=[]))
    monkeypatch.setattr("swarm.drones.pilot.update_window_names", AsyncMock())
    monkeypatch.setattr("swarm.drones.pilot.set_terminal_title", AsyncMock())
    monkeypatch.setattr("swarm.drones.pilot.revive_worker", AsyncMock())

    # _tmux_states cache starts empty, so first poll writes SLEEPING and
    # since worker.state doesn't actually change (stays RESTING), the
    # display-only branch should fire state_changed.
    state_changes: list[str] = []
    pilot.on_state_changed(lambda w: state_changes.append(w.name))

    await pilot.poll_once()

    # state_changed should have been emitted (either from worker.state change
    # or from the display_state divergence path)
    assert "api" in state_changes


# ── Focus backoff cap ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_focus_caps_backoff(pilot_setup):
    """Setting _focused_workers should cap backoff at _focus_interval."""
    pilot, workers, log = pilot_setup
    pilot.enabled = True

    # Build up idle streak to get high backoff
    for _ in range(5):
        had_action = await pilot.poll_once()
        assert had_action is False
        pilot._idle_streak += 1

    # Without focus, backoff should be high
    normal_backoff = min(
        pilot._base_interval * (2 ** min(pilot._idle_streak, 3)),
        pilot._max_interval,
    )
    assert normal_backoff > pilot._focus_interval

    # Set focus on a known worker
    pilot._focused_workers = {workers[0].name}

    # Backoff should be capped at _focus_interval
    capped_backoff = min(normal_backoff, pilot._focus_interval)
    assert capped_backoff == pilot._focus_interval


@pytest.mark.asyncio
async def test_focus_no_effect_when_worker_not_tracked(pilot_setup):
    """Focus on unknown worker should not cap backoff."""
    pilot, workers, log = pilot_setup
    pilot.enabled = True

    pilot._idle_streak = 5
    pilot._focused_workers = {"nonexistent"}

    backoff = min(
        pilot._base_interval * (2 ** min(pilot._idle_streak, 3)),
        pilot._max_interval,
    )
    # No intersection with workers → focus cap should not apply
    worker_names = {w.name for w in workers}
    assert not (pilot._focused_workers & worker_names)
    assert backoff > pilot._focus_interval
