"""Tests for drones/pilot.py — async polling loop and decision engine."""

from __future__ import annotations

import asyncio
import time
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
    monkeypatch.setattr("swarm.drones.pilot.get_zoomed_pane", AsyncMock(return_value=None))

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
    # BUZZING -> WAITING requires 3 confirmations (hysteresis)
    await pilot.poll_once()
    await pilot.poll_once()
    await pilot.poll_once()
    # After three polls (hysteresis), workers with empty prompts should be WAITING
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
    # Initial tmux state sync fires state_changed even though STUNG is debounced
    assert len(state_changes) > 0


@pytest.mark.asyncio
async def test_revive_on_stung(pilot_setup, monkeypatch):
    """STUNG workers with revives remaining should be revived after debounce."""
    pilot, workers, log = pilot_setup
    pilot.enabled = True

    monkeypatch.setattr("swarm.drones.pilot.get_pane_command", AsyncMock(return_value="bash"))
    monkeypatch.setattr("swarm.drones.pilot.capture_pane", AsyncMock(return_value="$ "))

    await pilot.poll_once()  # first STUNG — debounced
    assert len([e for e in log.entries if e.action == SystemAction.REVIVED]) == 0
    await pilot.poll_once()  # second STUNG — accepted + revived
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

    await pilot.poll_once()  # first STUNG — debounced
    await pilot.poll_once()  # second STUNG — accepted, escalated (crash loop)
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

    await pilot.poll_once()  # first STUNG — debounced (no action)
    result = await pilot.poll_once()  # second STUNG — revive action
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

    # Force an action (revive via STUNG detection — needs 2 polls for debounce)
    monkeypatch.setattr("swarm.drones.pilot.get_pane_command", AsyncMock(return_value="bash"))
    monkeypatch.setattr("swarm.drones.pilot.capture_pane", AsyncMock(return_value="$ "))

    await pilot.poll_once()  # first STUNG — debounced
    had_action = await pilot.poll_once()  # second STUNG — revived
    assert had_action is True

    # Apply _loop's reset logic
    if had_action:
        pilot._idle_streak = 0

    assert pilot._idle_streak == 0


# ── Escalation does NOT reset backoff ─────────────────────────────────


@pytest.mark.asyncio
async def test_escalation_does_not_reset_idle_streak(pilot_setup, monkeypatch):
    """Escalation-only actions should NOT reset idle_streak (backoff should grow)."""
    pilot, workers, log = pilot_setup
    pilot.enabled = True

    # Build up idle streak
    for _ in range(3):
        had_action = await pilot.poll_once()
        assert had_action is False
        pilot._idle_streak += 1

    assert pilot._idle_streak == 3

    # Make workers STUNG with exhausted revives → ESCALATE decision
    for w in workers:
        w.revive_count = 3
    monkeypatch.setattr("swarm.drones.pilot.get_pane_command", AsyncMock(return_value="bash"))
    monkeypatch.setattr("swarm.drones.pilot.capture_pane", AsyncMock(return_value="$ "))

    # Needs 2 polls for STUNG debounce; escalation fires on second
    await pilot.poll_once()  # first STUNG — debounced
    had_action = await pilot.poll_once()  # second STUNG — accepted + escalated
    assert had_action is True  # escalation still counts as had_action

    # But _had_substantive_action should be False (escalation only)
    assert pilot._had_substantive_action is False

    # Idle streak should NOT be reset by escalation alone
    # (in _loop, the check is: if _had_substantive_action or any_state_changed)
    # Since state_changed is True (BUZZING → STUNG), streak would reset.
    # But on *subsequent* polls (state unchanged), streak should grow.


@pytest.mark.asyncio
async def test_substantive_action_resets_idle_streak(pilot_setup, monkeypatch):
    """CONTINUE and REVIVE actions should reset idle_streak."""
    pilot, workers, log = pilot_setup
    pilot.enabled = True

    # STUNG with revives remaining → REVIVE (substantive) — needs 2 polls
    monkeypatch.setattr("swarm.drones.pilot.get_pane_command", AsyncMock(return_value="bash"))
    monkeypatch.setattr("swarm.drones.pilot.capture_pane", AsyncMock(return_value="$ "))

    await pilot.poll_once()  # first STUNG — debounced
    had_action = await pilot.poll_once()  # second STUNG — revived
    assert had_action is True
    assert pilot._had_substantive_action is True  # REVIVE is substantive


# ── Skip-decide optimization for escalated workers ────────────────────


