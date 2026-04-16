"""TestRunner — test mode initialization, task loading, and report generation."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from swarm.logging import get_logger
from swarm.server.task_utils import log_task_exception as _log_task_exception
from swarm.tasks.task import PRIORITY_MAP, TYPE_MAP, TaskPriority, TaskType

if TYPE_CHECKING:
    from swarm.drones.pilot import DronePilot
    from swarm.events import EventEmitter
    from swarm.server.daemon import SwarmDaemon
    from swarm.tasks.board import TaskBoard
    from swarm.testing.log import TestRunLog
    from swarm.worker.worker import Worker

_log = get_logger("server.test_runner")


class TestRunner:
    """Manages test mode lifecycle: init, task loading, and report generation."""

    def __init__(
        self,
        *,
        daemon: SwarmDaemon,
        task_board: TaskBoard,
        broadcast_ws: Callable[[dict[str, Any]], None],
        track_task: Callable[[asyncio.Task[object]], None],
        create_task: Callable[..., Any],
        get_pilot: Callable[[], DronePilot | None],
        emitter: EventEmitter,
    ) -> None:
        self._daemon = daemon
        self._task_board = task_board
        self._broadcast_ws = broadcast_ws
        self._track_task = track_task
        self._create_task = create_task
        self._get_pilot = get_pilot
        self._emitter = emitter
        self._test_log: TestRunLog | None = None

    @property
    def test_log(self) -> TestRunLog | None:
        return self._test_log

    def init_test_mode(self) -> None:
        """Initialize test mode: TestRunLog, TestOperator, wire events."""
        import uuid

        from swarm.testing.config import TestConfig
        from swarm.testing.log import TestRunLog
        from swarm.testing.operator import TestOperator

        config = self._daemon.config
        test_cfg = config.test if config.test.enabled else TestConfig(enabled=True)
        report_dir = Path(test_cfg.report_dir).expanduser()
        run_id = uuid.uuid4().hex[:8]

        infra = _capture_infra(self._daemon, test_cfg)
        self._test_log = TestRunLog(run_id, report_dir, infra=infra)
        self._test_operator = TestOperator(self._daemon, self._test_log, test_cfg)
        self._test_operator.start()

        pilot = self._get_pilot()

        # Override pilot idle threshold for faster test-mode completion detection
        if pilot:
            pilot.set_auto_complete_idle(test_cfg.auto_complete_min_idle)

        # Wire pilot's drone_decision event to test log
        if pilot:
            pilot.set_emit_decisions(True)
            pilot.on(
                "drone_decision",
                lambda w, content, d: self._test_log.record_drone_decision(
                    worker_name=w.name,
                    content=content,
                    decision=d.decision.value,
                    reason=d.reason,
                    rule_pattern=d.rule_pattern,
                    rule_index=d.rule_index,
                    source=d.source,
                ),
            )
            # Track state transitions for the test report
            _prev_states: dict[str, str] = {}

            def _log_state_change(w: Worker) -> None:
                old = _prev_states.get(w.name, "UNKNOWN")
                new = w.state.value
                if old != new:
                    self._test_log.record_state_change(w.name, old, new)
                    _prev_states[w.name] = new

            pilot.on_state_changed(_log_state_change)

        # Wire Queen analysis events to test log
        self._emitter.on(
            "queen_analysis",
            lambda wn, action, reasoning, conf: self._test_log.record_queen_analysis(
                worker_name=wn,
                action=action,
                reasoning=reasoning,
                confidence=conf,
            ),
        )

        # Wire hive_complete to report generation
        if pilot:
            pilot.on_hive_complete(self.on_test_complete)

        # Load test tasks into the task board
        if config.test.enabled:
            self.load_test_tasks()

        # Broadcast test mode status to dashboard
        self._broadcast_ws({"type": "test_mode", "enabled": True, "run_id": run_id})

        _log.info("test mode initialized (run_id=%s, log=%s)", run_id, self._test_log.log_path)

    def load_test_tasks(self) -> None:
        """Load tasks from the test project's tasks.yaml into the task board."""
        fixture_tasks_file = (
            Path(__file__).resolve().parent.parent.parent.parent
            / "tests"
            / "fixtures"
            / "test-project"
            / "tasks.yaml"
        )
        if not fixture_tasks_file.exists():
            _log.warning("test tasks.yaml not found at %s", fixture_tasks_file)
            return

        import yaml

        data = yaml.safe_load(fixture_tasks_file.read_text()) or {}
        tasks = data.get("tasks", [])

        # Remove stale test tasks from previous runs to prevent duplicates.
        fixture_titles = {t["title"] for t in tasks if isinstance(t, dict) and t.get("title")}
        if fixture_titles:
            stale_ids = {
                task.id for task in self._task_board.all_tasks if task.title in fixture_titles
            }
            if stale_ids:
                removed = self._task_board.remove_tasks(stale_ids)
                _log.info("removed %d stale test tasks from previous runs", removed)

        for t in tasks:
            if not isinstance(t, dict) or not t.get("title"):
                continue
            self._create_task(
                title=t["title"],
                description=t.get("description", ""),
                priority=PRIORITY_MAP.get(t.get("priority", "normal"), TaskPriority.NORMAL),
                task_type=TYPE_MAP.get(t.get("task_type", "chore"), TaskType.CHORE),
                tags=t.get("tags", []),
                actor="test-mode",
            )
        _log.info("loaded %d test tasks", len(tasks))

    def on_test_complete(self) -> None:
        """Called when hive completes in test mode — trigger report generation."""
        if self._test_log is None:
            return

        test_log = self._test_log

        async def _generate() -> None:
            from swarm.testing.report import ReportGenerator

            gen = ReportGenerator(test_log, test_log.report_dir)
            try:
                report_path = await gen.generate()
                _log.info("test report written to %s", report_path)
                self._broadcast_ws({"type": "test_report_ready", "path": str(report_path)})
            except Exception:
                _log.error("test report generation failed", exc_info=True)

        task = asyncio.create_task(_generate())
        task.add_done_callback(_log_task_exception)
        self._track_task(task)

    async def generate_report_if_pending(self) -> None:
        """Generate a test report on shutdown if one wasn't produced during the run."""
        if self._test_log is None:
            return
        try:
            from swarm.testing.report import ReportGenerator

            gen = ReportGenerator(self._test_log, self._test_log.report_dir)
            report_path = await gen.generate_if_pending()
            if report_path:
                _log.info("fallback test report written to %s", report_path)
        except Exception:
            _log.error("fallback test report generation failed", exc_info=True)


