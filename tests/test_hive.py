"""Tests for tmux/hive.py — session management and discovery."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from swarm.tmux.cell import TmuxError
from swarm.tmux.hive import (
    add_pane,
    add_window,
    count_panes,
    create_session,
    discover_workers,
    find_swarm_session,
    get_pane_option,
    kill_pane,
    kill_session,
    list_window_indices,
    session_exists,
    set_pane_option,
    update_window_names,
)
from swarm.worker.worker import Worker, WorkerState


# ---------- session_exists ----------


@pytest.mark.asyncio
async def test_session_exists_true():
    with patch("swarm.tmux.hive.run_tmux", new_callable=AsyncMock, return_value=""):
        assert await session_exists("my-session") is True


@pytest.mark.asyncio
async def test_session_exists_false():
    with patch(
        "swarm.tmux.hive.run_tmux",
        new_callable=AsyncMock,
        side_effect=TmuxError("no session"),
    ):
        assert await session_exists("nope") is False


# ---------- create_session ----------


@pytest.mark.asyncio
async def test_create_session():
    mock = AsyncMock()
    with (
        patch("swarm.tmux.hive.run_tmux", mock),
        patch("os.get_terminal_size", return_value=(120, 40)),
    ):
        await create_session("swarm", "api", "/tmp/api")
    # Should call new-session, two set commands, and set-environment
    assert mock.await_count == 4
    first_call = mock.call_args_list[0]
    assert first_call[0][0] == "new-session"
    assert "120" in first_call[0]
    assert "40" in first_call[0]


@pytest.mark.asyncio
async def test_create_session_no_terminal():
    mock = AsyncMock()
    with (
        patch("swarm.tmux.hive.run_tmux", mock),
        patch("os.get_terminal_size", side_effect=OSError),
    ):
        await create_session("swarm", "api", "/tmp/api")
    first_call = mock.call_args_list[0]
    # Fallback: 200x50
    assert "200" in first_call[0]
    assert "50" in first_call[0]


# ---------- add_pane / add_window ----------


@pytest.mark.asyncio
async def test_add_pane():
    with patch("swarm.tmux.hive.run_tmux", new_callable=AsyncMock, return_value="%5"):
        pane_id = await add_pane("swarm", "0", "/tmp/work")
    assert pane_id == "%5"


@pytest.mark.asyncio
async def test_add_window():
    with patch("swarm.tmux.hive.run_tmux", new_callable=AsyncMock, return_value="2"):
        idx = await add_window("swarm", "workers", "/tmp/work")
    assert idx == "2"


# ---------- pane/window option helpers ----------


@pytest.mark.asyncio
async def test_set_pane_option():
    mock = AsyncMock()
    with patch("swarm.tmux.hive.run_tmux", mock):
        await set_pane_option("%0", "@swarm_name", "api")
    mock.assert_awaited_once_with("set", "-p", "-t", "%0", "@swarm_name", "api")


@pytest.mark.asyncio
async def test_get_pane_option():
    with patch("swarm.tmux.hive.run_tmux", new_callable=AsyncMock, return_value="api"):
        val = await get_pane_option("%0", "@swarm_name")
    assert val == "api"


# ---------- list_window_indices / count_panes ----------


@pytest.mark.asyncio
async def test_list_window_indices():
    with patch(
        "swarm.tmux.hive.run_tmux",
        new_callable=AsyncMock,
        return_value="0\n1\n2\n",
    ):
        indices = await list_window_indices("swarm")
    assert indices == ["0", "1", "2"]


@pytest.mark.asyncio
async def test_list_window_indices_empty():
    with patch("swarm.tmux.hive.run_tmux", new_callable=AsyncMock, return_value=""):
        indices = await list_window_indices("swarm")
    assert indices == []


@pytest.mark.asyncio
async def test_count_panes():
    with patch(
        "swarm.tmux.hive.run_tmux",
        new_callable=AsyncMock,
        return_value="%0\n%1\n%2\n",
    ):
        n = await count_panes("swarm", "0")
    assert n == 3


# ---------- kill_pane / kill_session ----------


@pytest.mark.asyncio
async def test_kill_pane():
    mock = AsyncMock()
    with patch("swarm.tmux.hive.run_tmux", mock):
        await kill_pane("%3")
    mock.assert_awaited_once_with("kill-pane", "-t", "%3")


@pytest.mark.asyncio
async def test_kill_session():
    mock = AsyncMock()
    with patch("swarm.tmux.hive.run_tmux", mock):
        await kill_session("swarm")
    mock.assert_awaited_once_with("kill-session", "-t", "swarm")


# ---------- find_swarm_session ----------


@pytest.mark.asyncio
async def test_find_swarm_session_found():
    async def fake_run(*args):
        if args[0] == "list-sessions":
            return "alpha\nbeta\n"
        if args[0] == "list-panes":
            if args[3] == "alpha":
                return "\n\n"  # No swarm metadata
            return "api-worker\n"  # Has metadata
        return ""

    with patch("swarm.tmux.hive.run_tmux", side_effect=fake_run):
        result = await find_swarm_session()
    assert result == "beta"


@pytest.mark.asyncio
async def test_find_swarm_session_none():
    async def fake_run(*args):
        if args[0] == "list-sessions":
            return "alpha\n"
        return "\n"

    with patch("swarm.tmux.hive.run_tmux", side_effect=fake_run):
        result = await find_swarm_session()
    assert result is None


@pytest.mark.asyncio
async def test_find_swarm_session_no_tmux():
    with patch(
        "swarm.tmux.hive.run_tmux",
        new_callable=AsyncMock,
        side_effect=TmuxError("no tmux"),
    ):
        result = await find_swarm_session()
    assert result is None


# ---------- discover_workers ----------


@pytest.mark.asyncio
async def test_discover_workers_reads_swarm_state():
    """discover_workers should read @swarm_state from tmux and set worker state."""
    # Format: pane_id, win_idx, pane_idx, name, path, @swarm_state
    lines = "%0\t0\t0\tapi\t/tmp\tRESTING\n%1\t0\t1\tweb\t/tmp\tBUZZING\n"
    with (
        patch("swarm.tmux.hive.session_exists", new_callable=AsyncMock, return_value=True),
        patch("swarm.tmux.hive.run_tmux", new_callable=AsyncMock, return_value=lines),
    ):
        workers = await discover_workers("test")
    assert len(workers) == 2
    assert workers[0].state == WorkerState.RESTING
    assert workers[1].state == WorkerState.BUZZING


@pytest.mark.asyncio
async def test_discover_workers_sleeping_maps_to_resting():
    """SLEEPING in tmux should map back to RESTING (internal state)."""
    lines = "%0\t0\t0\tapi\t/tmp\tSLEEPING\n"
    with (
        patch("swarm.tmux.hive.session_exists", new_callable=AsyncMock, return_value=True),
        patch("swarm.tmux.hive.run_tmux", new_callable=AsyncMock, return_value=lines),
    ):
        workers = await discover_workers("test")
    assert workers[0].state == WorkerState.RESTING


@pytest.mark.asyncio
async def test_discover_workers_unknown_state_defaults_buzzing():
    """Unknown @swarm_state should default to BUZZING."""
    lines = "%0\t0\t0\tapi\t/tmp\tUNKNOWN\n"
    with (
        patch("swarm.tmux.hive.session_exists", new_callable=AsyncMock, return_value=True),
        patch("swarm.tmux.hive.run_tmux", new_callable=AsyncMock, return_value=lines),
    ):
        workers = await discover_workers("test")
    assert workers[0].state == WorkerState.BUZZING


@pytest.mark.asyncio
async def test_discover_workers_empty_state_defaults_buzzing():
    """Empty @swarm_state should default to BUZZING."""
    lines = "%0\t0\t0\tapi\t/tmp\t\n"
    with (
        patch("swarm.tmux.hive.session_exists", new_callable=AsyncMock, return_value=True),
        patch("swarm.tmux.hive.run_tmux", new_callable=AsyncMock, return_value=lines),
    ):
        workers = await discover_workers("test")
    assert workers[0].state == WorkerState.BUZZING


@pytest.mark.asyncio
async def test_discover_workers_extra_tabs_in_path():
    """Paths containing tabs should not crash discover_workers.

    With the 6-field format (including @swarm_state), a tab in the path
    is consumed as a field separator, so the path gets truncated.  The key
    invariant is that discover_workers doesn't crash.
    """
    # 6 fields: pane_id, win_idx, pane_idx, name, path, @swarm_state
    # The tab inside the "path" is consumed as the state field separator.
    lines = "%0\t0\t0\tapi\t/home/user/my\tproject\n%1\t0\t1\tweb\t/home/user/normal\tBUZZING\n"
    with (
        patch("swarm.tmux.hive.session_exists", new_callable=AsyncMock, return_value=True),
        patch("swarm.tmux.hive.run_tmux", new_callable=AsyncMock, return_value=lines),
    ):
        workers = await discover_workers("test")
    assert len(workers) == 2
    assert workers[0].name == "api"
    assert workers[0].path == "/home/user/my"  # truncated at tab
    assert workers[1].name == "web"
    assert workers[1].path == "/home/user/normal"


@pytest.mark.asyncio
async def test_discover_workers_no_session():
    with patch("swarm.tmux.hive.session_exists", new_callable=AsyncMock, return_value=False):
        workers = await discover_workers("no-session")
    assert workers == []


@pytest.mark.asyncio
async def test_discover_workers_unnamed_panes():
    """Panes without @swarm_name get auto-generated names."""
    lines = "%0\t0\t0\t\t/tmp/work\n%1\t1\t0\t\t/tmp/other\n"
    with (
        patch("swarm.tmux.hive.session_exists", new_callable=AsyncMock, return_value=True),
        patch("swarm.tmux.hive.run_tmux", new_callable=AsyncMock, return_value=lines),
    ):
        workers = await discover_workers("test")
    assert workers[0].name == "pane-0.0"
    assert workers[1].name == "pane-1.0"


@pytest.mark.asyncio
async def test_discover_workers_malformed_line():
    lines = "incomplete\n%0\t0\t0\tapi\t/tmp\n"
    with (
        patch("swarm.tmux.hive.session_exists", new_callable=AsyncMock, return_value=True),
        patch("swarm.tmux.hive.run_tmux", new_callable=AsyncMock, return_value=lines),
    ):
        workers = await discover_workers("test")
    assert len(workers) == 1


@pytest.mark.asyncio
async def test_discover_workers_tmux_error():
    """TmuxError during list-panes returns empty list."""
    call_count = 0

    async def fake_run(*args):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return ""  # has-session succeeds
        raise TmuxError("gone")

    with (
        patch("swarm.tmux.hive.session_exists", new_callable=AsyncMock, return_value=True),
        patch("swarm.tmux.hive.run_tmux", side_effect=fake_run),
    ):
        workers = await discover_workers("test")
    assert workers == []


# ---------- update_window_names ----------


@pytest.mark.asyncio
async def test_update_window_names_adds_idle_suffix():
    w1 = Worker(name="api", path="/tmp", pane_id="%0", state=WorkerState.RESTING)
    w2 = Worker(name="web", path="/tmp", pane_id="%1", state=WorkerState.BUZZING)

    calls = []

    async def fake_run(*args):
        calls.append(args)
        if args[0] == "list-windows":
            return "0\tworkers"
        if args[0] == "list-panes":
            return "%0\t0\n%1\t0\n"
        return ""

    with patch("swarm.tmux.hive.run_tmux", side_effect=fake_run):
        await update_window_names("swarm", [w1, w2])

    rename_calls = [c for c in calls if c[0] == "rename-window"]
    assert len(rename_calls) == 1
    assert rename_calls[0][-1] == "workers (1 idle)"


@pytest.mark.asyncio
async def test_update_window_names_waiting_takes_priority():
    w1 = Worker(name="api", path="/tmp", pane_id="%0", state=WorkerState.WAITING)
    w2 = Worker(name="web", path="/tmp", pane_id="%1", state=WorkerState.RESTING)

    calls = []

    async def fake_run(*args):
        calls.append(args)
        if args[0] == "list-windows":
            return "0\tworkers"
        if args[0] == "list-panes":
            return "%0\t0\n%1\t0\n"
        return ""

    with patch("swarm.tmux.hive.run_tmux", side_effect=fake_run):
        await update_window_names("swarm", [w1, w2])

    rename_calls = [c for c in calls if c[0] == "rename-window"]
    assert len(rename_calls) == 1
    assert "1 waiting" in rename_calls[0][-1]


@pytest.mark.asyncio
async def test_update_window_names_strips_existing_suffix():
    w1 = Worker(name="api", path="/tmp", pane_id="%0", state=WorkerState.BUZZING)

    calls = []

    async def fake_run(*args):
        calls.append(args)
        if args[0] == "list-windows":
            return "0\tworkers (2 idle)"
        if args[0] == "list-panes":
            return "%0\t0\n"
        return ""

    with patch("swarm.tmux.hive.run_tmux", side_effect=fake_run):
        await update_window_names("swarm", [w1])

    rename_calls = [c for c in calls if c[0] == "rename-window"]
    assert len(rename_calls) == 1
    assert rename_calls[0][-1] == "workers"


@pytest.mark.asyncio
async def test_update_window_names_no_change():
    """No rename-window call when name hasn't changed."""
    w1 = Worker(name="api", path="/tmp", pane_id="%0", state=WorkerState.BUZZING)

    calls = []

    async def fake_run(*args):
        calls.append(args)
        if args[0] == "list-windows":
            return "0\tworkers"
        if args[0] == "list-panes":
            return "%0\t0\n"
        return ""

    with patch("swarm.tmux.hive.run_tmux", side_effect=fake_run):
        await update_window_names("swarm", [w1])

    rename_calls = [c for c in calls if c[0] == "rename-window"]
    assert len(rename_calls) == 0


