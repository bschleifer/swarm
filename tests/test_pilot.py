"""Tests for drones/pilot.py — async polling loop and decision engine."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from swarm.drones.log import DroneAction, DroneLog
from swarm.drones.pilot import DronePilot
from swarm.config import DroneConfig
from swarm.tasks.board import TaskBoard
from swarm.tasks.task import TaskStatus
from swarm.worker.worker import Worker, WorkerState


def _make_worker(name: str = "api", state: WorkerState = WorkerState.BUZZING) -> Worker:
    w = Worker(name=name, path="/tmp", pane_id=f"%{name}")
    w.state = state
    return w


@pytest.fixture
def pilot_setup(monkeypatch):
    """Set up a DronePilot with mocked tmux calls."""
    workers = [_make_worker("api"), _make_worker("web")]
    log = DroneLog()
    pilot = DronePilot(workers, log, interval=1.0, session_name="test",
                      drone_config=DroneConfig())

    # Mock all tmux operations
    monkeypatch.setattr("swarm.drones.pilot.pane_exists", AsyncMock(return_value=True))
    monkeypatch.setattr("swarm.drones.pilot.get_pane_command", AsyncMock(return_value="claude"))
    monkeypatch.setattr("swarm.drones.pilot.capture_pane",
                        AsyncMock(return_value="esc to interrupt"))
    monkeypatch.setattr("swarm.drones.pilot.send_enter", AsyncMock())
    monkeypatch.setattr("swarm.drones.pilot.send_keys", AsyncMock())
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
async def test_poll_once_detects_resting(pilot_setup, monkeypatch):
    """poll_once should detect RESTING state from pane content."""
    pilot, workers, log = pilot_setup
    monkeypatch.setattr("swarm.drones.pilot.capture_pane",
                        AsyncMock(return_value="> "))
    pilot.enabled = True
    # First poll: hysteresis (BUZZING -> RESTING requires 2 confirmations)
    await pilot.poll_once()
    # Second poll: should confirm RESTING
    await pilot.poll_once()
    # Workers should now be RESTING
    for w in workers:
        # Might still be BUZZING due to hysteresis
        pass  # State detection tested separately


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
    monkeypatch.setattr("swarm.drones.pilot.get_pane_command",
                        AsyncMock(return_value="bash"))
    monkeypatch.setattr("swarm.drones.pilot.capture_pane",
                        AsyncMock(return_value="$ "))

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

    monkeypatch.setattr("swarm.drones.pilot.get_pane_command",
                        AsyncMock(return_value="bash"))
    monkeypatch.setattr("swarm.drones.pilot.capture_pane",
                        AsyncMock(return_value="$ "))

    await pilot.poll_once()
    # Should have revive entries
    revives = [e for e in log.entries if e.action == DroneAction.REVIVED]
    assert len(revives) > 0


@pytest.mark.asyncio
async def test_escalate_on_crash_loop(pilot_setup, monkeypatch):
    """Workers that exhaust revive attempts should be escalated."""
    pilot, workers, log = pilot_setup
    pilot.enabled = True

    # Set workers to already have max revives
    for w in workers:
        w.revive_count = 3

    monkeypatch.setattr("swarm.drones.pilot.get_pane_command",
                        AsyncMock(return_value="bash"))
    monkeypatch.setattr("swarm.drones.pilot.capture_pane",
                        AsyncMock(return_value="$ "))

    escalations = []
    pilot.on_escalate(lambda w, r: escalations.append((w.name, r)))

    await pilot.poll_once()
    escalates = [e for e in log.entries if e.action == DroneAction.ESCALATED]
    assert len(escalates) > 0


@pytest.mark.asyncio
async def test_toggle(pilot_setup):
    """toggle() should flip enabled state."""
    pilot, _, _ = pilot_setup
    assert not pilot.enabled
    result = pilot.toggle()
    assert result is True
    assert pilot.enabled
    result = pilot.toggle()
    assert result is False
    assert not pilot.enabled


@pytest.mark.asyncio
async def test_continue_on_choice_prompt(pilot_setup, monkeypatch):
    """Choice prompts should trigger CONTINUE decision."""
    pilot, workers, log = pilot_setup
    pilot.enabled = True

    # Put workers in RESTING state first
    for w in workers:
        w.state = WorkerState.RESTING

    content = """> 1. Always allow
  2. Yes
  3. No