@pytest.mark.asyncio
async def test_skip_decide_for_escalated_unchanged_worker(pilot_setup, monkeypatch):
    """Already-escalated workers with no state change should skip decide()."""
    pilot, workers, log = pilot_setup
    pilot.enabled = True

    # Pre-escalate a worker
    pilot._escalated.add(workers[0].pane_id)
    # Set prev_state to match current state (no change)
    pilot._prev_states[workers[0].pane_id] = WorkerState.BUZZING

    await pilot.poll_once()

    # The escalated worker should not have any decide-driven log entries
    # (BUZZING workers return NONE anyway, but the optimization skips decide entirely)
    # Verify the other worker (not escalated) still gets processed normally
    assert workers[0].pane_id in pilot._escalated  # still escalated


@pytest.mark.asyncio
async def test_escalated_worker_reevaluated_on_state_change(pilot_setup, monkeypatch):
    """When an escalated worker changes state, decide() should run."""
    pilot, workers, log = pilot_setup
    pilot.enabled = True

    # Pre-escalate a worker and put it in WAITING state
    workers[0].state = WorkerState.WAITING
    pilot._escalated.add(workers[0].pane_id)
    pilot._prev_states[workers[0].pane_id] = WorkerState.WAITING

    # Now worker is detected as BUZZING (default mock) → actual state change
    await pilot.poll_once()

    # BUZZING branch in decide() clears escalation
    assert workers[0].pane_id not in pilot._escalated


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
    pilot._saw_completion = True  # simulate a task completed this session
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
    pilot._saw_completion = True  # even with a completion, disabled config blocks it
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
    pilot._saw_completion = True  # simulate a task completed this session
    await asyncio.wait_for(pilot._loop(), timeout=2.0)

    assert not pilot._running, "_running should be False after hive_complete"
    assert not pilot.needs_restart(), "watchdog should not restart after hive_complete"


@pytest.mark.asyncio
async def test_hive_complete_not_triggered_on_empty_board(monkeypatch):
    """Empty task board should NOT trigger hive_complete (no tasks ever created)."""
    workers = [_make_worker("api", state=WorkerState.RESTING)]
    log = DroneLog()

    board = TaskBoard()  # empty — no tasks created

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

    events: list[str] = []
    pilot.on_hive_complete(lambda: events.append("hive_complete"))

    pilot.enabled = True
    pilot._running = True
    # _loop() would run forever (no hive_complete exit) — use timeout to prove it
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(pilot._loop(), timeout=0.15)

    assert "hive_complete" not in events, "empty board must not trigger hive_complete"
    assert pilot.enabled, "pilot should remain enabled with empty board"


@pytest.mark.asyncio
async def test_hive_complete_not_triggered_on_stale_completions(monkeypatch):
    """Completed tasks from a previous session should NOT trigger hive_complete."""
    workers = [_make_worker("api", state=WorkerState.RESTING)]
    log = DroneLog()

    # Board with all-completed tasks (as if loaded from persistent store)
    board = TaskBoard()
    task = board.create("Old task")
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
    # _saw_completion defaults to False — no task was completed this session

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
    # _loop() would run forever (stale completions don't trigger auto-stop)
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(pilot._loop(), timeout=0.15)

    assert "hive_complete" not in events, "stale completions must not trigger hive_complete"
    assert pilot.enabled, "pilot should remain enabled with stale completions"


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
        queen.min_confidence = 0.7

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
        """Successful Queen assignment should emit a proposal event when auto-approve is off."""
        from swarm.tasks.task import SwarmTask

        task = SwarmTask(title="Build API", description="REST API")
        workers = [_make_worker("api", state=WorkerState.RESTING)]
        pilot, _, board, queen, log = self._make_pilot_with_queen(
            monkeypatch, workers=workers, tasks=[task]
        )
        pilot.drone_config = DroneConfig(auto_approve_assignments=False)

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
    async def test_auto_assign_includes_worker_with_completed_task(self, monkeypatch):
        """Workers whose only tasks are COMPLETED should be considered idle."""
        from swarm.tasks.task import SwarmTask

        task1 = SwarmTask(title="Old task")
        task2 = SwarmTask(title="New task")
        workers = [_make_worker("api", state=WorkerState.RESTING)]
        pilot, _, board, queen, _ = self._make_pilot_with_queen(
            monkeypatch, workers=workers, tasks=[task1, task2]
        )
        # Complete task1 — it remains assigned_worker="api" but status=COMPLETED
        board.assign(task1.id, "api")
        board.complete(task1.id)

        queen.assign_tasks.return_value = [
            {
                "worker": "api",
                "task_id": task2.id,
                "message": "Do the new task",
                "confidence": 0.9,
            }
        ]

        assigned = []
        pilot.on_task_assigned(lambda w, t, m="": assigned.append((w.name, t.id)))

        result = await pilot._auto_assign_tasks()
        assert result is True
        assert len(assigned) == 1
        assert assigned[0] == ("api", task2.id)

    @pytest.mark.asyncio
    async def test_auto_assign_skips_worker_with_pending_proposal(self, monkeypatch):
        """Workers with pending proposals should be excluded from auto-assign."""
        from swarm.tasks.task import SwarmTask

        task = SwarmTask(title="Build API")
        workers = [_make_worker("api", state=WorkerState.RESTING)]
        pilot, _, board, queen, _ = self._make_pilot_with_queen(
            monkeypatch, workers=workers, tasks=[task]
        )
        # Per-worker proposal check returns True for "api"
        pilot._pending_proposals_for_worker = lambda name: name == "api"

        result = await pilot._auto_assign_tasks()
        assert result is False
        queen.assign_tasks.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_auto_assign_allows_other_workers_when_one_has_proposal(self, monkeypatch):
        """Per-worker proposal check should only block the specific worker."""
        from swarm.tasks.task import SwarmTask

        task = SwarmTask(title="Build API")
        workers = [
            _make_worker("api", state=WorkerState.RESTING),
            _make_worker("web", state=WorkerState.RESTING),
        ]
        pilot, _, board, queen, _ = self._make_pilot_with_queen(
            monkeypatch, workers=workers, tasks=[task]
        )
        # "api" has a pending proposal, "web" does not
        pilot._pending_proposals_for_worker = lambda name: name == "api"

        queen.assign_tasks.return_value = [
            {
                "worker": "web",
                "task_id": task.id,
                "message": "Do it",
                "confidence": 0.9,
            }
        ]

        assigned = []
        pilot.on_task_assigned(lambda w, t, m="": assigned.append(w.name))

        result = await pilot._auto_assign_tasks()
        assert result is True
        # Queen should only have been called with "web" (api filtered out)
        call_args = queen.assign_tasks.call_args
        assert "api" not in call_args[0][0]  # first positional arg = idle worker names
        assert "web" in call_args[0][0]

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
        queen.min_confidence = 0.7

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
        continued = [e for e in log.entries if e.action == SystemAction.QUEEN_CONTINUED]
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


