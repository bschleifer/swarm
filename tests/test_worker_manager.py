from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from swarm.config import WorkerConfig
from swarm.worker.manager import add_worker_live, kill_worker, launch_hive, revive_worker
from swarm.worker.worker import Worker, WorkerState


@pytest.mark.asyncio
async def test_launch_hive_creates_session():
    with (
        patch("swarm.worker.manager.hive.session_exists", AsyncMock(return_value=False)),
        patch("swarm.worker.manager.hive.create_session", AsyncMock()) as create_session,
        patch(
            "swarm.worker.manager.plan_layout",
            return_value=[[WorkerConfig(name="api", path="/tmp/api")]],
        ),
        patch("swarm.worker.manager.apply_tiled_layout", AsyncMock(return_value=["%1"])),
        patch("swarm.worker.manager.hive.set_pane_option", AsyncMock()),
        patch("swarm.worker.manager.send_keys", AsyncMock()),
        patch("swarm.worker.manager.setup_tmux_for_session", AsyncMock()),
        patch("swarm.worker.manager.apply_session_style", AsyncMock()),
        patch("swarm.worker.manager.bind_session_keys", AsyncMock()),
    ):
        workers = [WorkerConfig(name="api", path="/tmp/api")]
        result = await launch_hive("test", workers)

        create_session.assert_called_once_with("test", "api", "/tmp/api")
        assert len(result) == 1
        assert result[0].name == "api"
        assert result[0].pane_id == "%1"


@pytest.mark.asyncio
async def test_launch_hive_kills_existing_session():
    with (
        patch("swarm.worker.manager.hive.session_exists", AsyncMock(return_value=True)),
        patch("swarm.worker.manager.hive.kill_session", AsyncMock()) as kill_session,
        patch("swarm.worker.manager.hive.create_session", AsyncMock()),
        patch(
            "swarm.worker.manager.plan_layout",
            return_value=[[WorkerConfig(name="api", path="/tmp/api")]],
        ),
        patch("swarm.worker.manager.apply_tiled_layout", AsyncMock(return_value=["%1"])),
        patch("swarm.worker.manager.hive.set_pane_option", AsyncMock()),
        patch("swarm.worker.manager.send_keys", AsyncMock()),
        patch("swarm.worker.manager.setup_tmux_for_session", AsyncMock()),
        patch("swarm.worker.manager.apply_session_style", AsyncMock()),
        patch("swarm.worker.manager.bind_session_keys", AsyncMock()),
    ):
        workers = [WorkerConfig(name="api", path="/tmp/api")]
        await launch_hive("test", workers)

        kill_session.assert_called_once_with("test")


@pytest.mark.asyncio
async def test_launch_hive_multiple_windows():
    wc1 = WorkerConfig(name="api", path="/tmp/api")
    wc2 = WorkerConfig(name="web", path="/tmp/web")

    with (
        patch("swarm.worker.manager.hive.session_exists", AsyncMock(return_value=False)),
        patch("swarm.worker.manager.hive.create_session", AsyncMock()),
        patch("swarm.worker.manager.hive.add_window", AsyncMock(return_value="1")) as add_window,
        patch("swarm.worker.manager.plan_layout", return_value=[[wc1], [wc2]]),
        patch("swarm.worker.manager.apply_tiled_layout", AsyncMock(side_effect=[["%1"], ["%2"]])),
        patch("swarm.worker.manager.hive.set_pane_option", AsyncMock()),
        patch("swarm.worker.manager.send_keys", AsyncMock()),
        patch("swarm.worker.manager.setup_tmux_for_session", AsyncMock()),
        patch("swarm.worker.manager.apply_session_style", AsyncMock()),
        patch("swarm.worker.manager.bind_session_keys", AsyncMock()),
    ):
        workers = [wc1, wc2]
        result = await launch_hive("test", workers)

        add_window.assert_called_once_with("test", "web", "/tmp/web")
        assert len(result) == 2
        assert result[0].window_index == "0"
        assert result[1].window_index == "1"