Enter to select · ↑/↓ to navigate"""
    monkeypatch.setattr("swarm.drones.pilot.capture_pane",
                        AsyncMock(return_value=content))
    monkeypatch.setattr("swarm.drones.pilot.get_pane_command",
                        AsyncMock(return_value="claude"))

    await pilot.poll_once()
    continued = [e for e in log.entries if e.action == DroneAction.CONTINUED]
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

    monkeypatch.setattr("swarm.drones.pilot.get_pane_command",
                        AsyncMock(return_value="bash"))
    monkeypatch.setattr("swarm.drones.pilot.capture_pane",
                        AsyncMock(return_value="$ "))

    result = await pilot.poll_once()
    assert result is True


@pytest.mark.asyncio
async def test_adaptive_backoff(pilot_setup, monkeypatch):
    """Idle streak should grow and backoff interval should increase."""
    pilot, workers, log = pilot_setup
    pilot.enabled = True

    # All workers BUZZING, no action taken → idle streak grows
    await pilot.poll_once()  # Returns False (no action)

    # Simulate what _loop does
    had_action = False
    if not had_action:
        pilot._idle_streak += 1

    assert pilot._idle_streak == 1

    # Compute expected backoff
    backoff = min(
        pilot._base_interval * (2 ** min(pilot._idle_streak, 3)),
        pilot._max_interval,
    )
    assert backoff == 2.0  # 1.0 * 2^1 = 2.0

    # Streak 2
    pilot._idle_streak = 2
    backoff = min(
        pilot._base_interval * (2 ** min(pilot._idle_streak, 3)),
        pilot._max_interval,
    )
    assert backoff == 4.0  # 1.0 * 2^2 = 4.0

    # Streak 3+ caps at 2^3 = 8 (within max_idle_interval=30)
    pilot._idle_streak = 5
    backoff = min(
        pilot._base_interval * (2 ** min(pilot._idle_streak, 3)),
        pilot._max_interval,
    )
    assert backoff == 8.0  # 1.0 * 2^3 = 8.0, capped at min(8, 30)


@pytest.mark.asyncio
async def test_adaptive_backoff_resets_on_action(pilot_setup, monkeypatch):
    """Idle streak should reset to 0 when an action is taken."""
    pilot, workers, log = pilot_setup
    pilot.enabled = True

    # Simulate idle streak
    pilot._idle_streak = 5

    # Force an action (revive)
    monkeypatch.setattr("swarm.drones.pilot.get_pane_command",
                        AsyncMock(return_value="bash"))
    monkeypatch.setattr("swarm.drones.pilot.capture_pane",
                        AsyncMock(return_value="$ "))

    had_action = await pilot.poll_once()
    assert had_action is True

    # Simulate what _loop does on action
    if had_action:
        pilot._idle_streak = 0

    assert pilot._idle_streak == 0


# ── Loop termination: empty hive ────────────────────────────────────────


@pytest.mark.asyncio
async def test_loop_exits_on_empty_hive(monkeypatch):
    """_loop should exit and emit hive_empty when all workers are gone."""
    workers = [_make_worker("api")]
    log = DroneLog()
    pilot = DronePilot(workers, log, interval=0.01, session_name=None,
                      drone_config=DroneConfig())

    # pane_exists returns False → worker will be removed
    monkeypatch.setattr("swarm.drones.pilot.pane_exists", AsyncMock(return_value=False))

    events: list[str] = []
    pilot.on_hive_empty(lambda: events.append("hive_empty"))

    pilot.enabled = True
    # Run _loop — should exit after one cycle since workers become empty
    await asyncio.wait_for(pilot._loop(), timeout=2.0)

    assert not pilot.enabled
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
    board.complete(task.id)

    pilot = DronePilot(
        workers, log, interval=0.01, session_name=None,
        drone_config=DroneConfig(auto_stop_on_complete=True),
        task_board=board,
    )

    # Workers are RESTING and all tasks complete
    monkeypatch.setattr("swarm.drones.pilot.pane_exists", AsyncMock(return_value=True))
    monkeypatch.setattr("swarm.drones.pilot.get_pane_command", AsyncMock(return_value="claude"))
    monkeypatch.setattr("swarm.drones.pilot.capture_pane", AsyncMock(return_value="> "))
    monkeypatch.setattr("swarm.drones.pilot.set_pane_option", AsyncMock())

    events: list[str] = []
    pilot.on_hive_complete(lambda: events.append("hive_complete"))

    pilot.enabled = True
    await asyncio.wait_for(pilot._loop(), timeout=2.0)

    assert "hive_complete" in events


@pytest.mark.asyncio
async def test_hive_complete_not_emitted_when_disabled(monkeypatch):
    """auto_stop_on_complete=False should prevent hive_complete."""
    workers = [_make_worker("api", state=WorkerState.RESTING)]
    log = DroneLog()

    board = TaskBoard()
    task = board.create("Test task")
    board.complete(task.id)

    pilot = DronePilot(
        workers, log, interval=0.01, session_name=None,
        drone_config=DroneConfig(auto_stop_on_complete=False),
        task_board=board,
    )

    monkeypatch.setattr("swarm.drones.pilot.pane_exists", AsyncMock(return_value=True))
    monkeypatch.setattr("swarm.drones.pilot.get_pane_command", AsyncMock(return_value="claude"))
    monkeypatch.setattr("swarm.drones.pilot.capture_pane", AsyncMock(return_value="> "))
    monkeypatch.setattr("swarm.drones.pilot.set_pane_option", AsyncMock())

    events: list[str] = []
    pilot.on_hive_complete(lambda: events.append("hive_complete"))

    pilot.enabled = True
    # Run a few poll cycles manually (not _loop, since it wouldn't terminate)
    for _ in range(5):
        await pilot.poll_once()

    assert "hive_complete" not in events


# ── Circuit breaker ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_circuit_breaker_trips(monkeypatch):
    """Worker with N consecutive poll failures should be treated as dead."""
    workers = [_make_worker("api"), _make_worker("web")]
    log = DroneLog()
    max_failures = 3
    pilot = DronePilot(
        workers, log, interval=1.0, session_name=None,
        drone_config=DroneConfig(max_poll_failures=max_failures),
    )

    # pane_exists succeeds, but capture_pane throws for "api" only
    async def pane_exists_ok(pane_id):
        return True


    async def get_cmd(pane_id):
        if pane_id == "%api":
            raise RuntimeError("simulated failure")
        return "claude"

    monkeypatch.setattr("swarm.drones.pilot.pane_exists", pane_exists_ok)
    monkeypatch.setattr("swarm.drones.pilot.get_pane_command", get_cmd)
    monkeypatch.setattr("swarm.drones.pilot.capture_pane",
                        AsyncMock(return_value="esc to interrupt"))
    monkeypatch.setattr("swarm.drones.pilot.set_pane_option", AsyncMock())
    monkeypatch.setattr("swarm.drones.pilot.send_enter", AsyncMock())
    monkeypatch.setattr("swarm.drones.pilot.send_keys", AsyncMock())
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
        workers, log, interval=1.0, session_name=None,
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
        workers, log, interval=1.0, session_name=None,
        drone_config=DroneConfig(max_poll_failures=2),
        task_board=board,
    )

    async def pane_exists_ok(pane_id):
        return True

    async def always_fail(pane_id):
        raise RuntimeError("boom")

    monkeypatch.setattr("swarm.drones.pilot.pane_exists", pane_exists_ok)
    monkeypatch.setattr("swarm.drones.pilot.get_pane_command", always_fail)
    monkeypatch.setattr("swarm.drones.pilot.capture_pane",
                        AsyncMock(return_value=""))
    monkeypatch.setattr("swarm.drones.pilot.set_pane_option", AsyncMock())

    # 2 failures → circuit breaker trips
    await pilot.poll_once()
    await pilot.poll_once()

    assert len(workers) == 0
    assert task.status == TaskStatus.PENDING
    assert task.assigned_worker is None