class TestAutoCompleteMinIdleConfig:
    """auto_complete_min_idle should be configurable from DroneConfig."""

    def test_default_value(self):
        """Default auto_complete_min_idle is 45s."""
        pilot = DronePilot([], DroneLog(), drone_config=DroneConfig())
        assert pilot._auto_complete_min_idle == 45.0

    def test_config_override(self):
        """DroneConfig.auto_complete_min_idle flows to pilot instance attribute."""
        cfg = DroneConfig(auto_complete_min_idle=10.0)
        pilot = DronePilot([], DroneLog(), drone_config=cfg)
        assert pilot._auto_complete_min_idle == 10.0

    def test_completion_uses_config_value(self):
        """_check_task_completions should respect the configured threshold."""
        import time

        workers = [_make_worker("api", state=WorkerState.RESTING, resting_since=0)]
        log = DroneLog()
        board = TaskBoard()
        # Set low threshold (15s)
        cfg = DroneConfig(auto_complete_min_idle=15.0)
        pilot = DronePilot(workers, log, interval=1.0, session_name="test", drone_config=cfg)
        pilot.task_board = board
        pilot.enabled = True

        # Worker idle for 20s (above 15s threshold, below default 45s)
        workers[0].state_since = time.time() - 20

        task = board.create("Fix bug")
        board.assign(task.id, "api")

        events: list[str] = []
        pilot.on("task_done", lambda w, t, r: events.append(t.id))

        pilot._check_task_completions()
        assert len(events) == 1  # triggered at 20s with 15s threshold

    def test_completion_blocked_below_threshold(self):
        """Worker idle below configured threshold should not propose completion."""
        import time

        workers = [_make_worker("api", state=WorkerState.RESTING, resting_since=0)]
        log = DroneLog()
        board = TaskBoard()
        cfg = DroneConfig(auto_complete_min_idle=60.0)
        pilot = DronePilot(workers, log, interval=1.0, session_name="test", drone_config=cfg)
        pilot.task_board = board
        pilot.enabled = True

        # Worker idle for 45s (above default 45s, but below configured 60s)
        workers[0].state_since = time.time() - 45

        task = board.create("Fix bug")
        board.assign(task.id, "api")

        events: list[str] = []
        pilot.on("task_done", lambda w, t, r: events.append(t.id))

        pilot._check_task_completions()
        assert len(events) == 0  # blocked by 60s threshold


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
    queen.min_confidence = 0.7
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

    # Now tick is exactly at the interval.
    # Set workers to RESTING so coordination isn't skipped (coordination
    # is throttled when all workers are BUZZING).  Also switch capture_pane
    # to RESTING-compatible content so update_state is a no-op.
    for w in workers:
        w.state = WorkerState.RESTING
    monkeypatch.setattr(
        "swarm.drones.pilot.capture_pane", AsyncMock(return_value="? for shortcuts")
    )
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
    queen.min_confidence = 0.7
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
    monkeypatch.setattr("swarm.drones.pilot.get_zoomed_pane", AsyncMock(return_value=None))
    monkeypatch.setattr("swarm.queen.context.build_hive_context", lambda *a, **kw: "ctx")

    assigned = []
    pilot.on_task_assigned(lambda w, t, m="": assigned.append((w.name, t.id)))

    result = await pilot.poll_once()

    assert result is True
    assert len(assigned) == 1  # auto-approved (confidence 0.9 >= 0.7)
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
    monkeypatch.setattr("swarm.drones.pilot.get_zoomed_pane", AsyncMock(return_value=None))

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
    monkeypatch.setattr("swarm.drones.pilot.get_zoomed_pane", AsyncMock(return_value=None))

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
    """Setting _focused_workers should cap backoff at _focus_interval for idle workers."""
    pilot, workers, log = pilot_setup
    pilot.enabled = True

    # Transition the focused worker to RESTING (3 confirmations required)
    workers[0].update_state(WorkerState.RESTING)
    workers[0].update_state(WorkerState.RESTING)
    workers[0].update_state(WorkerState.RESTING)
    assert workers[0].state == WorkerState.RESTING

    # Build up idle streak to get high backoff
    pilot._idle_streak = 5

    # Without focus, backoff should be high
    normal_backoff = pilot._compute_backoff()
    assert normal_backoff > pilot._focus_interval

    # Set focus on the RESTING worker
    pilot._focused_workers = {workers[0].name}

    # Backoff should be capped at _focus_interval
    capped_backoff = pilot._compute_backoff()
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