@pytest.mark.asyncio
async def test_launch_hive_sets_pane_options():
    with (
        patch("swarm.worker.manager.hive.session_exists", AsyncMock(return_value=False)),
        patch("swarm.worker.manager.hive.create_session", AsyncMock()),
        patch(
            "swarm.worker.manager.plan_layout",
            return_value=[[WorkerConfig(name="api", path="/tmp/api")]],
        ),
        patch("swarm.worker.manager.apply_tiled_layout", AsyncMock(return_value=["%1"])),
        patch("swarm.worker.manager.hive.set_pane_option", AsyncMock()) as set_pane_option,
        patch("swarm.worker.manager.send_keys", AsyncMock()),
        patch("swarm.worker.manager.setup_tmux_for_session", AsyncMock()),
        patch("swarm.worker.manager.apply_session_style", AsyncMock()),
        patch("swarm.worker.manager.bind_session_keys", AsyncMock()),
    ):
        workers = [WorkerConfig(name="api", path="/tmp/api")]
        await launch_hive("test", workers)

        assert set_pane_option.call_count == 2
        set_pane_option.assert_any_call("%1", "@swarm_name", "api")
        set_pane_option.assert_any_call("%1", "@swarm_state", "BUZZING")


@pytest.mark.asyncio
async def test_launch_hive_sends_claude_command():
    with (
        patch("swarm.worker.manager.hive.session_exists", AsyncMock(return_value=False)),
        patch("swarm.worker.manager.hive.create_session", AsyncMock()),
        patch(
            "swarm.worker.manager.plan_layout",
            return_value=[[WorkerConfig(name="api", path="/tmp/api")]],
        ),
        patch("swarm.worker.manager.apply_tiled_layout", AsyncMock(return_value=["%1"])),
        patch("swarm.worker.manager.hive.set_pane_option", AsyncMock()),
        patch("swarm.worker.manager.send_keys", AsyncMock()) as send_keys,
        patch("swarm.worker.manager.setup_tmux_for_session", AsyncMock()),
        patch("swarm.worker.manager.apply_session_style", AsyncMock()),
        patch("swarm.worker.manager.bind_session_keys", AsyncMock()),
    ):
        workers = [WorkerConfig(name="api", path="/tmp/api")]
        await launch_hive("test", workers)

        send_keys.assert_called_once_with("%1", "claude --continue", enter=True)


@pytest.mark.asyncio
async def test_revive_worker_pane_exists():
    worker = Worker(name="api", path="/tmp/api", pane_id="%1")

    with (
        patch("swarm.tmux.cell.pane_exists", AsyncMock(return_value=True)),
        patch("swarm.worker.manager.hive.set_pane_option", AsyncMock()) as set_opt,
        patch("swarm.worker.manager.send_keys", AsyncMock()) as send_keys,
    ):
        await revive_worker(worker)

        set_opt.assert_called_once_with("%1", "@swarm_state", "BUZZING")
        send_keys.assert_called_once_with("%1", "claude --continue", enter=True)


@pytest.mark.asyncio
async def test_revive_worker_pane_gone_with_session():
    worker = Worker(name="api", path="/tmp/api", pane_id="%1", window_index="0")

    with (
        patch("swarm.tmux.cell.pane_exists", AsyncMock(return_value=False)),
        patch("swarm.worker.manager.hive.add_pane", AsyncMock(return_value="%2")) as add_pane,
        patch("swarm.worker.manager.hive.set_pane_option", AsyncMock()) as set_pane_option,
        patch("swarm.worker.manager.send_keys", AsyncMock()) as send_keys,
    ):
        await revive_worker(worker, session_name="test")

        add_pane.assert_called_once_with("test", "0", "/tmp/api")
        assert worker.pane_id == "%2"
        set_pane_option.assert_any_call("%2", "@swarm_name", "api")
        set_pane_option.assert_any_call("%2", "@swarm_state", "BUZZING")
        send_keys.assert_called_once_with("%2", "claude", enter=True)


@pytest.mark.asyncio
async def test_revive_worker_pane_gone_no_session():
    worker = Worker(name="api", path="/tmp/api", pane_id="%1")

    with (
        patch("swarm.tmux.cell.pane_exists", AsyncMock(return_value=False)),
        patch("swarm.worker.manager.hive.add_pane", AsyncMock()) as add_pane,
    ):
        await revive_worker(worker, session_name=None)

        add_pane.assert_not_called()


