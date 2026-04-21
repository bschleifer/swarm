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

import time
from unittest.mock import MagicMock

import pytest

from swarm.mcp import tools as tools_module
from swarm.mcp.tools import TOOLS, handle_tool_call, tools_source_drift
from swarm.tasks.task import SwarmTask, TaskStatus

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


# ---------------------------------------------------------------------------
# Task #225 Phase 1 — auto-dispatch on assignment
# ---------------------------------------------------------------------------


class TestCreateTaskAutoDispatch:
    """Phase 1 of task #225: ``swarm_create_task(target_worker=X)`` must
    push the task into X's PTY by default, not merely flip a DB column.

    The old behaviour called ``assign_task`` only — which queued the task
    in the ASSIGNED state but never sent the task body to the worker.
    That produced the operator-facing failure mode where workers sat on
    hours-old assigned tasks because nothing dispatched them.
    """

    def _daemon(self) -> MagicMock:
        # AsyncMock for the two daemon methods the handler schedules as
        # coroutines — so calling them returns an awaitable the handler
        # can hand to ``loop.create_task`` without a TypeError.
        from unittest.mock import AsyncMock

        d = MagicMock()
        d.drone_log = MagicMock()
        d.message_store = MagicMock()
        d.task_board = MagicMock()
        d.task_board.all_tasks = []
        fake_task = MagicMock()
        fake_task.id = "new-task-id"
        fake_task.number = 99
        d.create_task = MagicMock(return_value=fake_task)
        d.edit_task = MagicMock(return_value=True)
        d.assign_task = AsyncMock()
        d.assign_and_start_task = AsyncMock()
        return d

    def test_cross_worker_target_calls_assign_and_start_task(self):
        """Default behaviour: target set + no ``start`` arg → full dispatch."""
        d = self._daemon()
        handle_tool_call(
            d,
            "hub",
            "swarm_create_task",
            {"title": "Fix the thing", "target_worker": "platform"},
        )
        d.assign_and_start_task.assert_called_once()
        call_args = d.assign_and_start_task.call_args
        assert call_args.args[0] == "new-task-id"
        assert call_args.args[1] == "platform"
        # Legacy assign_task path is NOT taken when we dispatch.
        d.assign_task.assert_not_called()

    def test_start_false_preserves_queue_without_dispatch(self):
        """Explicit opt-out: ``start=False`` keeps the old queue-only
        behaviour so the Queen/operator can line up work without
        interrupting the target worker's current turn."""
        d = self._daemon()
        handle_tool_call(
            d,
            "hub",
            "swarm_create_task",
            {"title": "Queue this", "target_worker": "platform", "start": False},
        )
        d.assign_task.assert_called_once()
        d.assign_and_start_task.assert_not_called()

    def test_self_target_does_not_dispatch_to_same_session(self):
        """A worker filing a task against itself shouldn't inject the
        task body back into the same PTY that just filed it — the caller
        is already mid-turn. Queue it for later instead."""
        d = self._daemon()
        handle_tool_call(
            d,
            "hub",
            "swarm_create_task",
            {"title": "Note to self", "target_worker": "hub"},
        )
        d.assign_task.assert_called_once()
        d.assign_and_start_task.assert_not_called()

    def test_no_target_worker_leaves_task_unassigned(self):
        """No ``target_worker`` → neither path fires; task sits PENDING."""
        d = self._daemon()
        handle_tool_call(d, "hub", "swarm_create_task", {"title": "Just a note"})
        d.assign_task.assert_not_called()
        d.assign_and_start_task.assert_not_called()


# ---------------------------------------------------------------------------
# Task #235 Phase 1 — Queen inbox auto-relay on swarm_send_message
# ---------------------------------------------------------------------------


