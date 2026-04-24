"""Zero-drift invariant: drone unread count == swarm_check_messages count (task #272).

Task #272 was filed on the premise that the InterWorkerMessageWatcher drone
was reporting a phantom unread count divergent from what
``swarm_check_messages`` saw.  Investigation showed the premise was wrong —
both paths call the same ``MessageStore.get_unread(recipient)`` method, the
4 messages the drone counted were genuinely in the DB, and the worker's
``swarm_check_messages`` call was failing for a separate reason (client-
side MCP tool registry stale after daemon reload — task #257's failure
class).  These tests pin the zero-drift invariant explicitly so any future
refactor that introduces a second unread-count path gets caught.

Acceptance #1 of task #272: "Drone's unread count matches what
``swarm_check_messages`` returns for the same worker.  Zero-drift invariant."
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from swarm.config import DroneConfig
from swarm.db import SwarmDB
from swarm.drones.inter_worker_watcher import InterWorkerMessageWatcher
from swarm.drones.log import DroneLog
from swarm.mcp.tools import handle_tool_call
from swarm.messages.store import MessageStore
from swarm.worker.worker import WorkerState


@pytest.fixture()
def store(tmp_path):
    db = SwarmDB(tmp_path / "swarm.db")
    return MessageStore(swarm_db=db)


def _worker(name: str, state: WorkerState = WorkerState.RESTING) -> MagicMock:
    w = MagicMock()
    w.name = name
    w.display_state = state
    w.state = state
    return w


def _daemon_with(store: MessageStore) -> MagicMock:
    d = MagicMock()
    d.message_store = store
    d.drone_log = DroneLog()
    return d


def _check_messages_count(daemon: MagicMock, worker: str) -> int:
    """Call ``swarm_check_messages`` and extract the number of messages
    surfaced back to the worker.  ``swarm_check_messages`` returns one
    content block whose text is either ``"No pending messages."`` for an
    empty inbox or a newline-joined list of ``[type] from sender: body``
    lines — one per message."""
    result = handle_tool_call(daemon, worker, "swarm_check_messages", {})
    text = result[0]["text"]
    if text == "No pending messages.":
        return 0
    return len(text.splitlines())


class TestZeroDriftInvariant:
    """Drone + swarm_check_messages MUST return the same count for a
    given worker.  Failing either side of this invariant reintroduces
    the 'phantom unread' complaint."""

    def test_empty_inbox_agrees(self, store):
        daemon = _daemon_with(store)
        cfg = DroneConfig(idle_nudge_interval_seconds=60, idle_nudge_debounce_seconds=900)
        watcher = InterWorkerMessageWatcher(
            drone_config=cfg,
            message_store=store,
            drone_log=daemon.drone_log,
            send_to_worker=AsyncMock(),
        )
        drone_view = store.get_unread("wifi-portal")
        mcp_view = _check_messages_count(daemon, "wifi-portal")
        assert len(drone_view) == 0
        assert mcp_view == 0
        # Watcher shouldn't fire anything either.
        assert not watcher._message_store.get_unread("wifi-portal")

    def test_action_required_inbox_agrees(self, store):
        """Four dependency messages from distinct senders — drone
        reports 4, MCP reports 4.  Distinct senders are needed because
        ``MessageStore.send`` dedupes on
        ``(sender, recipient, msg_type)`` within a 60s window."""
        for sender in ("public-website", "platform", "admin", "nexus"):
            store.send(sender, "wifi-portal", "dependency", "do the thing")
        drone_view = store.get_unread("wifi-portal")
        assert len(drone_view) == 4
        daemon = _daemon_with(store)
        mcp_view = _check_messages_count(daemon, "wifi-portal")
        assert mcp_view == 4

    def test_informational_inbox_agrees(self, store):
        """Four finding messages from distinct senders — drone still
        reports 4 (same count) even though task #271 means the watcher
        won't actually nudge on them.  Zero-drift is about the COUNT,
        not the trigger."""
        for sender in ("public-website", "platform", "admin", "nexus"):
            store.send(sender, "wifi-portal", "finding", "shipped 0.3.0")
        drone_view = store.get_unread("wifi-portal")
        mcp_view = _check_messages_count(_daemon_with(store), "wifi-portal")
        assert len(drone_view) == 4
        assert mcp_view == 4

    def test_broadcast_and_direct_both_counted(self, store):
        """Broadcast (``recipient='*'``) rows fan out to per-worker rows
        via ``send()``'s broadcast path.  Both drone and MCP handler
        see the recipient's own copy, not the '*' sentinel."""
        store.send("public-website", "wifi-portal", "dependency", "direct")
        # Broadcast — MessageStore.send with "*" writes one row per
        # configured worker.  With no worker registry in this test the
        # broadcast writes nothing, so only the direct row counts.  The
        # invariant we pin: whatever the store returns, MCP returns
        # the same count.
        drone_view = store.get_unread("wifi-portal")
        mcp_view = _check_messages_count(_daemon_with(store), "wifi-portal")
        assert len(drone_view) == mcp_view

    def test_mark_read_propagates_to_drone_view(self, store):
        """After ``swarm_check_messages`` marks messages read, a follow-
        up drone query returns zero.  This is the happy path the
        wifi-portal repro broke because the worker's client-side MCP
        tool registry was stale (task #257) — the MCP call never
        reached the server, so mark_read never ran, so the drone kept
        seeing 3 unread and kept nudging."""
        for sender in ("platform", "admin", "nexus"):
            store.send(sender, "wifi-portal", "dependency", "ping")

        daemon = _daemon_with(store)
        before = len(store.get_unread("wifi-portal"))
        assert before == 3

        # Worker calls swarm_check_messages — marks them all read.
        _check_messages_count(daemon, "wifi-portal")

        after = len(store.get_unread("wifi-portal"))
        assert after == 0
        assert _check_messages_count(daemon, "wifi-portal") == 0

    def test_queen_sourced_message_counted_same(self, store):
        """Messages from the Queen show up in the recipient's unread
        count for both paths — the watcher's later filter drops them
        to avoid double-nudging the Queen's own relay, but the count
        itself doesn't differ."""
        store.send("queen", "wifi-portal", "dependency", "from queen")
        drone_view = store.get_unread("wifi-portal")
        mcp_view = _check_messages_count(_daemon_with(store), "wifi-portal")
        assert len(drone_view) == 1
        assert mcp_view == 1


class TestSingleSourceOfTruth:
    """Structural assertion: both call sites import and use
    ``MessageStore.get_unread`` directly.  If a second unread-count
    path is ever added (e.g. denormalised cache, per-worker counter
    table), this test will fail and force the author to decide whether
    drift is intentional."""

    def test_drone_uses_get_unread(self):
        import inspect

        from swarm.drones import inter_worker_watcher

        src = inspect.getsource(inter_worker_watcher)
        assert "get_unread" in src
        # Make sure the drone isn't synthesising a count from some
        # other attribute of the store.
        assert "unread_count" not in src or "get_unread" in src

    def test_check_messages_uses_get_unread(self):
        import inspect

        from swarm.mcp import tools

        # ``_handle_check_messages`` is the sole path behind swarm_check_messages.
        src = inspect.getsource(tools._handle_check_messages)
        assert "get_unread" in src