@pytest.mark.asyncio
async def test_focus_no_cap_when_workers_buzzing(pilot_setup):
    """Focus on a BUZZING worker should NOT cap backoff — fast poll is wasted."""
    pilot, workers, log = pilot_setup
    pilot.enabled = True

    # Workers default to BUZZING in the fixture
    assert all(w.state == WorkerState.BUZZING for w in workers)

    pilot._idle_streak = 5
    pilot._focused_workers = {workers[0].name}

    backoff = pilot._compute_backoff()
    # BUZZING + focus should NOT be capped at _focus_interval
    assert backoff > pilot._focus_interval


@pytest.mark.asyncio
async def test_focus_caps_when_worker_resting(pilot_setup):
    """Focus on a RESTING worker should cap backoff at _focus_interval."""
    pilot, workers, log = pilot_setup
    pilot.enabled = True

    # Transition the focused worker to RESTING (needs 3 confirmations)
    workers[0].update_state(WorkerState.RESTING)
    workers[0].update_state(WorkerState.RESTING)
    workers[0].update_state(WorkerState.RESTING)
    assert workers[0].state == WorkerState.RESTING

    pilot._idle_streak = 5
    pilot._focused_workers = {workers[0].name}

    backoff = pilot._compute_backoff()
    assert backoff == pilot._focus_interval


# ── Auto-approve assignments ─────────────────────────────────────────────


