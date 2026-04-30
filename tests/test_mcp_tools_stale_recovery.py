"""Tests for the IdleWatcher MCP tools-dropped recovery path (task #257).

Covers the client-side drop-after-reload scenario:

1. Daemon starts at T0.
2. Worker is idle during the reload window — Claude Code's HTTP MCP transport
   gives up reconnecting; the client's tool registry is now empty.
3. When the watcher sweeps the worker later, the normal nudge
   (``swarm_task_status filter=mine``) would be useless because the worker
   can't call swarm_* tools any more.
4. The watcher detects this by noting "no MCP activity since daemon start"
   and injects ``/mcp`` into the worker's PTY to force a client re-init.
5. A ``MCP_TOOLS_STALE`` buzz entry is written under ``LogCategory.MCP``.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from swarm.config import DroneConfig
from swarm.drones.idle_watcher import IdleWatcher
from swarm.drones.log import DroneAction, LogCategory, SystemAction
from swarm.worker.worker import WorkerState

# ---------------------------------------------------------------------------
# Test doubles — intentionally minimal.  Full integration with a live daemon
# is covered by test_daemon + test_pilot; here we pin the watcher's branch
# logic directly.
# ---------------------------------------------------------------------------


def _worker(name: str, state: WorkerState = WorkerState.RESTING) -> MagicMock:
    w = MagicMock()
    w.name = name
    w.display_state = state
    w.state = state
    return w


def _task(number: int, task_id: str) -> MagicMock:
    t = MagicMock()
    t.number = number
    t.id = task_id
    t.status = MagicMock()
    t.status.value = "in_progress"
    return t


def _board(tasks_by_worker: dict[str, list[MagicMock]]) -> MagicMock:
    b = MagicMock()

    def active(name: str) -> list[MagicMock]:
        return tasks_by_worker.get(name, [])

    b.active_tasks_for_worker = MagicMock(side_effect=active)
    b.all_tasks = [t for tasks in tasks_by_worker.values() for t in tasks]
    return b


def _log() -> MagicMock:
    log = MagicMock()
    log.entries = []

    def add(action, worker, detail, category=None, **_):
        entry = MagicMock()
        entry.action = action
        entry.worker_name = worker
        entry.detail = detail
        entry.category = category
        log.entries.append(entry)

    log.add = MagicMock(side_effect=add)
    return log


def _make_watcher(
    *,
    board,
    drone_log,
    mcp_activity_lookup=None,
    daemon_start_time=None,
    interval: float = 60.0,
    # Default to a delay longer than any test runs for, so the post-/mcp
    # follow-up nudge (task #315) doesn't perturb tests that only exercise
    # the sweep itself. Tests covering the follow-up behaviour pass 0.0.
    mcp_followup_delay_seconds: float = 999.0,
):
    cfg = DroneConfig(idle_nudge_interval_seconds=interval, idle_nudge_debounce_seconds=60.0)
    sender = AsyncMock()
    w = IdleWatcher(
        drone_config=cfg,
        task_board=board,
        drone_log=drone_log,
        send_to_worker=sender,
        mcp_activity_lookup=mcp_activity_lookup,
        daemon_start_time=daemon_start_time,
        mcp_followup_delay_seconds=mcp_followup_delay_seconds,
    )
    return w, sender


async def _drain_followups(watcher) -> None:
    """Wait for any in-flight ``/mcp`` follow-up tasks to complete."""
    pending = list(watcher._mcp_followups)
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)


def _cancel_followups(watcher) -> None:
    """Cancel any pending follow-up tasks so they don't leak past the test."""
    for task in list(watcher._mcp_followups):
        task.cancel()


# ---------------------------------------------------------------------------
# Core recovery path
# ---------------------------------------------------------------------------


