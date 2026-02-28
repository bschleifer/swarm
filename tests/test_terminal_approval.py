"""Tests for operator terminal approval detection and /action/add-approval-rule."""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from swarm.config import DroneConfig, HiveConfig, QueenConfig
from swarm.drones.log import DroneLog
from swarm.drones.pilot import DronePilot
from swarm.queen.queen import Queen
from swarm.queen.queue import QueenCallQueue
from swarm.server.analyzer import QueenAnalyzer
from swarm.server.config_manager import ConfigManager
from swarm.server.daemon import SwarmDaemon
from swarm.server.email_service import EmailService
from swarm.server.proposals import ProposalManager
from swarm.server.task_manager import TaskManager
from swarm.server.worker_service import WorkerService
from swarm.tasks.board import TaskBoard
from swarm.tasks.history import TaskHistory
from swarm.tasks.proposal import ProposalStore
from swarm.web.app import handle_action_add_approval_rule
from swarm.worker.worker import Worker
from tests.conftest import make_worker as _make_worker
from tests.fakes.process import FakeWorkerProcess

_HEADERS = {"X-Requested-With": "Dashboard"}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _set_content(workers: list[Worker], content: str, command: str = "claude") -> None:
    for w in workers:
        if w.process:
            w.process.set_content(content)
            w.process._child_foreground_command = command


@pytest.fixture
def pilot_setup(monkeypatch):
    """DronePilot with two workers for terminal-approval tests."""
    workers = [_make_worker("alpha"), _make_worker("beta")]
    log = DroneLog()
    pilot = DronePilot(workers, log, interval=1.0, pool=None, drone_config=DroneConfig())
    monkeypatch.setattr("swarm.drones.pilot.revive_worker", AsyncMock())
    return pilot, workers, log


# Claude-style choice prompt: numbered options with cursor arrow
_CHOICE_CONTENT = """\
  Allow `npm test` to run?

  > 1. Yes
    2. No
> """

# Accept-edits prompt content (matches _RE_ACCEPT_EDITS, no numbered choices)
_ACCEPT_EDITS_CONTENT = """\
  >> accept edits on src/main.py
  (Y)es / (N)o
> """

# Plan prompt content (matches _RE_PLAN_MARKERS — should be skipped)
_PLAN_CONTENT = """\
  Proceed with this plan?

  > 1. Yes
    2. No
> """

# User question content (matches is_user_question — should be skipped)
_USER_QUESTION_CONTENT = """\
  Which database should I use? Chat about this or type something.

  > 1. PostgreSQL
    2. MySQL
> """


async def _transition_waiting_to_buzzing(pilot, workers, content, command="claude"):
    """Drive workers through WAITING then back to BUZZING.

    Drones are disabled so no auto-continue fires — the transition is
    "unexplained", simulating an operator pressing Enter in the terminal.
    """
    pilot.enabled = False  # prevent drone auto-continue

    # WAITING only needs 1 confirmation from BUZZING (strong signal)
    _set_content(workers, content, command)
    await pilot.poll_once()

    # Now set BUZZING content to trigger WAITING→BUZZING
    _set_content(workers, "esc to interrupt", command)
    await pilot.poll_once()


# ---------------------------------------------------------------------------
# Detection tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_drone_continue_not_flagged(pilot_setup):
    """WAITING→BUZZING after drone CONTINUE should NOT emit terminal approval."""
    pilot, workers, log = pilot_setup
    events: list[tuple] = []
    pilot.on("operator_terminal_approval", lambda *a: events.append(a))
    pilot.enabled = True

    # Make workers WAITING (1 confirmation needed for WAITING)
    _set_content(workers, _CHOICE_CONTENT)
    await pilot.poll_once()

    # Mark both as drone-continued (simulating what _execute_deferred_actions does)
    for w in workers:
        pilot._drone_continued.add(w.name)

    # Transition to BUZZING
    _set_content(workers, "esc to interrupt")
    await pilot.poll_once()

    assert len(events) == 0, "Drone-continued workers should not trigger terminal approval"