class TestSendMessageQueenAutoRelay:
    """When a worker sends a message TO the Queen, the handler must also
    push a short relay prompt into the Queen's PTY so her next turn sees
    the reply naturally — matching how workers get task dispatches in
    #225. Intra-worker messages do NOT auto-relay (that bypass is
    Queen-only by design; workers can't auto-interrupt each other).
    """

    def _daemon(self) -> MagicMock:
        from unittest.mock import AsyncMock

        d = MagicMock()
        d.drone_log = MagicMock()
        d.message_store = MagicMock()
        d.message_store.send = MagicMock(return_value="msg-1")
        d.message_store.broadcast = MagicMock(return_value=["msg-2", "msg-3"])
        d.send_to_worker = AsyncMock()
        # Two-worker roster: queen + hub.
        wk1 = MagicMock()
        wk1.name = "queen"
        wk2 = MagicMock()
        wk2.name = "hub"
        d.config = MagicMock()
        d.config.workers = [wk1, wk2]
        return d

    def test_message_to_queen_auto_relays_to_queen_pty(self):
        d = self._daemon()
        handle_tool_call(
            d,
            "hub",
            "swarm_send_message",
            {"to": "queen", "type": "finding", "content": "Stats are ready."},
        )
        # Message persisted as before.
        d.message_store.send.assert_called_once()
        # AND a relay notification was fired into the Queen's PTY.
        d.send_to_worker.assert_called_once()
        args, kwargs = d.send_to_worker.call_args
        assert args[0] == "queen"
        relay_text = args[1]
        assert "hub" in relay_text  # sender cited
        assert "finding" in relay_text.lower() or "FINDING" in relay_text
        assert kwargs.get("_log_operator") is False

    def test_message_to_regular_worker_does_not_auto_relay(self):
        """Worker A → worker B must NOT inject into B's PTY. Workers
        don't get elevated interruption rights — only the Queen does."""
        d = self._daemon()
        handle_tool_call(
            d,
            "hub",
            "swarm_send_message",
            {"to": "platform", "type": "warning", "content": "Watch out."},
        )
        d.message_store.send.assert_called_once()
        d.send_to_worker.assert_not_called()

    def test_queen_messaging_herself_does_not_relay(self):
        """Defensive: queen → queen would otherwise self-loop a PTY
        prompt on every self-message. Skip the auto-relay."""
        d = self._daemon()
        handle_tool_call(
            d,
            "queen",
            "swarm_send_message",
            {"to": "queen", "type": "status", "content": "note-to-self"},
        )
        d.message_store.send.assert_called_once()
        d.send_to_worker.assert_not_called()

    def test_broadcast_that_includes_queen_auto_relays_to_her(self):
        """``to="*"`` broadcasts hit every worker incl. queen. The queen
        still gets the relay so the broadcast doesn't silently sit in
        her inbox."""
        d = self._daemon()
        handle_tool_call(
            d,
            "hub",
            "swarm_send_message",
            {"to": "*", "type": "finding", "content": "Heads up everyone"},
        )
        d.message_store.broadcast.assert_called_once()
        # Queen is in the configured roster so she gets the relay.
        d.send_to_worker.assert_called_once()
        assert d.send_to_worker.call_args.args[0] == "queen"


# ---------------------------------------------------------------------------
# swarm_task_status — pagination / ordering (regression for task #142)
# ---------------------------------------------------------------------------


def _task(
    number: int,
    *,
    title: str | None = None,
    status: TaskStatus = TaskStatus.PENDING,
    assigned: str | None = None,
    created_at: float | None = None,
    completed_at: float | None = None,
) -> SwarmTask:
    t = SwarmTask(
        title=title or f"Task {number}",
        status=status,
        assigned_worker=assigned,
        created_at=created_at if created_at is not None else time.time() + number,
        completed_at=completed_at,
    )
    t.number = number
    return t