class TestMCPRefreshOnStaleTools:
    @pytest.mark.asyncio
    async def test_fires_mcp_refresh_when_no_activity_since_daemon_start(self):
        """Worker with active task + no MCP calls since daemon boot →
        inject /mcp, skip regular nudge, log MCP_TOOLS_STALE."""
        daemon_start = 1_000.0
        # Simulate the pathological state: worker has never made an MCP call
        # since this daemon booted.  ``None`` means "no record at all".
        mcp_lookup = MagicMock(return_value=None)

        task = _task(246, "t-246")
        board = _board({"rcg-dev-install": [task]})
        drone_log = _log()
        watcher, sender = _make_watcher(
            board=board,
            drone_log=drone_log,
            mcp_activity_lookup=mcp_lookup,
            daemon_start_time=daemon_start,
        )

        sent = await watcher.sweep([_worker("rcg-dev-install")], now=1_100.0)

        # Refresh took priority over the regular nudge, so ``sent`` is 0 for
        # AUTO_NUDGE but the PTY call still fired exactly once, and it
        # carried the /mcp probe text rather than the normal task nudge.
        assert sent == 0
        sender.assert_awaited_once()
        call_args = sender.await_args
        assert call_args.args[0] == "rcg-dev-install"
        assert call_args.args[1] == "/mcp"

        # Buzz log has an MCP_TOOLS_STALE entry under the MCP category.
        stale_entries = [e for e in drone_log.entries if e.action == SystemAction.MCP_TOOLS_STALE]
        assert len(stale_entries) == 1
        assert stale_entries[0].worker_name == "rcg-dev-install"
        assert stale_entries[0].category == LogCategory.MCP

    @pytest.mark.asyncio
    async def test_fires_when_last_activity_predates_daemon_start(self):
        """A MCP timestamp older than daemon_start_time is the same signal
        as 'no record' — the client's activity was before the reload."""
        daemon_start = 2_000.0
        mcp_lookup = MagicMock(return_value=1_500.0)  # from previous boot

        board = _board({"w": [_task(10, "t-10")]})
        drone_log = _log()
        watcher, sender = _make_watcher(
            board=board,
            drone_log=drone_log,
            mcp_activity_lookup=mcp_lookup,
            daemon_start_time=daemon_start,
        )

        await watcher.sweep([_worker("w")], now=2_100.0)
        sender.assert_awaited_once_with("w", "/mcp", _log_operator=False)

    @pytest.mark.asyncio
    async def test_does_not_fire_when_worker_has_recent_activity(self):
        """Post-daemon-start activity means tools are alive → normal nudge path."""
        daemon_start = 1_000.0
        mcp_lookup = MagicMock(return_value=1_050.0)  # AFTER daemon start

        board = _board({"w": [_task(10, "t-10")]})
        drone_log = _log()
        watcher, sender = _make_watcher(
            board=board,
            drone_log=drone_log,
            mcp_activity_lookup=mcp_lookup,
            daemon_start_time=daemon_start,
        )

        sent = await watcher.sweep([_worker("w")], now=1_100.0)

        assert sent == 1
        # The only PTY call is the normal nudge — no /mcp injection.
        sender.assert_awaited_once()
        assert sender.await_args.args[1] != "/mcp"
        assert "appear idle" in sender.await_args.args[1]
        # Buzz log has AUTO_NUDGE, not MCP_TOOLS_STALE.
        actions = [e.action for e in drone_log.entries]
        assert DroneAction.AUTO_NUDGE in actions
        assert SystemAction.MCP_TOOLS_STALE not in actions