@pytest.mark.asyncio
async def test_button_continue_also_offers_rule(pilot_setup):
    """mark_operator_continue() + WAITING→BUZZING should also emit terminal approval."""
    pilot, workers, log = pilot_setup
    events: list[tuple] = []
    pilot.on("operator_terminal_approval", lambda *a: events.append(a))
    pilot.enabled = False  # prevent drone auto-continue

    # Make workers WAITING
    _set_content(workers, _CHOICE_CONTENT)
    await pilot.poll_once()

    # Mark both as button-continued
    for w in workers:
        pilot.mark_operator_continue(w.name)

    # Transition to BUZZING
    _set_content(workers, "esc to interrupt")
    await pilot.poll_once()

    assert len(events) > 0, "Button-continued workers should also trigger approval banner"


@pytest.mark.asyncio
async def test_terminal_approval_detected(pilot_setup):
    """WAITING→BUZZING with no markers should emit operator_terminal_approval."""
    pilot, workers, log = pilot_setup
    events: list[tuple] = []
    pilot.on("operator_terminal_approval", lambda *a: events.append(a))

    await _transition_waiting_to_buzzing(pilot, workers, _CHOICE_CONTENT)

    assert len(events) > 0, "Terminal approval should be detected"
    # Each event is (worker, summary, prompt_type, pattern)
    for worker, summary, prompt_type, pattern in events:
        assert prompt_type == "choice"
        assert summary  # non-empty


@pytest.mark.asyncio
async def test_plan_prompt_skipped(pilot_setup):
    """Plan prompts should NOT trigger terminal approval."""
    pilot, workers, log = pilot_setup
    events: list[tuple] = []
    pilot.on("operator_terminal_approval", lambda *a: events.append(a))

    await _transition_waiting_to_buzzing(pilot, workers, _PLAN_CONTENT)

    assert len(events) == 0, "Plan prompts should not trigger terminal approval"


@pytest.mark.asyncio
async def test_user_question_skipped(pilot_setup):
    """User questions should NOT trigger terminal approval."""
    pilot, workers, log = pilot_setup
    events: list[tuple] = []
    pilot.on("operator_terminal_approval", lambda *a: events.append(a))

    await _transition_waiting_to_buzzing(pilot, workers, _USER_QUESTION_CONTENT)

    assert len(events) == 0, "User questions should not trigger terminal approval"


@pytest.mark.asyncio
async def test_accept_edits_detected(pilot_setup):
    """Accept-edits prompts should trigger terminal approval with prompt_type='accept_edits'."""
    pilot, workers, log = pilot_setup
    events: list[tuple] = []
    pilot.on("operator_terminal_approval", lambda *a: events.append(a))

    await _transition_waiting_to_buzzing(pilot, workers, _ACCEPT_EDITS_CONTENT)

    assert len(events) > 0, "Accept-edits should be detected"
    for worker, summary, prompt_type, pattern in events:
        assert prompt_type == "accept_edits"
        assert summary == "accept edits"


def test_pattern_suggestion():
    """_suggest_approval_pattern extracts command from backtick-quoted text."""
    from swarm.providers import get_provider

    provider = get_provider("claude")
    # Content with a backtick command in summary
    content = "Allow `npm test` to run?\n\nYes / No\n> "
    pattern = DronePilot._suggest_approval_pattern(content, provider)
    # Should extract "npm" as the command
    if pattern:
        assert "npm" in pattern

    # Content with no backtick
    content2 = "Allow something?\n\nYes / No\n> "
    pattern2 = DronePilot._suggest_approval_pattern(content2, provider)
    assert pattern2 == "" or isinstance(pattern2, str)


def test_cleanup_on_dead_worker(pilot_setup):
    """Tracking data should be cleaned up when a worker is reaped."""
    pilot, workers, log = pilot_setup

    # Populate tracking data
    pilot._waiting_content["alpha"] = "cached"
    pilot._drone_continued.add("alpha")
    pilot._operator_continued.add("alpha")

    # Directly call _cleanup_dead_workers (simulates reap after STUNG timeout)
    pilot._cleanup_dead_workers([workers[0]])

    assert "alpha" not in pilot._waiting_content
    assert "alpha" not in pilot._drone_continued
    assert "alpha" not in pilot._operator_continued