class TestTaskStatusPagination:
    """Regression for task #142 — tool capped output at 20 oldest tasks,
    so newer assignments to a worker were invisible via MCP."""

    def _daemon(self, tasks: list[SwarmTask]) -> MagicMock:
        d = MagicMock()
        d.task_board = MagicMock()
        d.task_board.all_tasks = tasks
        return d

    def test_mine_filter_surfaces_newer_assignments_over_old(self):
        """The original bug: ~20 old completed tasks hid newer open ones."""
        tasks = [
            _task(i, status=TaskStatus.COMPLETED, assigned="platform", completed_at=1000.0 + i)
            for i in range(1, 25)
        ]
        # Freshly assigned, but higher number than the 20-row old window.
        tasks.append(_task(142, status=TaskStatus.ASSIGNED, assigned="platform"))

        result = handle_tool_call(
            self._daemon(tasks), "platform", "swarm_task_status", {"filter": "mine"}
        )
        text = result[0]["text"]
        assert "#142" in text, "open assignment must be visible via filter=mine"

    def test_mine_hides_completed_by_default(self):
        tasks = [
            _task(1, status=TaskStatus.COMPLETED, assigned="platform", completed_at=100.0),
            _task(2, status=TaskStatus.ASSIGNED, assigned="platform"),
        ]
        text = handle_tool_call(
            self._daemon(tasks), "platform", "swarm_task_status", {"filter": "mine"}
        )[0]["text"]
        assert "#2" in text
        assert "#1" not in text

    def test_mine_include_completed_shows_all(self):
        tasks = [
            _task(1, status=TaskStatus.COMPLETED, assigned="platform", completed_at=100.0),
            _task(2, status=TaskStatus.ASSIGNED, assigned="platform"),
        ]
        text = handle_tool_call(
            self._daemon(tasks),
            "platform",
            "swarm_task_status",
            {"filter": "mine", "include_completed": True},
        )[0]["text"]
        assert "#1" in text
        assert "#2" in text

    def test_lookup_by_number(self):
        tasks = [_task(i, assigned="platform") for i in range(1, 30)]
        tasks.append(_task(142, title="The needle", assigned="platform"))
        text = handle_tool_call(
            self._daemon(tasks), "platform", "swarm_task_status", {"number": 142}
        )[0]["text"]
        assert "#142" in text
        assert "The needle" in text
        # other tasks must not be included in a single-number lookup
        assert "#1 " not in text

    def test_lookup_by_number_missing(self):
        text = handle_tool_call(
            self._daemon([]), "platform", "swarm_task_status", {"number": 9999}
        )[0]["text"]
        assert "9999" in text
        assert "no task" in text.lower()

    def test_limit_clamps_and_reports_truncation(self):
        tasks = [_task(i, status=TaskStatus.PENDING) for i in range(1, 101)]
        text = handle_tool_call(self._daemon(tasks), "platform", "swarm_task_status", {"limit": 5})[
            0
        ]["text"]
        # Truncation footer present
        assert "more not shown" in text
        assert "total=100" in text
        # Only 5 task rows shown
        task_lines = [ln for ln in text.splitlines() if ln.startswith("#")]
        assert len(task_lines) == 5

    def test_open_tasks_sort_before_completed(self):
        tasks = [
            _task(1, status=TaskStatus.COMPLETED, completed_at=999.0),
            _task(2, status=TaskStatus.PENDING),
        ]
        text = handle_tool_call(
            self._daemon(tasks), "platform", "swarm_task_status", {"filter": "all"}
        )[0]["text"]
        lines = [ln for ln in text.splitlines() if ln.startswith("#")]
        assert lines[0].startswith("#2 "), "open task must come before completed"

    def test_invalid_limit_reports_error(self):
        text = handle_tool_call(
            self._daemon([]), "platform", "swarm_task_status", {"limit": "abc"}
        )[0]["text"]
        assert "Invalid 'limit'" in text


# ---------------------------------------------------------------------------
# swarm_complete_task — disambiguation (regression for task #169)
# ---------------------------------------------------------------------------


class TestCompleteTaskDisambiguation:
    """Regression for task #169 — when a worker had multiple in_progress
    assignments, ``swarm_complete_task`` with no ``number`` picked the
    wrong task (iteration order), silently closing an unrelated task and
    attaching the resolution to the wrong record."""

    def _daemon(self, tasks: list[SwarmTask]) -> MagicMock:
        d = MagicMock()
        d.task_board = MagicMock()
        d.task_board.all_tasks = tasks
        d.complete_task = MagicMock(return_value=True)
        return d

    def _call(
        self, daemon: MagicMock, args: dict[str, object] | None = None
    ) -> tuple[str, MagicMock]:
        args = {"resolution": "fix for task A"} if args is None else args
        result = handle_tool_call(daemon, "platform", "swarm_complete_task", args)
        return result[0]["text"], daemon.complete_task

    def test_singular_task_no_number_closes_it(self):
        """Legacy happy path — single in_progress assignment still auto-closes."""
        tasks = [_task(42, status=TaskStatus.IN_PROGRESS, assigned="platform")]
        d = self._daemon(tasks)
        text, complete = self._call(d)
        assert "#42" in text and "completed" in text.lower()
        complete.assert_called_once()
        assert complete.call_args.kwargs.get("resolution") == "fix for task A"

    def test_multiple_in_progress_without_number_errors(self):
        """Two+ in_progress tasks and no ``number`` → must error, not guess."""
        tasks = [
            _task(100, status=TaskStatus.IN_PROGRESS, assigned="platform"),
            _task(142, status=TaskStatus.IN_PROGRESS, assigned="platform"),
            _task(200, status=TaskStatus.IN_PROGRESS, assigned="platform"),
        ]
        d = self._daemon(tasks)
        text, complete = self._call(d)
        complete.assert_not_called()
        # Error must list the candidate numbers so the worker can retry.
        assert "#100" in text
        assert "#142" in text
        assert "#200" in text
        assert "number" in text.lower()

    def test_with_number_closes_that_specific_task(self):
        """When the worker passes ``number``, exactly that task closes — not
        whichever one the all_tasks iterator yields first."""
        tasks = [
            _task(100, status=TaskStatus.IN_PROGRESS, assigned="platform"),
            _task(142, status=TaskStatus.IN_PROGRESS, assigned="platform"),
        ]
        d = self._daemon(tasks)
        text, complete = self._call(d, {"resolution": "fix #142", "number": 142})
        assert "#142" in text and "completed" in text.lower()
        complete.assert_called_once()
        # The id passed to complete_task must belong to task #142, not #100.
        completed_id = complete.call_args.args[0]
        assert completed_id == tasks[1].id

    def test_with_number_not_assigned_to_caller_errors(self):
        tasks = [
            _task(142, status=TaskStatus.IN_PROGRESS, assigned="other-worker"),
        ]
        d = self._daemon(tasks)
        text, complete = self._call(d, {"resolution": "oops", "number": 142})
        complete.assert_not_called()
        assert "#142" in text
        assert "not assigned" in text.lower() or "not your" in text.lower()

    def test_with_number_not_active_errors(self):
        tasks = [
            _task(
                142,
                status=TaskStatus.COMPLETED,
                assigned="platform",
                completed_at=999.0,
            ),
        ]
        d = self._daemon(tasks)
        text, complete = self._call(d, {"resolution": "double-closed", "number": 142})
        complete.assert_not_called()
        assert "#142" in text
        assert "not in progress" in text.lower() or "not active" in text.lower()

    def test_with_number_not_found_errors(self):
        d = self._daemon([])
        text, complete = self._call(d, {"resolution": "ghost", "number": 9999})
        complete.assert_not_called()
        assert "9999" in text

    def test_no_active_task_at_all_still_errors(self):
        """Worker with no active assignments, no number → clear error."""
        d = self._daemon([])
        text, complete = self._call(d)
        complete.assert_not_called()
        assert "no active task" in text.lower()