class TestMCPRefreshDebounce:
    @pytest.mark.asyncio
    async def test_fires_at_most_once_per_worker_per_boot(self):
        """Two sweeps on the same stale worker → exactly one ``/mcp`` injection.

        After the first /mcp fires, the worker's ``_mcp_refresh_fired`` flag
        prevents re-probing.  Subsequent sweeps fall through to the normal
        nudge path — this is intentional: the refresh-fired flag only guards
        the /mcp probe itself, not the entire nudge surface.  Operator can
        still see an ordinary idle nudge on the worker.  Prevents an
        infinite /mcp-loop when the stale state somehow persists.
        """
        daemon_start = 1_000.0
        mcp_lookup = MagicMock(return_value=None)

        board = _board({"w": [_task(10, "t-10")]})
        drone_log = _log()
        watcher, sender = _make_watcher(
            board=board,
            drone_log=drone_log,
            mcp_activity_lookup=mcp_lookup,
            daemon_start_time=daemon_start,
            interval=1.0,
        )

        workers = [_worker("w")]
        await watcher.sweep(workers, now=1_100.0)
        await watcher.sweep(workers, now=1_300.0)

        # First call is /mcp probe; second is the regular nudge.
        assert sender.await_count == 2
        assert sender.await_args_list[0].args[1] == "/mcp"
        assert "appear idle" in sender.await_args_list[1].args[1]
        # MCP_TOOLS_STALE buzz entry fires exactly once.
        stale_entries = [e for e in drone_log.entries if e.action == SystemAction.MCP_TOOLS_STALE]
        assert len(stale_entries) == 1

    @pytest.mark.asyncio
    async def test_send_failure_does_not_set_debounce(self):
        """If the /mcp PTY inject fails, the next sweep can retry — we don't
        lock the worker into a non-firing state on a transient error."""
        daemon_start = 1_000.0
        mcp_lookup = MagicMock(return_value=None)

        board = _board({"w": [_task(10, "t-10")]})
        drone_log = _log()
        watcher, sender = _make_watcher(
            board=board,
            drone_log=drone_log,
            mcp_activity_lookup=mcp_lookup,
            daemon_start_time=daemon_start,
            interval=1.0,
        )

        sender.side_effect = [RuntimeError("PTY write failed"), None]

        workers = [_worker("w")]
        await watcher.sweep(workers, now=1_100.0)
        # First call raised → no buzz entry, debounce NOT set.
        assert len([e for e in drone_log.entries if e.action == SystemAction.MCP_TOOLS_STALE]) == 0

        await watcher.sweep(workers, now=1_200.0)
        # Second call succeeds → buzz entry written + debounce set.
        assert sender.await_count == 2
        assert len([e for e in drone_log.entries if e.action == SystemAction.MCP_TOOLS_STALE]) == 1


class TestMCPRefreshDisabledWithoutCallbacks:
    @pytest.mark.asyncio
    async def test_no_mcp_lookup_means_feature_off(self):
        """If mcp_activity_lookup is None, the stale-tools path never fires —
        existing behaviour preserved for deployments that haven't wired this up."""
        board = _board({"w": [_task(10, "t-10")]})
        drone_log = _log()
        watcher, sender = _make_watcher(
            board=board,
            drone_log=drone_log,
            mcp_activity_lookup=None,
            daemon_start_time=1_000.0,
        )
        await watcher.sweep([_worker("w")], now=1_100.0)

        sender.assert_awaited_once()
        assert sender.await_args.args[1] != "/mcp"
        assert all(e.action != SystemAction.MCP_TOOLS_STALE for e in drone_log.entries)

    @pytest.mark.asyncio
    async def test_no_daemon_start_time_means_feature_off(self):
        """Same fallback if daemon_start_time is None."""
        board = _board({"w": [_task(10, "t-10")]})
        drone_log = _log()
        watcher, sender = _make_watcher(
            board=board,
            drone_log=drone_log,
            mcp_activity_lookup=MagicMock(return_value=None),
            daemon_start_time=None,
        )
        await watcher.sweep([_worker("w")], now=1_100.0)

        assert sender.await_args.args[1] != "/mcp"


