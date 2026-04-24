"""Tests for the worker-facing ``swarm_draft_email`` MCP tool.

The tool lets workers create a draft email in the operator's Outlook
Drafts folder via Microsoft Graph.  The draft is never auto-sent —
operator reviews + sends manually.  Mirrors the existing email-reply
flow that fires on email-sourced task completion, but initiated by the
worker on demand (e.g. "ask the stakeholder for schema clarification").

Handler is sync with fire-and-forget async Graph calls; tests exercise
validation synchronously and call the coroutine helper directly for the
success / failure paths.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from swarm.drones.log import DroneLog, LogCategory, SystemAction
from swarm.mcp.tools import handle_tool_call


@pytest.fixture()
def daemon():
    """Minimal daemon stand-in: drone_log + graph_mgr stub + broadcast."""
    d = MagicMock()
    d.drone_log = DroneLog()
    d.broadcast_ws = MagicMock()
    graph_mgr = MagicMock()
    graph_mgr.is_connected = MagicMock(return_value=True)
    graph_mgr.create_draft = AsyncMock(
        return_value={"id": "AAMkAD...", "web_link": "https://outlook.office.com/mail/deeplink/..."}
    )
    d.graph_mgr = graph_mgr
    return d


def _entries(daemon, *, action):
    return [e for e in daemon.drone_log.entries if e.action == action]


# ---------------------------------------------------------------------------
# Validation — synchronous, no event loop needed
# ---------------------------------------------------------------------------


class TestValidation:
    def test_missing_to_rejected(self, daemon):
        result = handle_tool_call(daemon, "w", "swarm_draft_email", {"subject": "s", "body": "b"})
        assert "Missing 'to'" in result[0]["text"]
        daemon.graph_mgr.create_draft.assert_not_awaited()

    def test_empty_to_list_rejected(self, daemon):
        result = handle_tool_call(
            daemon, "w", "swarm_draft_email", {"to": [], "subject": "s", "body": "b"}
        )
        assert "Missing 'to'" in result[0]["text"]
        daemon.graph_mgr.create_draft.assert_not_awaited()

    def test_non_string_to_entry_rejected(self, daemon):
        result = handle_tool_call(
            daemon, "w", "swarm_draft_email", {"to": [123], "subject": "s", "body": "b"}
        )
        assert "non-empty strings" in result[0]["text"]
        daemon.graph_mgr.create_draft.assert_not_awaited()

    def test_missing_subject_rejected(self, daemon):
        result = handle_tool_call(daemon, "w", "swarm_draft_email", {"to": ["a@b"], "body": "b"})
        assert "Missing 'subject'" in result[0]["text"]
        daemon.graph_mgr.create_draft.assert_not_awaited()

    def test_missing_body_rejected(self, daemon):
        result = handle_tool_call(daemon, "w", "swarm_draft_email", {"to": ["a@b"], "subject": "s"})
        assert "Missing 'body'" in result[0]["text"]
        daemon.graph_mgr.create_draft.assert_not_awaited()

    def test_invalid_body_type_rejected(self, daemon):
        result = handle_tool_call(
            daemon,
            "w",
            "swarm_draft_email",
            {"to": ["a@b"], "subject": "s", "body": "b", "body_type": "markdown"},
        )
        assert "body_type" in result[0]["text"]
        daemon.graph_mgr.create_draft.assert_not_awaited()


class TestIntegrationUnavailable:
    def test_graph_manager_missing(self, daemon):
        daemon.graph_mgr = None
        result = handle_tool_call(
            daemon, "w", "swarm_draft_email", {"to": ["a@b"], "subject": "s", "body": "b"}
        )
        assert "not connected" in result[0]["text"].lower()

    def test_graph_not_connected(self, daemon):
        daemon.graph_mgr.is_connected = MagicMock(return_value=False)
        result = handle_tool_call(
            daemon, "w", "swarm_draft_email", {"to": ["a@b"], "subject": "s", "body": "b"}
        )
        assert "not connected" in result[0]["text"].lower()
        daemon.graph_mgr.create_draft.assert_not_awaited()


# ---------------------------------------------------------------------------
# Success / failure — no running loop → sync fallback runs the coroutine
# inline so we can assert on drone_log entries deterministically.
# ---------------------------------------------------------------------------


class TestSuccess:
    def test_returns_queued_message_on_valid_args(self, daemon):
        result = handle_tool_call(
            daemon,
            "public-website",
            "swarm_draft_email",
            {
                "to": ["ops@example.com"],
                "subject": "schema Q",
                "body": "Should visibility replace is_published?",
                "reason": "blocking #301",
            },
        )
        assert len(result) == 1
        assert "queued" in result[0]["text"].lower()
        assert "ops@example.com" in result[0]["text"]

    def test_graph_called_with_correct_payload(self, daemon):
        handle_tool_call(
            daemon,
            "w",
            "swarm_draft_email",
            {
                "to": ["ops@example.com"],
                "subject": "subj",
                "body": "body",
            },
        )
        daemon.graph_mgr.create_draft.assert_awaited_once()
        call = daemon.graph_mgr.create_draft.await_args
        assert call.args[0] == ["ops@example.com"]
        assert call.args[1] == "subj"
        assert call.args[2] == "body"
        assert call.kwargs["cc"] is None
        assert call.kwargs["body_type"] == "text"

    def test_draft_ok_logged_after_success(self, daemon):
        handle_tool_call(
            daemon,
            "public-website",
            "swarm_draft_email",
            {
                "to": ["ops@example.com"],
                "subject": "subj",
                "body": "body",
                "reason": "blocking #301",
            },
        )
        ok = _entries(daemon, action=SystemAction.DRAFT_OK)
        assert len(ok) == 1
        assert ok[0].worker_name == "public-website"
        assert "ops@example.com" in ok[0].detail
        assert "blocking #301" in ok[0].detail
        assert ok[0].category == LogCategory.SYSTEM

    def test_html_body_type_passed_through(self, daemon):
        handle_tool_call(
            daemon,
            "w",
            "swarm_draft_email",
            {"to": ["a@b"], "subject": "s", "body": "<p>x</p>", "body_type": "html"},
        )
        assert daemon.graph_mgr.create_draft.await_args.kwargs["body_type"] == "html"

    def test_cc_list_passed_through(self, daemon):
        handle_tool_call(
            daemon,
            "w",
            "swarm_draft_email",
            {"to": ["a@b"], "cc": ["c@d", "e@f"], "subject": "s", "body": "b"},
        )
        assert daemon.graph_mgr.create_draft.await_args.kwargs["cc"] == ["c@d", "e@f"]


class TestFailure:
    def test_graph_returns_none_logs_draft_failed(self, daemon):
        """Graph API rejection (None return) → DRAFT_FAILED buzz entry."""
        daemon.graph_mgr.create_draft = AsyncMock(return_value=None)
        handle_tool_call(
            daemon,
            "w",
            "swarm_draft_email",
            {"to": ["a@b"], "subject": "s", "body": "b"},
        )
        failed = _entries(daemon, action=SystemAction.DRAFT_FAILED)
        assert len(failed) == 1
        assert failed[0].is_notification is True
        # DRAFT_OK should NOT have been written.
        assert not _entries(daemon, action=SystemAction.DRAFT_OK)


class TestGraphManagerCreateDraft:
    """Verifies ``GraphTokenManager.create_draft`` payload shape — the
    Graph API needs the exact body / toRecipients / ccRecipients format."""

    @pytest.mark.asyncio
    async def test_payload_shape(self, monkeypatch):
        from swarm.auth.graph import GraphTokenManager

        gm = GraphTokenManager(client_id="cid")
        gm._refresh_token = "rt"
        gm._access_token = "at"
        gm._expires_at = 9e18

        captured: dict = {}

        class _FakeResp:
            status = 201

            async def json(self):
                return {"id": "msg-1", "webLink": "https://outlook.office.com/..."}

            async def text(self):
                return ""

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return None

        class _FakeSession:
            def post(self, url, *, headers, json, timeout):
                captured["url"] = url
                captured["headers"] = headers
                captured["json"] = json
                return _FakeResp()

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return None

        monkeypatch.setattr("swarm.auth.graph.aiohttp.ClientSession", lambda: _FakeSession())

        result = await gm.create_draft(
            ["to@x.com"], "subj", "body", cc=["cc@x.com"], body_type="html"
        )
        assert result == {"id": "msg-1", "web_link": "https://outlook.office.com/..."}
        assert captured["url"].endswith("/me/messages")
        assert captured["headers"]["Authorization"] == "Bearer at"
        assert captured["json"]["subject"] == "subj"
        assert captured["json"]["body"] == {"contentType": "html", "content": "body"}
        assert captured["json"]["toRecipients"] == [{"emailAddress": {"address": "to@x.com"}}]
        assert captured["json"]["ccRecipients"] == [{"emailAddress": {"address": "cc@x.com"}}]