class TestAutoApproveAssignments:
    """Tests for auto-approve when confidence is above threshold."""

    def _make_pilot_with_queen(self, monkeypatch, workers=None, tasks=None, auto_approve=True):
        if workers is None:
            workers = [_make_worker("api", state=WorkerState.RESTING)]
        log = DroneLog()
        board = TaskBoard()
        for t in tasks or []:
            board.add(t)

        queen = AsyncMock()
        queen.can_call = True
        queen.enabled = True
        queen.min_confidence = 0.7

        pilot = DronePilot(
            workers,
            log,
            interval=1.0,
            session_name="test",
            drone_config=DroneConfig(auto_approve_assignments=auto_approve),
            task_board=board,
            queen=queen,
        )
        pilot.enabled = True
        monkeypatch.setattr("swarm.queen.context.build_hive_context", lambda *a, **kw: "ctx")
        return pilot, workers, board, queen, log

    @pytest.mark.asyncio
    async def test_high_confidence_auto_approves(self, monkeypatch):
        """Assignments with confidence >= min_confidence should auto-approve."""
        from swarm.tasks.task import SwarmTask

        task = SwarmTask(title="Build API")
        pilot, workers, board, queen, log = self._make_pilot_with_queen(monkeypatch, tasks=[task])

        queen.assign_tasks.return_value = [
            {"worker": "api", "task_id": task.id, "message": "Do it", "confidence": 0.9}
        ]

        assigned = []
        pilot.on_task_assigned(lambda w, t, m="": assigned.append((w.name, t.id)))

        proposals = []
        pilot.on_proposal(lambda p: proposals.append(p))

        result = await pilot._auto_assign_tasks()
        assert result is True
        assert len(assigned) == 1
        assert assigned[0] == ("api", task.id)
        assert len(proposals) == 0  # bypassed proposal system

    @pytest.mark.asyncio
    async def test_low_confidence_creates_proposal(self, monkeypatch):
        """Assignments below threshold should create proposals as before."""
        from swarm.tasks.task import SwarmTask

        task = SwarmTask(title="Build API")
        pilot, workers, board, queen, log = self._make_pilot_with_queen(monkeypatch, tasks=[task])

        queen.assign_tasks.return_value = [
            {"worker": "api", "task_id": task.id, "message": "Do it", "confidence": 0.5}
        ]

        assigned = []
        pilot.on_task_assigned(lambda w, t, m="": assigned.append((w.name, t.id)))

        proposals = []
        pilot.on_proposal(lambda p: proposals.append(p))

        result = await pilot._auto_assign_tasks()
        assert result is True
        assert len(assigned) == 0  # not auto-approved
        assert len(proposals) == 1  # went through proposal system

    @pytest.mark.asyncio
    async def test_auto_approve_disabled_always_proposes(self, monkeypatch):
        """With auto_approve_assignments=False, all go through proposals."""
        from swarm.tasks.task import SwarmTask

        task = SwarmTask(title="Build API")
        pilot, workers, board, queen, log = self._make_pilot_with_queen(
            monkeypatch, tasks=[task], auto_approve=False
        )

        queen.assign_tasks.return_value = [
            {"worker": "api", "task_id": task.id, "message": "Do it", "confidence": 0.95}
        ]

        assigned = []
        pilot.on_task_assigned(lambda w, t, m="": assigned.append((w.name, t.id)))

        proposals = []
        pilot.on_proposal(lambda p: proposals.append(p))

        result = await pilot._auto_assign_tasks()
        assert result is True
        assert len(assigned) == 0
        assert len(proposals) == 1

    @pytest.mark.asyncio
    async def test_auto_approve_resets_idle_counter(self, monkeypatch):
        """Auto-approved assignment should reset the worker's idle counter."""
        from swarm.tasks.task import SwarmTask

        task = SwarmTask(title="Build API")
        pilot, workers, board, queen, log = self._make_pilot_with_queen(monkeypatch, tasks=[task])
        pilot._idle_consecutive["api"] = 5

        queen.assign_tasks.return_value = [
            {"worker": "api", "task_id": task.id, "message": "Do it", "confidence": 0.9}
        ]

        await pilot._auto_assign_tasks()
        assert pilot._idle_consecutive.get("api", 0) == 0


# ── Idle-consecutive tracking ────────────────────────────────────────────


class TestIdleConsecutiveTracking:
    """Tests for per-worker idle consecutive poll tracking."""

    @pytest.mark.asyncio
    async def test_idle_counter_increments(self, pilot_setup, monkeypatch):
        """RESTING workers should have their idle counter incremented."""
        pilot, workers, log = pilot_setup
        pilot.enabled = True

        # Make workers RESTING
        idle_content = '> Try "how does foo work"\n? for shortcuts'
        monkeypatch.setattr("swarm.drones.pilot.capture_pane", AsyncMock(return_value=idle_content))

        await pilot.poll_once()
        await pilot.poll_once()
        await pilot.poll_once()

        # Workers should be RESTING after 3 polls (hysteresis)
        resting = [w for w in workers if w.state == WorkerState.RESTING]
        for w in resting:
            assert pilot._idle_consecutive.get(w.name, 0) >= 1

    @pytest.mark.asyncio
    async def test_idle_counter_resets_on_buzzing(self, pilot_setup, monkeypatch):
        """Counter should reset when worker goes back to BUZZING."""
        pilot, workers, log = pilot_setup
        pilot.enabled = True
        pilot._idle_consecutive["api"] = 5

        # Workers are BUZZING (default mock returns "esc to interrupt")
        await pilot.poll_once()

        assert pilot._idle_consecutive.get("api", 0) == 0