# ---------------------------------------------------------------------------
# tools_source_drift — surfacing reload-needed state to the dashboard
# ---------------------------------------------------------------------------


class TestToolsSourceDrift:
    """Drift detection lets the dashboard flag the Reload button when
    ``tools.py`` has been edited since the daemon started — otherwise the
    running MCP server keeps publishing the old ``tools/list`` schema and
    fixes like task #169 sit unapplied in live worker sessions."""

    def test_no_drift_at_import_time(self):
        """Freshly imported module has matching startup and current hashes."""
        result = tools_source_drift()
        assert result["drift"] is False
        assert result["startup_hash"] == result["current_hash"]
        assert result["startup_hash"]  # non-empty (file was readable)
        assert result["source_path"].endswith("tools.py")

    def test_drift_detected_when_startup_hash_differs(self, monkeypatch):
        """Simulate a post-import edit by swapping the frozen startup hash."""
        monkeypatch.setattr(tools_module, "_SOURCE_HASH_AT_IMPORT", "deadbeef" * 8)
        result = tools_source_drift()
        assert result["drift"] is True
        assert result["startup_hash"] == "deadbeef" * 8
        assert result["current_hash"] != result["startup_hash"]

    def test_unreadable_source_reports_no_drift(self, monkeypatch, tmp_path):
        """If tools.py can't be read (e.g. deleted), don't false-positive."""
        monkeypatch.setattr(tools_module, "_SOURCE_PATH", tmp_path / "missing.py")
        result = tools_source_drift()
        assert result["drift"] is False
        assert result["current_hash"] == ""


# ---------------------------------------------------------------------------
# Queen-only MCP tools — read-only introspection surface
# ---------------------------------------------------------------------------


@pytest.fixture
def queen_daemon(tmp_path):
    """Fake daemon exposing the minimum surface the queen_view_* handlers use."""
    from pathlib import Path

    from swarm.db.core import SwarmDB
    from swarm.db.queen_chat_store import QueenChatStore
    from swarm.worker.worker import QUEEN_WORKER_NAME, Worker, WorkerState

    d = MagicMock()
    d.drone_log = MagicMock()
    d.swarm_db = SwarmDB(Path(tmp_path) / "q.db")
    d.queen_chat = QueenChatStore(d.swarm_db)
    # Capture WS broadcasts so conversation-tool tests can assert them.
    d._ws_events = []
    d.broadcast_ws = MagicMock(side_effect=lambda payload: d._ws_events.append(payload))
    # Two workers: queen herself + a regular worker.
    queen_w = Worker(name=QUEEN_WORKER_NAME, path="/tmp/q", kind="queen")
    queen_w.state = WorkerState.RESTING
    hub_w = Worker(name="hub", path="/tmp/hub")
    hub_w.state = WorkerState.BUZZING
    hub_w.context_pct = 0.42
    d.workers = [queen_w, hub_w]
    d.task_board = MagicMock()
    d.task_board.all_tasks = []
    d.task_board.active_tasks_for_worker = MagicMock(return_value=[])
    return d