@pytest.mark.asyncio
async def test_update_window_names_tmux_error():
    """TmuxError during list-windows returns silently."""
    with patch(
        "swarm.tmux.hive.run_tmux",
        new_callable=AsyncMock,
        side_effect=TmuxError("gone"),
    ):
        await update_window_names("swarm", [])  # Should not raise


@pytest.mark.asyncio
async def test_update_window_names_with_snapshots_skips_list_panes():
    """When snapshots are provided, no list-panes call is made."""
    from swarm.tmux.cell import PaneSnapshot

    w1 = Worker(name="api", path="/tmp", pane_id="%0", state=WorkerState.RESTING)
    w2 = Worker(name="web", path="/tmp", pane_id="%1", state=WorkerState.BUZZING)

    snapshots = {
        "%0": PaneSnapshot(
            pane_id="%0",
            command="claude",
            zoomed=False,
            active=True,
            window_index="0",
        ),
        "%1": PaneSnapshot(
            pane_id="%1",
            command="claude",
            zoomed=False,
            active=False,
            window_index="0",
        ),
    }

    calls: list[tuple] = []

    async def fake_run(*args):
        calls.append(args)
        if args[0] == "list-windows":
            return "0\tworkers"
        return ""

    with patch("swarm.tmux.hive.run_tmux", side_effect=fake_run):
        await update_window_names("swarm", [w1, w2], snapshots=snapshots)

    # Should NOT have called list-panes — that data came from snapshots
    list_panes_calls = [c for c in calls if c[0] == "list-panes"]
    assert len(list_panes_calls) == 0

    # Should still have renamed the window
    rename_calls = [c for c in calls if c[0] == "rename-window"]
    assert len(rename_calls) == 1
    assert rename_calls[0][-1] == "workers (1 idle)"