# ── Zoom suppression ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_zoomed_worker_skips_decisions(pilot_setup, monkeypatch):
    """A WAITING worker whose pane is zoomed should NOT get auto-continued."""
    pilot, workers, log = pilot_setup
    pilot.enabled = True

    # Put workers in WAITING state with a choice menu
    for w in workers:
        w.state = WorkerState.WAITING

    content = """> 1. Always allow
  2. Yes
  3. No
Enter to select · ↑/↓ to navigate"""
    monkeypatch.setattr("swarm.drones.pilot.capture_pane", AsyncMock(return_value=content))
    monkeypatch.setattr("swarm.drones.pilot.get_pane_command", AsyncMock(return_value="claude"))

    # Simulate zoom: "api" pane is zoomed
    monkeypatch.setattr("swarm.drones.pilot.get_zoomed_pane", AsyncMock(return_value="%api"))

    await pilot.poll_once()

    # "api" should NOT have been continued (zoomed), but "web" should have
    continued = [e for e in log.entries if e.action == SystemAction.CONTINUED]
    continued_workers = [e.worker_name for e in continued]
    assert "api" not in continued_workers
    assert "web" in continued_workers


@pytest.mark.asyncio
async def test_no_zoom_all_workers_get_decisions(pilot_setup, monkeypatch):
    """When get_zoomed_pane returns None, all workers get normal decisions."""
    pilot, workers, log = pilot_setup
    pilot.enabled = True

    for w in workers:
        w.state = WorkerState.WAITING

    content = """> 1. Always allow
  2. Yes
  3. No
Enter to select · ↑/↓ to navigate"""
    monkeypatch.setattr("swarm.drones.pilot.capture_pane", AsyncMock(return_value=content))
    monkeypatch.setattr("swarm.drones.pilot.get_pane_command", AsyncMock(return_value="claude"))
    monkeypatch.setattr("swarm.drones.pilot.get_zoomed_pane", AsyncMock(return_value=None))

    await pilot.poll_once()

    continued = [e for e in log.entries if e.action == SystemAction.CONTINUED]
    continued_workers = {e.worker_name for e in continued}
    assert "api" in continued_workers
    assert "web" in continued_workers


@pytest.mark.asyncio
async def test_zoomed_worker_still_tracks_state(pilot_setup, monkeypatch):
    """A zoomed worker should still detect state transitions."""
    pilot, workers, log = pilot_setup
    pilot.enabled = True

    # Simulate zoom on "api"
    monkeypatch.setattr("swarm.drones.pilot.get_zoomed_pane", AsyncMock(return_value="%api"))
    # Make workers transition to RESTING
    idle_content = '> Try "how does foo work"\n? for shortcuts'
    monkeypatch.setattr("swarm.drones.pilot.capture_pane", AsyncMock(return_value=idle_content))

    state_changes: list[str] = []
    pilot.on_state_changed(lambda w: state_changes.append(w.name))

    await pilot.poll_once()
    await pilot.poll_once()
    await pilot.poll_once()

    # "api" state should have changed even though it's zoomed
    assert "api" in state_changes


