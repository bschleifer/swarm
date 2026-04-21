"""Tests for the InterWorkerMessageWatcher drone (task #235 Phase 3)."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from swarm.config import DroneConfig
from swarm.drones.inter_worker_watcher import InterWorkerMessageWatcher
from swarm.drones.log import DroneAction
from swarm.worker.worker import WorkerState


def _worker(name: str, state: WorkerState) -> MagicMock:
    w = MagicMock()
    w.name = name
    w.display_state = state
    w.state = state
    return w


def _message(sender: str, recipient: str, content: str = "x", ts: float = 0.0) -> MagicMock:
    m = MagicMock()
    m.sender = sender
    m.recipient = recipient
    m.content = content
    m.created_at = ts
    m.read_at = None
    return m


def _store(unread_by_worker: dict[str, list[MagicMock]]) -> MagicMock:
    s = MagicMock()

    def get_unread(name: str) -> list[MagicMock]:
        return unread_by_worker.get(name, [])

    s.get_unread = MagicMock(side_effect=get_unread)
    return s


class _Sender:
    def __init__(self, *, raise_for: set[str] | None = None) -> None:
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self._raise_for = raise_for or set()

    async def __call__(self, name: str, message: str, **kwargs: Any) -> None:
        if name in self._raise_for:
            raise OSError(f"PTY gone for {name}")
        self.calls.append((name, message, kwargs))


def _watcher(
    *,
    store: MagicMock,
    interval: float = 60.0,
    debounce: float = 900.0,
    rate_limit_check=None,
    sender: _Sender | None = None,
) -> tuple[InterWorkerMessageWatcher, _Sender, MagicMock]:
    sender = sender if sender is not None else _Sender()
    drone_log = MagicMock()
    cfg = DroneConfig(
        idle_nudge_interval_seconds=interval,
        idle_nudge_debounce_seconds=debounce,
    )
    w = InterWorkerMessageWatcher(
        drone_config=cfg,
        message_store=store,
        drone_log=drone_log,
        send_to_worker=sender,
        rate_limit_check=rate_limit_check,
    )
    return w, sender, drone_log


# ---------------------------------------------------------------------------
# Core sweep behaviour
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resting_recipient_with_unread_gets_nudged() -> None:
    """Happy path: inter-worker message to an idle recipient → one
    PTY nudge + one AUTO_NUDGE_MESSAGE buzz entry."""
    store = _store({"hub": [_message("platform", "hub", "fix the thing")]})
    watcher, sender, drone_log = _watcher(store=store)

    sent = await watcher.sweep([_worker("hub", WorkerState.RESTING)], now=1000.0)

    assert sent == 1
    assert len(sender.calls) == 1
    name, message, kwargs = sender.calls[0]
    assert name == "hub"
    assert "platform" in message
    assert "swarm_check_messages" in message
    assert kwargs.get("_log_operator") is False
    drone_log.add.assert_called_once()
    entry = drone_log.add.call_args
    assert entry.args[0] is DroneAction.AUTO_NUDGE_MESSAGE
    assert entry.args[1] == "hub"


@pytest.mark.asyncio
async def test_buzzing_recipient_is_skipped() -> None:
    store = _store({"hub": [_message("platform", "hub")]})
    watcher, sender, _ = _watcher(store=store)

    sent = await watcher.sweep([_worker("hub", WorkerState.BUZZING)], now=1000.0)

    assert sent == 0
    assert sender.calls == []


@pytest.mark.asyncio
async def test_no_unread_messages_means_no_nudge() -> None:
    store = _store({})
    watcher, sender, _ = _watcher(store=store)

    sent = await watcher.sweep([_worker("hub", WorkerState.RESTING)], now=1000.0)

    assert sent == 0
    assert sender.calls == []


@pytest.mark.asyncio
async def test_queen_sourced_messages_dont_trigger_watcher() -> None:
    """Messages FROM the queen should not trigger the watcher — the
    queen already has her own prompt-worker path. Double-nudging would
    spam the recipient."""
    store = _store({"hub": [_message("queen", "hub")]})
    watcher, sender, _ = _watcher(store=store)

    sent = await watcher.sweep([_worker("hub", WorkerState.RESTING)], now=1000.0)

    assert sent == 0


@pytest.mark.asyncio
async def test_queen_recipient_is_skipped() -> None:
    """The queen gets her own inbox relay via Phase 1; don't double-nudge."""
    store = _store({"queen": [_message("hub", "queen")]})
    watcher, sender, _ = _watcher(store=store)

    sent = await watcher.sweep([_worker("queen", WorkerState.RESTING)], now=1000.0)

    assert sent == 0


# ---------------------------------------------------------------------------
# Debounce
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_debounce_suppresses_repeat_nudge_within_window() -> None:
    store = _store({"hub": [_message("platform", "hub")]})
    watcher, sender, _ = _watcher(store=store, interval=1.0, debounce=900.0)

    await watcher.sweep([_worker("hub", WorkerState.RESTING)], now=1000.0)
    await watcher.sweep([_worker("hub", WorkerState.RESTING)], now=1500.0)

    assert len(sender.calls) == 1


@pytest.mark.asyncio
async def test_debounce_allows_repeat_after_window() -> None:
    store = _store({"hub": [_message("platform", "hub")]})
    watcher, sender, _ = _watcher(store=store, interval=1.0, debounce=900.0)

    await watcher.sweep([_worker("hub", WorkerState.RESTING)], now=1000.0)
    await watcher.sweep([_worker("hub", WorkerState.RESTING)], now=2500.0)

    assert len(sender.calls) == 2


# ---------------------------------------------------------------------------
# Rate-limit escape hatch + fault isolation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rate_limited_worker_is_skipped() -> None:
    store = _store({"hub": [_message("platform", "hub")]})
    watcher, sender, drone_log = _watcher(store=store, rate_limit_check=lambda name: name == "hub")

    sent = await watcher.sweep([_worker("hub", WorkerState.RESTING)], now=1000.0)

    assert sent == 0
    assert sender.calls == []
    drone_log.add.assert_not_called()


@pytest.mark.asyncio
async def test_send_failure_for_one_recipient_does_not_stop_sweep() -> None:
    store = _store(
        {
            "alpha": [_message("platform", "alpha")],
            "bravo": [_message("platform", "bravo")],
        }
    )
    sender = _Sender(raise_for={"alpha"})
    watcher, _, drone_log = _watcher(store=store, sender=sender)

    workers = [
        _worker("alpha", WorkerState.RESTING),
        _worker("bravo", WorkerState.RESTING),
    ]
    sent = await watcher.sweep(workers, now=1000.0)

    assert sent == 1
    assert len(sender.calls) == 1
    assert sender.calls[0][0] == "bravo"
    assert drone_log.add.call_count == 1


# ---------------------------------------------------------------------------
# Interval / enabled
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_interval_zero_disables_sweep() -> None:
    store = _store({"hub": [_message("platform", "hub")]})
    watcher, sender, _ = _watcher(store=store, interval=0.0)

    assert watcher.enabled is False
    sent = await watcher.sweep([_worker("hub", WorkerState.RESTING)], now=1000.0)
    assert sent == 0
    assert sender.calls == []


@pytest.mark.asyncio
async def test_no_message_store_disables_sweep() -> None:
    """An operator without a message store shouldn't crash the sweep."""
    watcher, sender, _ = _watcher(store=None)  # type: ignore[arg-type]
    assert watcher.enabled is False

    sent = await watcher.sweep([_worker("hub", WorkerState.RESTING)], now=1000.0)
    assert sent == 0
    assert sender.calls == []