class TestQueenReadOnlyTools:
    def test_non_queen_caller_gets_permission_denied(self, queen_daemon):
        result = handle_tool_call(queen_daemon, "hub", "queen_view_task_board", {})
        assert "Permission denied" in result[0]["text"]

    def test_queen_view_worker_state_summary(self, queen_daemon):
        result = handle_tool_call(queen_daemon, "queen", "queen_view_worker_state", {})
        text = result[0]["text"]
        assert "queen (queen)" in text
        assert "hub" in text

    def test_queen_view_worker_state_unknown_worker(self, queen_daemon):
        result = handle_tool_call(
            queen_daemon, "queen", "queen_view_worker_state", {"worker": "ghost"}
        )
        assert "not found" in result[0]["text"].lower()

    def test_queen_view_task_board_empty(self, queen_daemon):
        result = handle_tool_call(queen_daemon, "queen", "queen_view_task_board", {})
        assert "no tasks" in result[0]["text"].lower()

    def test_queen_view_messages_empty(self, queen_daemon):
        result = handle_tool_call(
            queen_daemon, "queen", "queen_view_messages", {"since_seconds": 60}
        )
        assert "no messages" in result[0]["text"].lower()

    def test_queen_view_message_stream_empty(self, queen_daemon):
        """No messages in the window → graceful empty response."""
        result = handle_tool_call(
            queen_daemon, "queen", "queen_view_message_stream", {"since_seconds": 60}
        )
        assert "no messages" in result[0]["text"].lower()

    def test_queen_view_message_stream_actionable_filter(self, queen_daemon):
        """Task #235 Phase 2: ``actionable_only=true`` must filter to
        unread messages whose recipient is currently RESTING/SLEEPING/
        STUNG. In this fixture ``hub`` is BUZZING, so hub-bound
        messages should be filtered out of the actionable view but
        still visible in the raw view.
        """
        import time as _t

        from swarm.messages.store import MessageStore

        store = MessageStore(swarm_db=queen_daemon.swarm_db)
        # Two inbound messages to hub (BUZZING) — different msg_types so
        # the 60s dedup window doesn't merge them into one row. One read,
        # one unread.
        unread_id = store.send("platform", "hub", "finding", "Hey, unread")
        read_id = store.send("platform", "hub", "warning", "Hey, read")
        # Mark only the warning as read.
        store.mark_read("hub", [read_id])
        assert unread_id and read_id and unread_id != read_id

        # Raw view: both show up regardless of hub's BUZZING state.
        raw = handle_tool_call(
            queen_daemon, "queen", "queen_view_messages", {"since_seconds": 3600}
        )
        assert "unread" in raw[0]["text"].lower()

        # Actionable view: hub is BUZZING → both are filtered out.
        actionable = handle_tool_call(
            queen_daemon,
            "queen",
            "queen_view_message_stream",
            {"since_seconds": 3600, "actionable_only": True},
        )
        assert "no actionable" in actionable[0]["text"].lower()

        # Flip hub to RESTING — the unread one should now be actionable;
        # the read one must still be excluded.
        from swarm.worker.worker import WorkerState

        for w in queen_daemon.workers:
            if w.name == "hub":
                w.state = WorkerState.RESTING
                w.state_since = _t.time()  # fresh RESTING, not SLEEPING yet

        actionable = handle_tool_call(
            queen_daemon,
            "queen",
            "queen_view_message_stream",
            {"since_seconds": 3600, "actionable_only": True},
        )
        text = actionable[0]["text"]
        assert "Hey, unread" in text
        assert "Hey, read" not in text
        assert "UNREAD" in text
        assert "RESTING" in text

    def test_queen_view_message_stream_requires_queen(self, queen_daemon):
        """Non-queen callers must hit the permission gate."""
        result = handle_tool_call(
            queen_daemon, "hub", "queen_view_message_stream", {"since_seconds": 60}
        )
        assert "Permission denied" in result[0]["text"]

    def test_queen_view_buzz_log_empty(self, queen_daemon):
        result = handle_tool_call(
            queen_daemon, "queen", "queen_view_buzz_log", {"since_seconds": 60}
        )
        assert "no buzz" in result[0]["text"].lower()

    def test_queen_view_drone_actions_empty(self, queen_daemon):
        result = handle_tool_call(
            queen_daemon, "queen", "queen_view_drone_actions", {"since_seconds": 60}
        )
        assert "no recent" in result[0]["text"].lower()

    def test_queen_query_learnings_returns_recorded(self, queen_daemon):
        queen_daemon.queen_chat.add_learning(
            context="wrong call",
            correction="next time, ask",
            applied_to="oversight",
        )
        result = handle_tool_call(
            queen_daemon, "queen", "queen_query_learnings", {"applied_to": "oversight"}
        )
        assert "oversight" in result[0]["text"]
        assert "next time, ask" in result[0]["text"]

    def test_queen_query_learnings_gate_still_applies(self, queen_daemon):
        result = handle_tool_call(queen_daemon, "impostor", "queen_query_learnings", {})
        assert "Permission denied" in result[0]["text"]


