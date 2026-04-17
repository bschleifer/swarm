"""Metadata-quality tests for Swarm MCP tool definitions.

These tests ensure every tool exposed to workers has:
  - A rich description (>= 150 chars) — workers rely on descriptions to
    know *when* and *how* to call a tool, not just *what* it does.
  - An ``examples`` block in the inputSchema so workers can see a
    concrete payload.

Adding new MCP tools? Update them to meet this bar or update this test
with an intentional rationale.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from swarm.mcp.tools import TOOLS, handle_tool_call

MIN_DESCRIPTION_CHARS = 150


def test_every_tool_has_rich_description() -> None:
    thin = [t["name"] for t in TOOLS if len(t.get("description", "")) < MIN_DESCRIPTION_CHARS]
    assert not thin, (
        f"These MCP tools have descriptions under {MIN_DESCRIPTION_CHARS} chars "
        f"(workers need context on when/how to call): {thin}"
    )


def test_every_tool_has_examples() -> None:
    missing = [t["name"] for t in TOOLS if not t.get("inputSchema", {}).get("examples")]
    assert not missing, (
        f"These MCP tools lack an 'examples' field in inputSchema "
        f"(workers benefit from concrete payloads): {missing}"
    )


def test_examples_are_well_formed() -> None:
    """Each example must be a dict matching the tool's property shape."""
    for tool in TOOLS:
        schema = tool.get("inputSchema", {})
        examples = schema.get("examples") or []
        properties = schema.get("properties", {})
        required = set(schema.get("required", []))
        assert isinstance(examples, list), f"{tool['name']}: examples must be a list"
        assert examples, f"{tool['name']}: examples list is empty"
        for i, ex in enumerate(examples):
            assert isinstance(ex, dict), f"{tool['name']} example[{i}] must be dict"
            missing_required = required - ex.keys()
            assert not missing_required, (
                f"{tool['name']} example[{i}] missing required keys: {missing_required}"
            )
            unknown = ex.keys() - properties.keys()
            assert not unknown, f"{tool['name']} example[{i}] has keys not in schema: {unknown}"


def test_every_tool_description_explains_when() -> None:
    """Descriptions should include a 'when to call' hint — heuristic:
    contain one of a handful of trigger words."""
    trigger_words = ("when", "before", "after", "at the start", "use when", "call")
    weak = []
    for tool in TOOLS:
        desc = tool.get("description", "").lower()
        if not any(word in desc for word in trigger_words):
            weak.append(tool["name"])
    assert not weak, (
        f"These MCP tools' descriptions don't hint at *when* to call them "
        f"(include a trigger word like 'when', 'before', 'after', 'call at'): {weak}"
    )


# ---------------------------------------------------------------------------
# swarm_batch tool
# ---------------------------------------------------------------------------


@pytest.fixture
def batch_daemon():
    """Minimal daemon fake — buzz logger and message store as MagicMocks."""
    d = MagicMock()
    d.drone_log = MagicMock()
    d.message_store = MagicMock()
    d.message_store.send = MagicMock(return_value="msg-123")
    d.task_board = MagicMock()
    d.task_board.all_tasks = []
    return d