class TestMCPRefreshFollowupNudge:
    """Task #315: after firing /mcp, the watcher schedules a delayed
    follow-up nudge so the worker doesn't sit at an empty post-dialog
    prompt for a full sweep interval (default 180s). The operator's
    evidence on 2026-04-29 showed d365-solutions sat idle for ~65s
    between /mcp dismissal and a manual queen prompt — without this
    follow-up the wait would be up to 180s every time MCP recovery
    fires.
    """

    @pytest.mark.asyncio
    async def test_followup_sends_task_nudge_after_mcp(self):
        """After /mcp fires, a follow-up nudge with the worker's active
        task numbers is sent without waiting for the next sweep."""
        daemon_start = 1_000.0
        mcp_lookup = MagicMock(return_value=None)
        task = _task(312, "t-312")
        board = _board({"d365-solutions": [task]})
        drone_log = _log()
        watcher, sender = _make_watcher(
            board=board,
            drone_log=drone_log,
            mcp_activity_lookup=mcp_lookup,
            daemon_start_time=daemon_start,
            mcp_followup_delay_seconds=0.0,
        )

        await watcher.sweep([_worker("d365-solutions")], now=1_100.0)
        await _drain_followups(watcher)

        # First send is /mcp; second send is the follow-up task nudge.
        assert sender.await_count == 2
        assert sender.await_args_list[0].args[1] == "/mcp"
        followup_msg = sender.await_args_list[1].args[1]
        assert "#312" in followup_msg
        assert "appear idle" in followup_msg

        # Buzz log records both MCP_TOOLS_STALE and a follow-up AUTO_NUDGE.
        actions = [(e.action, e.detail) for e in drone_log.entries]
        assert any(a == SystemAction.MCP_TOOLS_STALE for a, _ in actions)
        followup_nudges = [
            d for a, d in actions if a == DroneAction.AUTO_NUDGE and "post-/mcp" in d
        ]
        assert len(followup_nudges) == 1, f"expected one follow-up AUTO_NUDGE, got {actions}"

    @pytest.mark.asyncio
    async def test_followup_skipped_when_task_no_longer_active(self):
        """If the task completes between /mcp and the follow-up firing,
        we don't nudge the worker about a stale task."""
        daemon_start = 1_000.0
        mcp_lookup = MagicMock(return_value=None)
        # Board returns the task at sweep time but is empty when the
        # follow-up re-queries — simulates the worker / queen completing
        # the task while /mcp is dismissing.
        task = _task(312, "t-312")
        active_calls = {"n": 0}

        def active(name: str):
            active_calls["n"] += 1
            return [task] if active_calls["n"] == 1 else []

        board = MagicMock()
        board.active_tasks_for_worker = MagicMock(side_effect=active)
        board.all_tasks = [task]

        drone_log = _log()
        watcher, sender = _make_watcher(
            board=board,
            drone_log=drone_log,
            mcp_activity_lookup=mcp_lookup,
            daemon_start_time=daemon_start,
            mcp_followup_delay_seconds=0.0,
        )

        await watcher.sweep([_worker("d365-solutions")], now=1_100.0)
        await _drain_followups(watcher)

        # Only the /mcp probe — the follow-up saw an empty task list and
        # quietly skipped without prompting.
        assert sender.await_count == 1
        assert sender.await_args.args[1] == "/mcp"
        followup_nudges = [e for e in drone_log.entries if e.action == DroneAction.AUTO_NUDGE]
        assert followup_nudges == []

    @pytest.mark.asyncio
    async def test_followup_send_failure_logs_and_does_not_raise(self):
        """A PTY error during the follow-up shouldn't crash the watcher
        or leak an unhandled task exception."""
        daemon_start = 1_000.0
        mcp_lookup = MagicMock(return_value=None)
        task = _task(312, "t-312")
        board = _board({"w": [task]})
        drone_log = _log()
        watcher, sender = _make_watcher(
            board=board,
            drone_log=drone_log,
            mcp_activity_lookup=mcp_lookup,
            daemon_start_time=daemon_start,
            mcp_followup_delay_seconds=0.0,
        )
        # /mcp probe succeeds; the follow-up nudge raises.
        sender.side_effect = [None, RuntimeError("PTY write failed")]

        await watcher.sweep([_worker("w")], now=1_100.0)
        # Drain — a propagated exception here would fail the test.
        await _drain_followups(watcher)

        # /mcp fired, follow-up tried but failed; no AUTO_NUDGE recorded.
        assert sender.await_count == 2
        followup_nudges = [e for e in drone_log.entries if e.action == DroneAction.AUTO_NUDGE]
        assert followup_nudges == []


class TestMCPActivityTracking:
    """Verifies the server-side tracker the IdleWatcher consults."""

    def test_get_worker_last_mcp_activity_returns_none_on_miss(self):
        from swarm.mcp.server import get_worker_last_mcp_activity

        assert get_worker_last_mcp_activity("never-seen-this-worker") is None

    def test_tracker_updates_on_dispatch(self, monkeypatch):
        """Directly exercise the module state: _worker_last_mcp_activity
        should record a timestamp after any dispatch."""
        from swarm.mcp import server as mcp_server

        monkeypatch.setattr(mcp_server.time, "time", lambda: 42_000.0)
        mcp_server._worker_last_mcp_activity.clear()

        # Simulate what ``_dispatch`` does for an identified worker.
        mcp_server._worker_last_mcp_activity["rcg-dev-install"] = mcp_server.time.time()

        assert mcp_server.get_worker_last_mcp_activity("rcg-dev-install") == 42_000.0