class TestQueenConversationTools:
    """queen_post_thread / queen_reply / queen_update_thread / queen_save_learning."""

    def test_post_thread_creates_and_broadcasts(self, queen_daemon):
        result = handle_tool_call(
            queen_daemon,
            "queen",
            "queen_post_thread",
            {"title": "Hub stuck", "body": "Been BUZZING for 18m.", "worker": "hub"},
        )
        assert "Thread posted" in result[0]["text"]
        threads = queen_daemon.queen_chat.list_threads()
        assert len(threads) == 1
        assert threads[0].title == "Hub stuck"
        assert threads[0].worker_name == "hub"
        events = [e["type"] for e in queen_daemon._ws_events]
        assert "queen.thread" in events
        assert "queen.message" in events

    def test_post_thread_rejects_missing_fields(self, queen_daemon):
        result = handle_tool_call(queen_daemon, "queen", "queen_post_thread", {"title": "x"})
        assert "Missing required" in result[0]["text"]

    def test_post_thread_requires_queen(self, queen_daemon):
        result = handle_tool_call(
            queen_daemon, "hub", "queen_post_thread", {"title": "x", "body": "y"}
        )
        assert "Permission denied" in result[0]["text"]

    def test_reply_operator_alias_lazy_creates_thread(self, queen_daemon):
        result = handle_tool_call(
            queen_daemon,
            "queen",
            "queen_reply",
            {"thread_id": "operator", "body": "Everyone's idle."},
        )
        assert "Reply posted" in result[0]["text"]
        threads = queen_daemon.queen_chat.list_threads(kind="operator")
        assert len(threads) == 1
        msgs = queen_daemon.queen_chat.list_messages(threads[0].id)
        assert msgs[0].content == "Everyone's idle."

    def test_reply_rejects_resolved_thread(self, queen_daemon):
        t = queen_daemon.queen_chat.create_thread(title="done", kind="operator")
        queen_daemon.queen_chat.resolve_thread(t.id, resolved_by="operator")
        result = handle_tool_call(
            queen_daemon, "queen", "queen_reply", {"thread_id": t.id, "body": "late"}
        )
        assert "resolved" in result[0]["text"].lower()

    def test_reply_unknown_thread(self, queen_daemon):
        result = handle_tool_call(
            queen_daemon,
            "queen",
            "queen_reply",
            {"thread_id": "bogus-id", "body": "x"},
        )
        assert "unknown" in result[0]["text"].lower()

    def test_update_thread_resolves_and_broadcasts(self, queen_daemon):
        t = queen_daemon.queen_chat.create_thread(title="resolvable")
        queen_daemon._ws_events.clear()
        result = handle_tool_call(
            queen_daemon,
            "queen",
            "queen_update_thread",
            {"thread_id": t.id, "status": "resolved", "reason": "approved"},
        )
        assert "resolved" in result[0]["text"].lower()
        fetched = queen_daemon.queen_chat.get_thread(t.id)
        assert fetched.status == "resolved"
        assert fetched.resolved_by == "queen"
        events = [e for e in queen_daemon._ws_events if e["type"] == "queen.thread"]
        assert events and events[-1]["event"] == "resolved"

    def test_update_thread_rejects_non_resolved_status(self, queen_daemon):
        t = queen_daemon.queen_chat.create_thread(title="x")
        result = handle_tool_call(
            queen_daemon,
            "queen",
            "queen_update_thread",
            {"thread_id": t.id, "status": "archived"},
        )
        assert "Only status='resolved'" in result[0]["text"]

    def test_save_learning_persists(self, queen_daemon):
        result = handle_tool_call(
            queen_daemon,
            "queen",
            "queen_save_learning",
            {
                "context": "wrong assumption",
                "correction": "ask first",
                "applied_to": "oversight",
            },
        )
        assert "Learning saved" in result[0]["text"]
        learnings = queen_daemon.queen_chat.query_learnings()
        assert len(learnings) == 1
        assert learnings[0].applied_to == "oversight"

    def test_save_learning_rejects_missing(self, queen_daemon):
        result = handle_tool_call(
            queen_daemon, "queen", "queen_save_learning", {"context": "only this"}
        )
        assert "Missing required" in result[0]["text"]


# ---------------------------------------------------------------------------
# Queen write-side action tools — reassign, interrupt, force-complete
# ---------------------------------------------------------------------------