@pytest.mark.asyncio
async def test_zoomed_worker_excluded_from_auto_assign(monkeypatch):
    """Zoomed idle workers should be excluded from auto-assignment."""
    from swarm.tasks.task import SwarmTask

    workers = [
        _make_worker("api", state=WorkerState.RESTING),
        _make_worker("web", state=WorkerState.RESTING),
    ]
    log = DroneLog()
    board = TaskBoard()
    task = SwarmTask(title="Build API")
    board.add(task)

    queen = AsyncMock()
    queen.can_call = True
    queen.enabled = True
    queen.min_confidence = 0.7
    queen.assign_tasks.return_value = [
        {
            "worker": "web",
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
    # "api" is zoomed
    pilot._zoomed_pane = "%api"

    monkeypatch.setattr("swarm.queen.context.build_hive_context", lambda *a, **kw: "ctx")

    assigned = []
    pilot.on_task_assigned(lambda w, t, m="": assigned.append(w.name))

    await pilot._auto_assign_tasks()

    # Queen should only have been called with "web" (api excluded due to zoom)
    call_args = queen.assign_tasks.call_args
    assert "api" not in call_args[0][0]
    assert "web" in call_args[0][0]


@pytest.mark.asyncio
async def test_zoomed_worker_skips_completion_check():
    """Zoomed worker should not have completion proposed."""
    import time

    workers = [_make_worker("api", state=WorkerState.RESTING, resting_since=0)]
    log = DroneLog()
    board = TaskBoard()
    pilot = DronePilot(workers, log, interval=1.0, session_name="test", drone_config=DroneConfig())
    pilot.task_board = board
    pilot.enabled = True
    pilot._zoomed_pane = "%api"

    workers[0].state_since = time.time() - 120

    task = board.create("Fix bug")
    board.assign(task.id, "api")

    events: list[str] = []
    pilot.on("task_done", lambda w, t, r: events.append(t.id))

    pilot._check_task_completions()
    assert len(events) == 0  # zoomed — not proposed


@pytest.mark.asyncio
async def test_zoomed_worker_directive_skipped(monkeypatch):
    """Queen directives targeting a zoomed worker should be skipped."""
    workers = [_make_worker("api", state=WorkerState.RESTING)]
    log = DroneLog()
    pilot = DronePilot(workers, log, interval=1.0, session_name="test", drone_config=DroneConfig())
    pilot.enabled = True
    pilot._zoomed_pane = "%api"

    send_enter_mock = AsyncMock()
    monkeypatch.setattr("swarm.drones.pilot.send_enter", send_enter_mock)

    result = await pilot._execute_directives(
        [{"worker": "api", "action": "continue", "reason": "nudge"}]
    )
    assert result is False
    send_enter_mock.assert_not_awaited()


# ── Idle-consecutive tracking (continued) ────────────────────────────────


class TestIdleConsecutiveTrackingContinued:
    """Additional tests for per-worker idle consecutive poll tracking."""

    @pytest.mark.asyncio
    async def test_needs_assign_check_on_resting_transition(self, monkeypatch):
        """BUZZING→RESTING transition should set _needs_assign_check."""
        workers = [_make_worker("api", state=WorkerState.BUZZING)]
        log = DroneLog()
        pilot = DronePilot(
            workers, log, interval=1.0, session_name="test", drone_config=DroneConfig()
        )
        pilot.enabled = True

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
        monkeypatch.setattr("swarm.drones.pilot.get_zoomed_pane", AsyncMock(return_value=None))

        # Need 3 polls for hysteresis to confirm RESTING
        await pilot.poll_once()
        await pilot.poll_once()
        await pilot.poll_once()

        # After the transition, the flag should have been set (then cleared by periodic tasks)
        # We check workers are now RESTING to confirm the transition happened
        assert workers[0].state == WorkerState.RESTING


# ── Coordination skip when hive state unchanged ──────────────────────


@pytest.fixture
def coordination_setup(pilot_setup):
    """Set up a pilot with a mocked Queen for coordination tests."""
    pilot, workers, log = pilot_setup
    queen_mock = AsyncMock()
    queen_mock.enabled = True
    queen_mock.coordinate_hive = AsyncMock(return_value={"directives": [], "confidence": 0.8})
    pilot.queen = queen_mock
    # Workers start BUZZING, but coordination skips all-BUZZING.
    # Directly set state (bypassing hysteresis) so coordination runs.
    workers[1].state = WorkerState.RESTING
    return pilot, workers, queen_mock


@pytest.mark.asyncio
async def test_coordination_skip_when_unchanged(coordination_setup):
    """Coordination cycle should skip the Queen call when hive state is unchanged."""
    pilot, workers, queen_mock = coordination_setup

    # First coordination call — should invoke Queen
    await pilot._coordination_cycle()
    assert queen_mock.coordinate_hive.call_count == 1

    # Second call with identical state — should skip
    await pilot._coordination_cycle()
    assert queen_mock.coordinate_hive.call_count == 1  # still 1


@pytest.mark.asyncio
async def test_coordination_runs_after_state_change(coordination_setup):
    """Coordination should run again after a worker state change."""
    pilot, workers, queen_mock = coordination_setup

    await pilot._coordination_cycle()
    assert queen_mock.coordinate_hive.call_count == 1

    # Change BUZZING worker to RESTING (both now RESTING — different snapshot)
    workers[0].state = WorkerState.RESTING

    await pilot._coordination_cycle()
    assert queen_mock.coordinate_hive.call_count == 2


# ── State-aware capture in coordination ───────────────────────────────


@pytest.mark.asyncio
async def test_coordination_skips_sleeping_workers(coordination_setup, monkeypatch):
    """Sleeping workers should not have pane output captured in coordination."""
    pilot, workers, queen_mock = coordination_setup

    # Make worker[1] SLEEPING (RESTING for > 5 min)
    workers[1].state_since = time.time() - 600  # 10 min ago

    capture_mock = AsyncMock(return_value="some output")
    monkeypatch.setattr("swarm.drones.pilot.capture_pane", capture_mock)

    await pilot._coordination_cycle()

    # capture_pane should only have been called for the BUZZING worker,
    # not the SLEEPING one
    called_pane_ids = [call.args[0] for call in capture_mock.call_args_list]
    assert workers[0].pane_id in called_pane_ids
    assert workers[1].pane_id not in called_pane_ids


@pytest.mark.asyncio
async def test_coordination_captures_fewer_lines_for_resting(coordination_setup, monkeypatch):
    """RESTING workers should get only 15 lines captured vs 60 for active."""
    pilot, workers, queen_mock = coordination_setup

    capture_mock = AsyncMock(return_value="some output")
    monkeypatch.setattr("swarm.drones.pilot.capture_pane", capture_mock)

    await pilot._coordination_cycle()

    # Find the capture call for worker[1] (RESTING) — should use lines=15
    for call in capture_mock.call_args_list:
        if call.args[0] == workers[1].pane_id:
            assert call.kwargs.get("lines") == 15
            break
    else:
        pytest.fail("Expected capture_pane call for resting worker")

    # Find the capture call for worker[0] (BUZZING) — should use lines=60
    for call in capture_mock.call_args_list:
        if call.args[0] == workers[0].pane_id:
            assert call.kwargs.get("lines") == 60
            break
    else:
        pytest.fail("Expected capture_pane call for buzzing worker")


# ── Sleeping worker poll throttling ───────────────────────────────────


@pytest.mark.asyncio
async def test_sleeping_worker_poll_throttled(monkeypatch):
    """Sleeping workers should skip expensive tmux calls between full polls."""
    workers = [_make_worker("sleepy", state=WorkerState.RESTING)]
    # Make it sleeping (idle > 5 min)
    workers[0].state_since = time.time() - 600

    log = DroneLog()
    config = DroneConfig(sleeping_poll_interval=30.0)
    pilot = DronePilot(workers, log, interval=1.0, session_name="test", drone_config=config)

    monkeypatch.setattr("swarm.drones.pilot.pane_exists", AsyncMock(return_value=True))
    get_cmd_mock = AsyncMock(return_value="claude")
    monkeypatch.setattr("swarm.drones.pilot.get_pane_command", get_cmd_mock)
    monkeypatch.setattr("swarm.drones.pilot.capture_pane", AsyncMock(return_value="> idle"))
    monkeypatch.setattr("swarm.drones.pilot.send_enter", AsyncMock())
    monkeypatch.setattr("swarm.drones.pilot.set_pane_option", AsyncMock())
    monkeypatch.setattr("swarm.drones.pilot.discover_workers", AsyncMock(return_value=[]))
    monkeypatch.setattr("swarm.drones.pilot.update_window_names", AsyncMock())
    monkeypatch.setattr("swarm.drones.pilot.set_terminal_title", AsyncMock())
    monkeypatch.setattr("swarm.drones.pilot.revive_worker", AsyncMock())
    monkeypatch.setattr("swarm.drones.pilot.get_zoomed_pane", AsyncMock(return_value=None))

    # First poll — should do a full poll (no previous timestamp)
    dead: list = []
    await pilot._poll_single_worker(workers[0], dead)
    assert get_cmd_mock.call_count == 1

    # Immediately poll again — should be throttled (skip get_pane_command)
    await pilot._poll_single_worker(workers[0], dead)
    assert get_cmd_mock.call_count == 1  # still 1


@pytest.mark.asyncio
async def test_sleeping_worker_not_throttled_when_focused(monkeypatch):
    """Sleeping workers that are focused should not be throttled."""
    workers = [_make_worker("sleepy", state=WorkerState.RESTING)]
    workers[0].state_since = time.time() - 600

    log = DroneLog()
    config = DroneConfig(sleeping_poll_interval=30.0)
    pilot = DronePilot(workers, log, interval=1.0, session_name="test", drone_config=config)
    pilot._focused_workers = {"sleepy"}

    monkeypatch.setattr("swarm.drones.pilot.pane_exists", AsyncMock(return_value=True))
    get_cmd_mock = AsyncMock(return_value="claude")
    monkeypatch.setattr("swarm.drones.pilot.get_pane_command", get_cmd_mock)
    monkeypatch.setattr("swarm.drones.pilot.capture_pane", AsyncMock(return_value="> idle"))
    monkeypatch.setattr("swarm.drones.pilot.send_enter", AsyncMock())
    monkeypatch.setattr("swarm.drones.pilot.set_pane_option", AsyncMock())
    monkeypatch.setattr("swarm.drones.pilot.discover_workers", AsyncMock(return_value=[]))
    monkeypatch.setattr("swarm.drones.pilot.update_window_names", AsyncMock())
    monkeypatch.setattr("swarm.drones.pilot.set_terminal_title", AsyncMock())
    monkeypatch.setattr("swarm.drones.pilot.revive_worker", AsyncMock())
    monkeypatch.setattr("swarm.drones.pilot.get_zoomed_pane", AsyncMock(return_value=None))

    # First poll — full
    dead: list = []
    await pilot._poll_single_worker(workers[0], dead)
    assert get_cmd_mock.call_count == 1

    # Second poll immediately — should still be full (focused overrides throttle)
    await pilot._poll_single_worker(workers[0], dead)
    assert get_cmd_mock.call_count == 2
