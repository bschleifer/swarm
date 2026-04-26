"""Tests for the SessionStart hook endpoint at /api/hooks/session-start.

The SessionStart hook injects a per-worker bootstrap (assigned task + unread
inter-worker messages) into the Claude Code session via ``additionalContext``,
so workers don't have to remember to call ``swarm_check_messages`` /
``swarm_task_status`` before starting work.

These tests pin the contract decided in the Wave 1 plan
(`~/.claude/plans/streamed-greeting-taco.md`):

* ``source == "resume"`` returns an empty bootstrap (transcript already has it).
* Unknown workers return an empty bootstrap (no crash).
* Known workers with an active task and/or unread messages get both inlined.
* Workers with neither return an empty bootstrap.
* Messages are capped at five with an overflow pointer to ``swarm_check_messages``.
* Messages stay **unread** in the store after the bootstrap (the dashboard
  badge must keep working).
* The bootstrap event is recorded in the drone log for audit/debug visibility.
"""

from __future__ import annotations

import pytest
from aiohttp.test_utils import TestClient, TestServer

from swarm.drones.log import LogCategory, SystemAction
from swarm.messages.store import MessageStore
from swarm.server.api import create_app
from swarm.tasks.task import SwarmTask, TaskStatus
from tests.conftest import make_daemon

# Hook endpoints are exempt from session auth (see _SESSION_AUTH_EXEMPT in
# server/api.py), but CSRF middleware still requires X-Requested-With.
_HOOK_HEADERS = {"X-Requested-With": "TestClient"}


@pytest.fixture
def daemon(monkeypatch, tmp_path):
    d = make_daemon(monkeypatch=monkeypatch)
    # The shared make_daemon factory doesn't wire a message store; the
    # SessionStart handler reads it via getattr(d, "message_store", None),
    # so we attach a real one backed by an isolated SQLite file.
    d.message_store = MessageStore(db_path=tmp_path / "messages.db")
    return d


@pytest.fixture
async def client(daemon):
    app = create_app(daemon, enable_web=False)
    async with TestClient(TestServer(app)) as c:
        yield c


def _post_session_start(client, **body):
    body.setdefault("hook_event_name", "SessionStart")
    body.setdefault("source", "startup")
    body.setdefault("cwd", "/tmp/api")
    return client.post("/api/hooks/session-start", json=body, headers=_HOOK_HEADERS)


async def _read_additional_context(resp) -> str:
    body = await resp.json()
    return body["hookSpecificOutput"]["additionalContext"]


def _assign_task(daemon, worker_name: str, **kwargs) -> SwarmTask:
    task = SwarmTask(
        title=kwargs.pop("title", "Fix the thing"),
        description=kwargs.pop("description", "Detailed description here"),
        status=TaskStatus.ASSIGNED,
        assigned_worker=worker_name,
        **kwargs,
    )
    daemon.task_board.add(task)
    return task


# ---------------------------------------------------------------------------
# Source filtering — resume must be a no-op
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resume_returns_empty_bootstrap_even_with_task_and_messages(client, daemon):
    """source == 'resume' must skip injection — the transcript already has it."""
    _assign_task(daemon, "api", title="Resume should not see me")
    daemon.message_store.send("web", "api", "finding", "ignored on resume")

    resp = await _post_session_start(client, source="resume")
    assert resp.status == 200
    assert await _read_additional_context(resp) == ""


# ---------------------------------------------------------------------------
# Unknown / no-state workers
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unknown_worker_returns_empty_bootstrap(client):
    """If cwd matches no worker, the bootstrap is empty (and 200)."""
    resp = await _post_session_start(client, cwd="/nowhere")
    assert resp.status == 200
    assert await _read_additional_context(resp) == ""


@pytest.mark.asyncio
async def test_known_worker_with_no_state_returns_empty_bootstrap(client):
    """A known worker with no task and no messages gets an empty bootstrap.

    We don't want to spam every fresh session with a redundant header that
    says ``Swarm Bootstrap`` followed by nothing useful.
    """
    resp = await _post_session_start(client)
    assert resp.status == 200
    assert await _read_additional_context(resp) == ""


@pytest.mark.asyncio
async def test_invalid_json_body_returns_empty_bootstrap(client):
    """Garbage JSON must NOT crash the worker — fail open with empty context."""
    resp = await client.post(
        "/api/hooks/session-start",
        data="not-json",
        headers={**_HOOK_HEADERS, "Content-Type": "application/json"},
    )
    assert resp.status == 200
    assert await _read_additional_context(resp) == ""


# ---------------------------------------------------------------------------
# Happy path: task + messages
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_known_worker_with_task_only_injects_task_block(client, daemon):
    _assign_task(
        daemon,
        "api",
        title="Repair the auth flow",
        description="Tokens are being dropped after refresh",
    )

    resp = await _post_session_start(client)
    assert resp.status == 200
    ctx = await _read_additional_context(resp)

    assert "## Swarm Bootstrap" in ctx
    assert "Repair the auth flow" in ctx
    assert "Tokens are being dropped after refresh" in ctx
    assert "assigned" in ctx  # status value
    assert "Unread messages" not in ctx