@pytest.fixture
def queen_action_daemon(tmp_path):
    """Queen-fixture with a real TaskBoard + minimal complete_task/worker_svc mocks."""
    from pathlib import Path
    from unittest.mock import AsyncMock, MagicMock

    from swarm.db.core import SwarmDB
    from swarm.db.queen_chat_store import QueenChatStore
    from swarm.tasks.board import TaskBoard
    from swarm.tasks.task import SwarmTask, TaskPriority
    from swarm.worker.worker import QUEEN_WORKER_NAME, Worker, WorkerState

    d = MagicMock()
    d.drone_log = MagicMock()
    d.swarm_db = SwarmDB(Path(tmp_path) / "q.db")
    d.queen_chat = QueenChatStore(d.swarm_db)
    d._ws_events = []
    d.broadcast_ws = MagicMock(side_effect=lambda payload: d._ws_events.append(payload))

    board = TaskBoard()
    task = SwarmTask(title="Example task", priority=TaskPriority.NORMAL, number=42)
    board.add(task)
    d.task_board = board

    # Workers: queen + hub + platform
    d.workers = [
        Worker(name=QUEEN_WORKER_NAME, path="/tmp/q", kind="queen"),
        Worker(name="hub", path="/tmp/hub", state=WorkerState.BUZZING),
        Worker(name="platform", path="/tmp/platform", state=WorkerState.RESTING),
    ]

    # Async daemon methods the handlers fire-and-forget
    d.complete_task = MagicMock(return_value=True)
    d.assign_and_start_task = AsyncMock(return_value=True)
    d.worker_svc = MagicMock()
    d.worker_svc.interrupt_worker = AsyncMock()
    return d, task


class TestQueenReassignTask:
    def test_requires_queen(self, queen_action_daemon):
        d, task = queen_action_daemon
        result = handle_tool_call(
            d,
            "hub",
            "queen_reassign_task",
            {"number": task.number, "to_worker": "platform", "reason": "x"},
        )
        assert "Permission denied" in result[0]["text"]

    def test_reassigns_assigned_task(self, queen_action_daemon):
        d, task = queen_action_daemon
        d.task_board.assign(task.id, "hub")
        result = handle_tool_call(
            d,
            "queen",
            "queen_reassign_task",
            {"number": 42, "to_worker": "platform", "reason": "hub overloaded"},
        )
        assert "Reassigned" in result[0]["text"]
        assert d.task_board.get(task.id).assigned_worker == "platform"

    def test_requires_reason(self, queen_action_daemon):
        d, task = queen_action_daemon
        d.task_board.assign(task.id, "hub")
        result = handle_tool_call(
            d,
            "queen",
            "queen_reassign_task",
            {"number": 42, "to_worker": "platform"},
        )
        assert "reason" in result[0]["text"].lower()

    def test_rejects_unknown_task(self, queen_action_daemon):
        d, _ = queen_action_daemon
        result = handle_tool_call(
            d,
            "queen",
            "queen_reassign_task",
            {"number": 9999, "to_worker": "platform", "reason": "x"},
        )
        assert "No task with number" in result[0]["text"]

    def test_no_op_when_already_on_target(self, queen_action_daemon):
        d, task = queen_action_daemon
        d.task_board.assign(task.id, "platform")
        result = handle_tool_call(
            d,
            "queen",
            "queen_reassign_task",
            {"number": 42, "to_worker": "platform", "reason": "x"},
        )
        assert "already assigned" in result[0]["text"]


class TestQueenInterruptWorker:
    def test_requires_queen(self, queen_action_daemon):
        d, _ = queen_action_daemon
        result = handle_tool_call(
            d,
            "hub",
            "queen_interrupt_worker",
            {"worker": "hub", "reason": "x"},
        )
        assert "Permission denied" in result[0]["text"]

    def test_interrupts_worker(self, queen_action_daemon):
        d, _ = queen_action_daemon
        result = handle_tool_call(
            d,
            "queen",
            "queen_interrupt_worker",
            {"worker": "hub", "reason": "stuck for 20m"},
        )
        assert "Interrupt sent to hub" in result[0]["text"]
        d.worker_svc.interrupt_worker.assert_called_once_with("hub")

    def test_refuses_to_interrupt_queen(self, queen_action_daemon):
        d, _ = queen_action_daemon
        result = handle_tool_call(
            d,
            "queen",
            "queen_interrupt_worker",
            {"worker": "queen", "reason": "x"},
        )
        assert "Refusing" in result[0]["text"]

    def test_requires_reason(self, queen_action_daemon):
        d, _ = queen_action_daemon
        result = handle_tool_call(
            d,
            "queen",
            "queen_interrupt_worker",
            {"worker": "hub"},
        )
        assert "reason" in result[0]["text"].lower()

    def test_rejects_unknown_worker(self, queen_action_daemon):
        d, _ = queen_action_daemon
        result = handle_tool_call(
            d,
            "queen",
            "queen_interrupt_worker",
            {"worker": "ghost", "reason": "x"},
        )
        assert "not found" in result[0]["text"].lower()