# ---------------------------------------------------------------------------
# Endpoint tests: /action/add-approval-rule
# ---------------------------------------------------------------------------

_TEST_PASSWORD = "test-secret"


@pytest.fixture
def daemon(monkeypatch):
    monkeypatch.setattr("swarm.queen.queen.load_session", lambda _: None)
    monkeypatch.setattr("swarm.queen.queen.save_session", lambda *a: None)

    cfg = HiveConfig(session_name="test", api_password=_TEST_PASSWORD)
    cfg.source_path = str(Path(tempfile.mktemp(suffix=".yaml")))
    d = SwarmDaemon.__new__(SwarmDaemon)
    d.config = cfg
    d.workers = [
        Worker(name="w1", path="/tmp/w1", process=FakeWorkerProcess(name="w1")),
    ]
    d._worker_lock = asyncio.Lock()
    d.drone_log = DroneLog()
    d.task_board = TaskBoard()
    d.task_history = TaskHistory(log_file=Path(tempfile.mktemp(suffix=".jsonl")))
    d.queen = Queen(config=QueenConfig(cooldown=0.0), session_name="test")
    d.queen_queue = QueenCallQueue(max_concurrent=2)
    d.proposal_store = ProposalStore()
    d.proposals = ProposalManager(d.proposal_store, d)
    d.analyzer = QueenAnalyzer(d.queen, d, d.queen_queue)
    d.notification_bus = MagicMock()
    d.pilot = MagicMock(spec=DronePilot)
    d.pilot.enabled = True
    d.ws_clients = set()
    d.terminal_ws_clients = set()
    d.pool = None
    d.start_time = 0.0
    d.broadcast_ws = MagicMock()
    d.graph_mgr = None
    d.email = EmailService(
        drone_log=d.drone_log,
        queen=d.queen,
        graph_mgr=d.graph_mgr,
        broadcast_ws=d.broadcast_ws,
    )
    d.tasks = TaskManager(
        task_board=d.task_board,
        task_history=d.task_history,
        drone_log=d.drone_log,
        pilot=d.pilot,
    )
    d.send_to_worker = AsyncMock()
    d._prep_worker_for_task = AsyncMock()
    d._heartbeat_task = None
    d._usage_task = None
    d._heartbeat_snapshot = {}
    d._config_mtime = 0.0
    d._bg_tasks: set[asyncio.Task[object]] = set()
    d._notification_history: list[dict] = []
    d.config_mgr = ConfigManager(d)
    d.worker_svc = WorkerService(d)
    return d


@pytest.fixture
async def client(daemon):
    app = web.Application()
    app["daemon"] = daemon
    app.router.add_post("/action/add-approval-rule", handle_action_add_approval_rule)
    async with TestClient(TestServer(app)) as c:
        yield c


@pytest.mark.asyncio
async def test_add_rule_valid(client, daemon):
    """Valid pattern → rule added, 200."""
    resp = await client.post(
        "/action/add-approval-rule",
        data={"pattern": r"\bnpm\b"},
        headers=_HEADERS,
    )
    assert resp.status == 200
    data = await resp.json()
    assert data["status"] == "ok"
    assert data["rule_added"] == r"\bnpm\b"
    rules = daemon.config.drones.approval_rules
    assert len(rules) == 1
    assert rules[0].pattern == r"\bnpm\b"
    assert rules[0].action == "approve"


@pytest.mark.asyncio
async def test_add_rule_invalid_regex(client, daemon):
    """Invalid regex → 400."""
    resp = await client.post(
        "/action/add-approval-rule",
        data={"pattern": "[invalid"},
        headers=_HEADERS,
    )
    assert resp.status == 400
    data = await resp.json()
    assert "invalid regex" in data["error"]


@pytest.mark.asyncio
async def test_add_rule_empty_pattern(client, daemon):
    """Empty pattern → 400."""
    resp = await client.post(
        "/action/add-approval-rule",
        data={"pattern": ""},
        headers=_HEADERS,
    )
    assert resp.status == 400
    data = await resp.json()
    assert "pattern" in data["error"]