class TestSwarmBatch:
    def test_batch_is_registered(self) -> None:
        names = {t["name"] for t in TOOLS}
        assert "swarm_batch" in names

    def test_runs_ops_sequentially_and_collects_results(self, batch_daemon):
        result = handle_tool_call(
            batch_daemon,
            "api",
            "swarm_batch",
            {
                "ops": [
                    {"tool": "swarm_report_progress", "args": {"phase": "planning", "pct": 10}},
                    {"tool": "swarm_report_progress", "args": {"phase": "implementing", "pct": 50}},
                ]
            },
        )
        text = result[0]["text"]
        assert "Batch results" in text
        assert "[1/2] swarm_report_progress" in text
        assert "[2/2] swarm_report_progress" in text

    def test_rejects_unknown_tool(self, batch_daemon):
        result = handle_tool_call(
            batch_daemon,
            "api",
            "swarm_batch",
            {"ops": [{"tool": "swarm_does_not_exist", "args": {}}]},
        )
        text = result[0]["text"]
        assert "unknown tool" in text.lower()

    def test_rejects_nested_batch(self, batch_daemon):
        """swarm_batch inside swarm_batch is blocked to prevent runaway recursion."""
        result = handle_tool_call(
            batch_daemon,
            "api",
            "swarm_batch",
            {"ops": [{"tool": "swarm_batch", "args": {"ops": []}}]},
        )
        text = result[0]["text"]
        assert "nested" in text.lower() or "cannot" in text.lower()

    def test_fail_fast_stops_on_first_error(self, batch_daemon):
        result = handle_tool_call(
            batch_daemon,
            "api",
            "swarm_batch",
            {
                "ops": [
                    {"tool": "swarm_report_progress", "args": {"phase": "ok"}},
                    {"tool": "swarm_unknown", "args": {}},
                    {"tool": "swarm_report_progress", "args": {"phase": "never runs"}},
                ],
                "fail_fast": True,
            },
        )
        text = result[0]["text"]
        # Only two results recorded (first op + the failed one); third skipped
        assert "[1/3]" in text
        assert "[2/3]" in text
        assert "[3/3]" not in text
        assert "stopped" in text.lower() or "aborted" in text.lower()

    def test_continue_on_error_runs_all_ops(self, batch_daemon):
        result = handle_tool_call(
            batch_daemon,
            "api",
            "swarm_batch",
            {
                "ops": [
                    {"tool": "swarm_unknown", "args": {}},
                    {"tool": "swarm_report_progress", "args": {"phase": "after error"}},
                ],
                "fail_fast": False,
            },
        )
        text = result[0]["text"]
        assert "[1/2]" in text
        assert "[2/2]" in text

    def test_empty_ops_is_rejected(self, batch_daemon):
        result = handle_tool_call(batch_daemon, "api", "swarm_batch", {"ops": []})
        text = result[0]["text"]
        assert "at least one" in text.lower() or "empty" in text.lower()

    def test_missing_ops_is_rejected(self, batch_daemon):
        result = handle_tool_call(batch_daemon, "api", "swarm_batch", {})
        text = result[0]["text"]
        assert "ops" in text.lower()


# ---------------------------------------------------------------------------
# swarm_create_task cross-project attribution
# ---------------------------------------------------------------------------


class TestCreateTaskCrossProjectFields:
    """Regression for the bug where a worker calling swarm_create_task
    with ``target_worker`` set produced a task row in the DB with
    ``source_worker=""`` — cross-project attribution was lost because
    the MCP handler never propagated the calling worker's name into the
    task's ``source_worker`` column.
    """

    def _daemon(self, *, return_task_id: str = "new-task-id") -> MagicMock:
        """Daemon fake wired for create + edit + assign assertions."""
        d = MagicMock()
        d.drone_log = MagicMock()
        d.message_store = MagicMock()
        d.task_board = MagicMock()
        d.task_board.all_tasks = []
        fake_task = MagicMock()
        fake_task.id = return_task_id
        fake_task.number = 42
        d.create_task = MagicMock(return_value=fake_task)
        d.edit_task = MagicMock(return_value=True)
        d.assign_task = MagicMock()
        return d

    def test_cross_project_sets_source_and_target(self):
        d = self._daemon()
        result = handle_tool_call(
            d,
            "hub",  # calling worker — this is the SOURCE
            "swarm_create_task",
            {
                "title": "Fix tenant resolution in /api/v1/contacts",
                "target_worker": "platform",
            },
        )
        assert "created" in result[0]["text"].lower()

        d.edit_task.assert_called_once()
        call_kwargs = d.edit_task.call_args.kwargs
        assert call_kwargs["source_worker"] == "hub"
        assert call_kwargs["target_worker"] == "platform"
        assert call_kwargs["actor"] == "hub"

    def test_same_worker_target_skips_cross_project_edit(self):
        """Workers filing tasks for themselves aren't cross-project —
        don't spam the DB with a no-op edit and don't flip
        ``is_cross_project`` on."""
        d = self._daemon()
        handle_tool_call(
            d,
            "hub",
            "swarm_create_task",
            {"title": "Internal cleanup", "target_worker": "hub"},
        )
        d.edit_task.assert_not_called()

    def test_no_target_skips_edit(self):
        """Plain task creation with no target_worker shouldn't touch the
        cross-project plumbing at all."""
        d = self._daemon()
        handle_tool_call(d, "hub", "swarm_create_task", {"title": "Local fix"})
        d.edit_task.assert_not_called()

    def test_unknown_calling_worker_still_sets_target(self):
        """When the MCP query param didn't identify a real worker
        (``worker_name == "unknown"``), we can still record target
        attribution even though source is unattributable. Target is
        more valuable than skipping the whole thing."""
        d = self._daemon()
        handle_tool_call(
            d,
            "unknown",
            "swarm_create_task",
            {"title": "Cross-project from unattributed caller", "target_worker": "platform"},
        )
        d.edit_task.assert_called_once()
        call_kwargs = d.edit_task.call_args.kwargs
        assert call_kwargs["source_worker"] == ""  # unknown → blank source
        assert call_kwargs["target_worker"] == "platform"