class TestQueenForceCompleteTask:
    def test_requires_queen(self, queen_action_daemon):
        d, _ = queen_action_daemon
        result = handle_tool_call(
            d,
            "hub",
            "queen_force_complete_task",
            {"number": 42, "resolution": "r", "reason": "x"},
        )
        assert "Permission denied" in result[0]["text"]

    def test_force_completes(self, queen_action_daemon):
        d, task = queen_action_daemon
        d.task_board.assign(task.id, "hub")
        result = handle_tool_call(
            d,
            "queen",
            "queen_force_complete_task",
            {
                "number": 42,
                "resolution": "Fixed the thing; confirmed via /check",
                "reason": "worker went silent",
            },
        )
        assert "Force-completed" in result[0]["text"]
        d.complete_task.assert_called_once()
        args, kwargs = d.complete_task.call_args
        assert kwargs.get("actor") == "queen"
        assert kwargs.get("resolution", "").startswith("Fixed the thing")

    def test_requires_reason(self, queen_action_daemon):
        d, _ = queen_action_daemon
        result = handle_tool_call(
            d,
            "queen",
            "queen_force_complete_task",
            {"number": 42, "resolution": "done"},
        )
        assert "reason" in result[0]["text"].lower()

    def test_requires_resolution(self, queen_action_daemon):
        d, _ = queen_action_daemon
        result = handle_tool_call(
            d,
            "queen",
            "queen_force_complete_task",
            {"number": 42, "reason": "x"},
        )
        assert "resolution" in result[0]["text"].lower()


class TestQueenPromptWorker:
    def test_requires_queen(self, queen_action_daemon):
        d, _ = queen_action_daemon
        result = handle_tool_call(
            d,
            "hub",
            "queen_prompt_worker",
            {"worker": "platform", "prompt": "hello", "reason": "x"},
        )
        assert "Permission denied" in result[0]["text"]

    def test_prompts_resting_worker(self, queen_action_daemon):
        d, _ = queen_action_daemon
        result = handle_tool_call(
            d,
            "queen",
            "queen_prompt_worker",
            {"worker": "platform", "prompt": "run /check", "reason": "pre-commit"},
        )
        assert "Prompt sent to platform" in result[0]["text"]
        d.worker_svc.send_to_worker.assert_called_once()
        args, kwargs = d.worker_svc.send_to_worker.call_args
        assert args[0] == "platform"
        assert args[1] == "run /check"

    def test_buzzing_worker_queues_not_refused(self, queen_action_daemon):
        """BUZZING target is allowed — Claude queues the prompt to next turn."""
        d, _ = queen_action_daemon
        result = handle_tool_call(
            d,
            "queen",
            "queen_prompt_worker",
            {"worker": "hub", "prompt": "stop", "reason": "rate limit"},
        )
        text = result[0]["text"]
        assert "Prompt sent to hub" in text
        assert "queued for next turn" in text
        d.worker_svc.send_to_worker.assert_called_once()

    def test_refuses_stung_worker(self, queen_action_daemon):
        """STUNG = dead process; no queue path, must revive first."""
        from swarm.worker.worker import WorkerState

        d, _ = queen_action_daemon
        # Mutate hub into STUNG for this test
        for w in d.workers:
            if w.name == "hub":
                w.state = WorkerState.STUNG
        result = handle_tool_call(
            d,
            "queen",
            "queen_prompt_worker",
            {"worker": "hub", "prompt": "x", "reason": "y"},
        )
        assert "STUNG" in result[0]["text"]
        assert "revive" in result[0]["text"].lower()
        d.worker_svc.send_to_worker.assert_not_called()

    def test_refuses_self_target(self, queen_action_daemon):
        d, _ = queen_action_daemon
        result = handle_tool_call(
            d,
            "queen",
            "queen_prompt_worker",
            {"worker": "queen", "prompt": "x", "reason": "y"},
        )
        assert "Refusing" in result[0]["text"]

    def test_rejects_unknown_worker(self, queen_action_daemon):
        d, _ = queen_action_daemon
        result = handle_tool_call(
            d,
            "queen",
            "queen_prompt_worker",
            {"worker": "ghost", "prompt": "x", "reason": "y"},
        )
        assert "not found" in result[0]["text"].lower()

    def test_requires_reason(self, queen_action_daemon):
        d, _ = queen_action_daemon
        result = handle_tool_call(
            d,
            "queen",
            "queen_prompt_worker",
            {"worker": "platform", "prompt": "hello"},
        )
        assert "reason" in result[0]["text"].lower()

    def test_requires_prompt(self, queen_action_daemon):
        d, _ = queen_action_daemon
        result = handle_tool_call(
            d,
            "queen",
            "queen_prompt_worker",
            {"worker": "platform", "reason": "y"},
        )
        assert "prompt" in result[0]["text"].lower()
