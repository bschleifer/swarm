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


def _message(
    sender: str,
    recipient: str,
    content: str = "x",
    ts: float = 0.0,
    msg_type: str = "dependency",
) -> MagicMock:
    """Construct a fake Message. Defaults to ``msg_type='dependency'``
    since task #271 narrowed the nudge trigger to action-required types
    (``dependency`` / ``warning``); tests that want to exercise the
    nudge-fires path can rely on the default, tests that want to pin
    the skip-on-informational path should pass ``msg_type='finding'``
    (or similar)."""
    m = MagicMock()
    m.sender = sender
    m.recipient = recipient
    m.content = content
    m.created_at = ts
    m.read_at = None
    m.msg_type = msg_type
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


# ---------------------------------------------------------------------------
# Task #271: narrow trigger by message type.  Informational types
# (finding / status / note) should not pull a worker off its current
# task; only action-required types (dependency / warning) nudge.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_finding_alone_does_not_trigger_nudge() -> None:
    """wifi-portal repro: an FYI finding from public-website on an idle
    worker who has already self-resolved the underlying concern must
    NOT trigger a PTY nudge.  Drone logs an
    ``AUTO_NUDGE_MESSAGE_SKIPPED`` entry instead so the operator has
    telemetry on the suppression."""
    store = _store({"wifi-portal": [_message("public-website", "wifi-portal", msg_type="finding")]})
    watcher, sender, drone_log = _watcher(store=store)

    sent = await watcher.sweep([_worker("wifi-portal", WorkerState.RESTING)], now=1000.0)

    assert sent == 0
    assert sender.calls == []
    # One SKIPPED entry, zero nudge entries.
    actions = [call.args[0] for call in drone_log.add.call_args_list]
    assert DroneAction.AUTO_NUDGE_MESSAGE_SKIPPED in actions
    assert DroneAction.AUTO_NUDGE_MESSAGE not in actions


@pytest.mark.asyncio
async def test_status_alone_does_not_trigger_nudge() -> None:
    """Routine progress-update messages are informational; skip the nudge."""
    store = _store({"hub": [_message("platform", "hub", msg_type="status")]})
    watcher, sender, drone_log = _watcher(store=store)

    sent = await watcher.sweep([_worker("hub", WorkerState.RESTING)], now=1000.0)

    assert sent == 0
    assert sender.calls == []
    actions = [call.args[0] for call in drone_log.add.call_args_list]
    assert DroneAction.AUTO_NUDGE_MESSAGE_SKIPPED in actions


@pytest.mark.asyncio
async def test_note_alone_does_not_trigger_nudge() -> None:
    """Side-channel notes (task #248 msg_type) are informational."""
    store = _store({"hub": [_message("platform", "hub", msg_type="note")]})
    watcher, sender, drone_log = _watcher(store=store)

    sent = await watcher.sweep([_worker("hub", WorkerState.RESTING)], now=1000.0)

    assert sent == 0
    assert sender.calls == []
    actions = [call.args[0] for call in drone_log.add.call_args_list]
    assert DroneAction.AUTO_NUDGE_MESSAGE_SKIPPED in actions


@pytest.mark.asyncio
async def test_dependency_triggers_nudge() -> None:
    """Baseline: a ``dependency`` message — the canonical action-
    required type — still fires a nudge."""
    store = _store({"hub": [_message("platform", "hub", msg_type="dependency")]})
    watcher, sender, drone_log = _watcher(store=store)

    sent = await watcher.sweep([_worker("hub", WorkerState.RESTING)], now=1000.0)

    assert sent == 1
    assert len(sender.calls) == 1
    actions = [call.args[0] for call in drone_log.add.call_args_list]
    assert DroneAction.AUTO_NUDGE_MESSAGE in actions
    assert DroneAction.AUTO_NUDGE_MESSAGE_SKIPPED not in actions


@pytest.mark.asyncio
async def test_warning_triggers_nudge() -> None:
    """``warning`` is also an action-required type and should nudge."""
    store = _store({"hub": [_message("platform", "hub", msg_type="warning")]})
    watcher, sender, _ = _watcher(store=store)

    sent = await watcher.sweep([_worker("hub", WorkerState.RESTING)], now=1000.0)

    assert sent == 1
    assert len(sender.calls) == 1


@pytest.mark.asyncio
async def test_mixed_inbox_nudges_on_action_required_surfaces_full_count() -> None:
    """When at least one action-required message exists, nudge fires
    and the nudge wording surfaces the full unread count (not just the
    action-required subset) so the worker knows what awaits in the
    inbox."""
    store = _store(
        {
            "hub": [
                _message("platform", "hub", msg_type="finding", ts=1.0),
                _message("admin", "hub", msg_type="status", ts=2.0),
                _message("nexus", "hub", msg_type="dependency", ts=3.0),
            ]
        }
    )
    watcher, sender, drone_log = _watcher(store=store)

    sent = await watcher.sweep([_worker("hub", WorkerState.RESTING)], now=1000.0)

    assert sent == 1
    assert len(sender.calls) == 1
    # Nudge text still references total unread count.
    assert "3 new messages" in sender.calls[0][1]
    # Buzz log entry names the action-required one that drove the nudge.
    nudge_entries = [
        c for c in drone_log.add.call_args_list if c.args[0] is DroneAction.AUTO_NUDGE_MESSAGE
    ]
    assert len(nudge_entries) == 1
    detail = nudge_entries[0].args[2]
    assert "nexus" in detail  # sender of the action-required msg
    assert "dependency" in detail


@pytest.mark.asyncio
async def test_skipped_entry_debounced_per_worker() -> None:
    """Back-to-back sweeps over the same informational-only inbox
    should log AUTO_NUDGE_MESSAGE_SKIPPED at most once per debounce
    window (avoids spamming the buzz log)."""
    store = _store({"hub": [_message("platform", "hub", msg_type="finding")]})
    watcher, sender, drone_log = _watcher(store=store, interval=1.0)

    await watcher.sweep([_worker("hub", WorkerState.RESTING)], now=1000.0)
    await watcher.sweep([_worker("hub", WorkerState.RESTING)], now=1100.0)

    skipped_entries = [
        c
        for c in drone_log.add.call_args_list
        if c.args[0] is DroneAction.AUTO_NUDGE_MESSAGE_SKIPPED
    ]
    assert len(skipped_entries) == 1, (
        "second sweep should not re-log AUTO_NUDGE_MESSAGE_SKIPPED within the debounce window"
    )
    assert sender.calls == []