def _capture_infra(daemon: SwarmDaemon, test_cfg: Any) -> Any:
    """Snapshot the infra that's about to run this test.

    Lazily imported to keep module imports minimal and to prevent
    cycles with ``swarm.testing.config``. Returns an ``InfraSnapshot``
    with model/provider/worker_count/port/env fingerprint populated.
    Unknown values default to empty strings so the report renders
    cleanly even when the daemon hasn't been fully wired yet.
    """
    import platform as _platform
    import sys

    from swarm.testing.config import InfraSnapshot, compute_env_hash

    try:
        from swarm import __version__ as swarm_version
    except ImportError:
        swarm_version = ""

    model = getattr(test_cfg, "pin_model", "") or ""
    provider = ""
    try:
        cfg = daemon.config
        provider = getattr(cfg, "provider", "") or ""
        if not model:
            provider_cfg = getattr(cfg, "provider_config", None)
            if provider_cfg is not None:
                model = getattr(provider_cfg, "model", "") or ""
    except Exception:
        pass

    workers_attr = getattr(daemon, "workers", None) or []
    try:
        worker_count = len(workers_attr)
    except TypeError:
        worker_count = 0

    env_hash, env_keys = compute_env_hash()
    import os as _os

    claude_home = _os.environ.get("CLAUDE_PROJECT_DIR", "") or _os.environ.get("CLAUDE_HOME", "")

    return InfraSnapshot(
        model=model,
        provider=provider,
        worker_count=worker_count,
        port=getattr(test_cfg, "port", 0),
        claude_home=claude_home,
        swarm_version=swarm_version or "",
        python_version=sys.version.split()[0],
        platform=_platform.platform(terse=True),
        env_hash=env_hash,
        env_keys=env_keys,
    )