@pytest.mark.asyncio
async def test_known_worker_with_messages_only_injects_messages_block(client, daemon):
    daemon.message_store.send("web", "api", "warning", "Heads up — shared file changed")
    daemon.message_store.send("hub", "api", "finding", "Found a related bug in /admin")

    resp = await _post_session_start(client)
    assert resp.status == 200
    ctx = await _read_additional_context(resp)

    assert "Unread messages (2)" in ctx
    assert "Heads up — shared file changed" in ctx
    assert "Found a related bug in /admin" in ctx
    assert "Your assigned task" not in ctx


@pytest.mark.asyncio
async def test_known_worker_with_task_and_messages_injects_both(client, daemon):
    _assign_task(daemon, "api", title="Implement feature X")
    daemon.message_store.send("web", "api", "warning", "Don't touch shared.py")

    resp = await _post_session_start(client)
    ctx = await _read_additional_context(resp)

    assert "Implement feature X" in ctx
    assert "Don't touch shared.py" in ctx
    # Both blocks should be present in the same payload.
    assert ctx.index("Your assigned task") < ctx.index("Unread messages")


# ---------------------------------------------------------------------------
# Message capping & overflow pointer
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_messages_capped_at_five_with_overflow_pointer(client, daemon):
    """When >5 unread messages exist, only 5 are inlined and the rest get a pointer."""
    for i in range(8):
        # Use distinct (sender, msg_type) combos to bypass the 60s dedup window.
        daemon.message_store.send(f"sender{i}", "api", "finding", f"msg #{i}")

    resp = await _post_session_start(client)
    ctx = await _read_additional_context(resp)

    assert "Unread messages (8)" in ctx
    # All 5 oldest messages should appear (get_unread sorts ASC).
    for i in range(5):
        assert f"msg #{i}" in ctx
    # The 3 overflow messages should NOT appear inline.
    for i in range(5, 8):
        assert f"msg #{i}" not in ctx
    assert "and 3 more" in ctx
    assert "swarm_check_messages" in ctx


# ---------------------------------------------------------------------------
# Read state preservation — the dashboard badge must keep working
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_messages_remain_unread_after_bootstrap(client, daemon):
    """Showing a message in the bootstrap context is NOT acknowledgement.

    The worker still has to call swarm_check_messages to mark them read,
    so the dashboard's unread badge stays accurate.
    """
    daemon.message_store.send("web", "api", "warning", "still unread please")

    resp = await _post_session_start(client)
    assert resp.status == 200

    unread_after = daemon.message_store.get_unread("api")
    assert len(unread_after) == 1
    assert unread_after[0].read_at is None


# ---------------------------------------------------------------------------
# Audit log entry
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bootstrap_recorded_in_drone_log(client, daemon):
    """A SESSION_BOOTSTRAP entry must land in the drone log for visibility."""
    _assign_task(daemon, "api", title="Audit me")

    resp = await _post_session_start(client)
    assert resp.status == 200

    bootstrap_entries = [
        e
        for e in daemon.drone_log.entries
        if getattr(e.action, "name", "") == SystemAction.SESSION_BOOTSTRAP.name
    ]
    assert len(bootstrap_entries) == 1
    entry = bootstrap_entries[0]
    assert entry.worker_name == "api"
    assert entry.category == LogCategory.SYSTEM
    assert "session_start(startup)" in entry.detail


# ---------------------------------------------------------------------------
# Slash commands discoverability nudge
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_nudge_appended_when_task_present(client, daemon):
    """When a task is bootstrapped, the slash-commands nudge is appended."""
    _assign_task(daemon, "api", title="A real task")

    resp = await _post_session_start(client)
    ctx = await _read_additional_context(resp)

    # All six commands listed
    assert "/swarm-status" in ctx
    assert "/swarm-handoff" in ctx
    assert "/swarm-finding" in ctx
    assert "/swarm-warning" in ctx
    assert "/swarm-blocker" in ctx
    assert "/swarm-progress" in ctx
    # The nudge marker phrase
    assert "slash commands available" in ctx.lower()


@pytest.mark.asyncio
async def test_nudge_appended_when_messages_present(client, daemon):
    """When only messages are bootstrapped, the nudge still rides along."""
    daemon.message_store.send("hub", "api", "warning", "heads up")

    resp = await _post_session_start(client)
    ctx = await _read_additional_context(resp)

    assert "/swarm-status" in ctx
    assert "/swarm-progress" in ctx


@pytest.mark.asyncio
async def test_nudge_skipped_when_bootstrap_empty(client):
    """A worker with no task and no messages gets no bootstrap (and no nudge)."""
    resp = await _post_session_start(client)
    ctx = await _read_additional_context(resp)

    # No bootstrap content => no nudge either; fresh empty workers discover via /help
    assert ctx == ""
    assert "/swarm-status" not in ctx