@pytest.mark.asyncio
async def test_add_worker_live_splits_in_existing_window():
    workers = []
    config = WorkerConfig(name="api", path="/tmp/api")

    with (
        patch("swarm.worker.manager.hive.list_window_indices", AsyncMock(return_value=["0"])),
        patch("swarm.worker.manager.hive.count_panes", AsyncMock(return_value=3)),
        patch("swarm.worker.manager.hive.add_pane", AsyncMock(return_value="%4")) as add_pane,
        patch("swarm.worker.manager.hive.set_pane_option", AsyncMock()) as set_pane_option,
    ):
        worker = await add_worker_live("test", config, workers, panes_per_window=9)

        add_pane.assert_called_once_with("test", "0", "/tmp/api")
        assert worker.name == "api"
        assert worker.pane_id == "%4"
        assert worker.window_index == "0"
        assert worker.state == WorkerState.RESTING
        assert len(workers) == 1
        set_pane_option.assert_any_call("%4", "@swarm_name", "api")
        set_pane_option.assert_any_call("%4", "@swarm_state", "RESTING")


@pytest.mark.asyncio
async def test_add_worker_live_creates_new_window():
    workers = []
    config = WorkerConfig(name="api", path="/tmp/api")

    with (
        patch("swarm.worker.manager.hive.list_window_indices", AsyncMock(return_value=["0"])),
        patch("swarm.worker.manager.hive.count_panes", AsyncMock(return_value=9)),
        patch("swarm.worker.manager.hive.add_window", AsyncMock(return_value="1")) as add_window,
        patch("swarm.worker.manager.get_pane_id", AsyncMock(return_value="%10")),
        patch("swarm.worker.manager.hive.set_pane_option", AsyncMock()),
    ):
        worker = await add_worker_live("test", config, workers, panes_per_window=9)

        add_window.assert_called_once_with("test", "api", "/tmp/api")
        assert worker.window_index == "1"
        assert worker.pane_id == "%10"


@pytest.mark.asyncio
async def test_add_worker_live_first_window():
    workers = []
    config = WorkerConfig(name="api", path="/tmp/api")

    with (
        patch("swarm.worker.manager.hive.list_window_indices", AsyncMock(return_value=[])),
        patch("swarm.worker.manager.hive.count_panes", AsyncMock(return_value=0)),
        patch("swarm.worker.manager.hive.add_pane", AsyncMock(return_value="%1")),
        patch("swarm.worker.manager.hive.set_pane_option", AsyncMock()),
    ):
        worker = await add_worker_live("test", config, workers, panes_per_window=9)

        assert worker.window_index == "0"


@pytest.mark.asyncio
async def test_kill_worker_sends_interrupt_and_kills_pane():
    worker = Worker(name="api", path="/tmp/api", pane_id="%1")

    with (
        patch("swarm.tmux.cell.send_interrupt", AsyncMock()) as send_interrupt,
        patch("swarm.worker.manager.hive.kill_pane", AsyncMock()) as kill_pane,
    ):
        await kill_worker(worker)

        send_interrupt.assert_called_once_with("%1")
        kill_pane.assert_called_once_with("%1")


@pytest.mark.asyncio
async def test_kill_worker_ignores_interrupt_error():
    worker = Worker(name="api", path="/tmp/api", pane_id="%1")

    from swarm.tmux.cell import TmuxError

    with (
        patch("swarm.tmux.cell.send_interrupt", AsyncMock(side_effect=TmuxError("boom"))),
        patch("swarm.worker.manager.hive.kill_pane", AsyncMock()) as kill_pane,
    ):
        await kill_worker(worker)

        kill_pane.assert_called_once()


@pytest.mark.asyncio
async def test_kill_worker_ignores_kill_pane_error():
    worker = Worker(name="api", path="/tmp/api", pane_id="%1")

    from swarm.tmux.cell import TmuxError

    with (
        patch("swarm.tmux.cell.send_interrupt", AsyncMock()),
        patch("swarm.worker.manager.hive.kill_pane", AsyncMock(side_effect=TmuxError("boom"))),
    ):
        await kill_worker(worker)
