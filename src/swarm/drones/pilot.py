"""Drone background drones — async polling loop + decision engine."""

from __future__ import annotations

import asyncio
import re
import time
from typing import TYPE_CHECKING, ClassVar

from swarm.config import DroneConfig
from swarm.drones.backoff import compute_backoff
from swarm.drones.coordination import CoordinationHandler
from swarm.drones.decision_executor import DecisionExecutor as _DecisionExecutor
from swarm.drones.directives import DirectiveExecutor
from swarm.drones.log import DroneAction, DroneLog
from swarm.drones.oversight_handler import OversightHandler
from swarm.drones.pressure import PressureManager
from swarm.drones.state_tracker import WorkerStateTracker
from swarm.drones.task_lifecycle import TaskLifecycle
from swarm.events import EventEmitter
from swarm.logging import get_logger
from swarm.pty.process import ProcessError
from swarm.worker.manager import revive_worker  # noqa: F401 — monkeypatched by tests
from swarm.worker.worker import Worker, WorkerState

if TYPE_CHECKING:
    import types
    from collections.abc import Awaitable, Callable
    from typing import Any

    from swarm.drones.rules import DroneDecision
    from swarm.events import (
        EscalateCallback,
        ProposalCallback,
        TaskAssignedCallback,
        TaskDoneCallback,
        VoidCallback,
        WorkerCallback,
    )
    from swarm.providers import LLMProvider
    from swarm.providers.styled import StyledContent
    from swarm.pty.provider import WorkerProcessProvider
    from swarm.queen.oversight import OversightMonitor
    from swarm.queen.queen import Queen
    from swarm.resources.monitor import MemoryPressureLevel
    from swarm.tasks.board import TaskBoard

_log = get_logger("drones.pilot")


def extract_prompt_snippet(content: str, max_lines: int = 15) -> str:
    """Extract the prompt area from PTY content for rule creation context."""
    lines = content.strip().splitlines()
    return "\n".join(lines[-max_lines:])


# Run Queen coordination every N poll cycles (default: every 12 cycles = ~60s at 5s interval)
_COORDINATION_INTERVAL = 12

# classify_worker_output examines <=30 lines; 35 gives margin for context.
_STATE_DETECT_LINES = 35

# Commands that should never be pre-populated as suggested approval patterns.
# Returning "" forces the user to type a pattern deliberately.
_DANGEROUS_CMDS = frozenset(
    {
        "rm",
        "rmdir",
        "kill",
        "killall",
        "pkill",
        "dd",
        "mkfs",
        "fdisk",
        "parted",
        "chmod",
        "chown",
        "chgrp",
        "sudo",
        "su",
        "doas",
        "reboot",
        "shutdown",
        "halt",
        "poweroff",
        "init",
        "mv",
    }
)

# Wrapper commands — include the next word to form the pattern
# (e.g. "uv run pytest" → 3 words, not just "uv")
_WRAPPER_CMDS = frozenset({"uv", "npx", "bunx", "pipx", "nix"})


def _build_safe_pattern(words: list[str]) -> str:
    """Build a safe, specific approval pattern from command words.

    Returns ``""`` if the root command is in :data:`_DANGEROUS_CMDS`.
    Otherwise returns a ``\\b``-delimited pattern using the first two
    meaningful words (three for wrapper commands like ``uv run``).
    """
    if not words:
        return ""

    root = words[0]
    # Handle variants like "mkfs.ext4" → check "mkfs"
    root_base = root.split(".")[0]
    if root in _DANGEROUS_CMDS or root_base in _DANGEROUS_CMDS:
        return ""

    # For wrapper commands like "uv run pytest", take 3 words
    if root in _WRAPPER_CMDS and len(words) >= 3 and words[1] == "run":
        key = " ".join(words[:3])
    elif len(words) >= 2:
        # Check if the second word is also dangerous (e.g. "sudo rm")
        if words[1] in _DANGEROUS_CMDS:
            return ""
        key = " ".join(words[:2])
    else:
        key = root

    return r"\b" + re.escape(key) + r"\b"


# Matches a prompt line with operator-typed text: "> /verify", "❯ fix the bug"
_RE_PROMPT_WITH_TEXT = re.compile(r"^[>❯]\s+\S")


class DronePilot(EventEmitter):
    def __init__(
        self,
        workers: list[Worker],
        log: DroneLog,
        interval: float = 5.0,
        pool: WorkerProcessProvider | None = None,
        drone_config: DroneConfig | None = None,
        task_board: TaskBoard | None = None,
        queen: Queen | None = None,
        worker_descriptions: dict[str, str] | None = None,
        context_builder: Callable[..., str] | None = None,
        auto_mode: bool = False,
    ) -> None:
        self.__init_emitter__()
        self.workers = workers
        self.log = log
        self.interval = interval
        self.pool = pool
        self._drone_config = drone_config or DroneConfig()
        self.auto_mode = auto_mode
        self._provider_cache: dict[str, LLMProvider] = {}
        self._task_board = task_board
        self._queen = queen
        self.worker_descriptions = worker_descriptions or {}
        self._context_builder = context_builder
        self.enabled = False
        self._running = False  # loop lifecycle (separate from action gating)
        self._task: asyncio.Task | None = None
        self._tick: int = 0
        # Adaptive polling
        self._idle_streak: int = 0
        self._base_interval: float = interval
        self._max_interval: float = self._drone_config.max_idle_interval
        # Per-worker circuit breaker: name → (consecutive_failures, last_failure_time)
        self._poll_failures: dict[str, tuple[int, float]] = {}
        # Prevent concurrent poll_once execution
        self._poll_lock = asyncio.Lock()
        # Focus tracking: when a user is viewing a worker, poll faster
        self._focused_workers: set[str] = set()
        self._focus_interval: float = 2.0
        # Hive-complete detection — only fires after a task is completed
        # during this pilot session (not stale completions from disk).
        self._all_done_streak: int = 0
        # Shared mutable state containers
        self._prev_states: dict[str, WorkerState] = {}
        self._escalated: dict[str, float] = {}  # name → monotonic escalation time
        self._revive_history: dict[str, list[float]] = {}
        self._idle_consecutive: dict[str, int] = {}
        self._proposed_completions: dict[str, float] = {}
        self._suspended: set[str] = set()
        self._suspended_at: dict[str, float] = {}
        # Proposal support: callback to check if pending proposals exist
        self._pending_proposals_check: Callable[[], bool] | None = None
        # Per-worker proposal check: returns True if the named worker has pending proposals
        self._pending_proposals_for_worker: Callable[[str], bool] | None = None
        # Consecutive poll loop errors for structured error tracking
        self._consecutive_errors: int = 0
        # Oversight monitor (initialized externally via set_oversight)
        self._oversight: OversightMonitor | None = None
        # Oversight check interval in ticks (separate from coordination)
        self._oversight_interval: int = 24  # ~2 min at 5s poll

        # --- Sub-handlers (extracted for complexity reduction) ---
        self._directives = DirectiveExecutor(
            workers=self.workers,
            log=self.log,
            pool=self.pool,
            queen=self.queen,
            task_board=self.task_board,
            emit=self.emit,
            classify_worker_state=self._classify_worker_state,
            get_provider=self._get_provider,
            safe_worker_action=self._safe_worker_action,
            pending_proposals_check=self._pending_proposals_check,
            proposed_completions=self._proposed_completions,
        )
        self._coordination = CoordinationHandler(
            workers=self.workers,
            log=self.log,
            queen=self.queen,
            task_board=self.task_board,
            build_context=self._build_context,
            directive_executor=self._directives,
            pending_proposals_check=self._pending_proposals_check,
            escalated=self._escalated,
        )
        self._oversight_handler = OversightHandler(
            workers=self.workers,
            log=self.log,
            queen=self.queen,
            task_board=self.task_board,
            oversight_monitor=self._oversight,
            emit=self.emit,
            capture_outputs=self._coordination.capture_worker_outputs,
        )
        self._pressure_mgr = PressureManager(
            workers=self.workers,
            log=self.log,
            pool=self.pool,
            suspended=self._suspended,
            suspended_at=self._suspended_at,
            emit=self.emit,
        )
        self._decision_exec = _DecisionExecutor(
            workers=self.workers,
            log=self.log,
            pool=self.pool,
            drone_config=self.drone_config,
            auto_mode=self.auto_mode,
            emit=self.emit,
            get_provider=self._get_provider,
            directive_executor=self._directives,
            escalated=self._escalated,
            revive_history=self._revive_history,
        )
        self._state_tracker = WorkerStateTracker(
            workers=self.workers,
            log=self.log,
            task_board=self.task_board,
            drone_config=self.drone_config,
            get_provider=self._get_provider,
            emit=self.emit,
            decision_executor=self._decision_exec,
            prev_states=self._prev_states,
            idle_consecutive=self._idle_consecutive,
            escalated=self._escalated,
            suspended=self._suspended,
            suspended_at=self._suspended_at,
            focused_workers=self._focused_workers,
            revive_history=self._revive_history,
        )
        self._task_lifecycle = TaskLifecycle(
            workers=self.workers,
            log=self.log,
            task_board=self.task_board,
            queen=self.queen,
            drone_config=self.drone_config,
            proposed_completions=self._proposed_completions,
            idle_consecutive=self._idle_consecutive,
            emit=self.emit,
            build_context=self._build_context,
            pending_proposals_check=self._pending_proposals_check,
            pending_proposals_for_worker=self._pending_proposals_for_worker,
        )
        # Wire the drone-continued callback
        self._decision_exec.set_drone_continued_callback(self._state_tracker.mark_drone_continued)

    @property
    def task_board(self) -> TaskBoard | None:
        """Return the task board."""
        return self._task_board

    @task_board.setter
    def task_board(self, value: TaskBoard | None) -> None:
        """Set the task board, propagating to sub-handlers."""
        self._task_board = value
        for attr in ("_task_lifecycle", "_directives", "_coordination", "_oversight_handler"):
            handler = getattr(self, attr, None)
            if handler is not None:
                handler.task_board = value

    @property
    def queen(self) -> Queen | None:
        """Return the Queen instance."""
        return self._queen

    @queen.setter
    def queen(self, value: Queen | None) -> None:
        """Set the Queen instance, propagating to sub-handlers."""
        self._queen = value
        if hasattr(self, "_directives"):
            self._directives.queen = value
        if hasattr(self, "_coordination"):
            self._coordination.queen = value
        if hasattr(self, "_oversight_handler"):
            self._oversight_handler.queen = value
        if hasattr(self, "_task_lifecycle"):
            self._task_lifecycle.queen = value

    @property
    def drone_config(self) -> DroneConfig:
        """Return the drone config."""
        return self._drone_config

    @drone_config.setter
    def drone_config(self, value: DroneConfig) -> None:
        """Set the drone config, propagating to sub-handlers."""
        self._drone_config = value
        for attr in ("_decision_exec", "_state_tracker", "_task_lifecycle", "_pressure_mgr"):
            handler = getattr(self, attr, None)
            if handler is not None:
                handler.drone_config = value

    @property
    def pressure_suspended_workers(self) -> list[str]:
        """Return sorted list of workers currently suspended due to resource pressure."""
        return self._pressure_mgr.pressure_suspended_workers

    # --- Backward-compat state access ---

    @property
    def _auto_complete_min_idle(self) -> float:
        return self._task_lifecycle._auto_complete_min_idle

    @_auto_complete_min_idle.setter
    def _auto_complete_min_idle(self, value: float) -> None:
        self._task_lifecycle._auto_complete_min_idle = value

    @property
    def _COMPLETION_REPROPOSE_COOLDOWN(self) -> int:
        return self._task_lifecycle._COMPLETION_REPROPOSE_COOLDOWN

    @_COMPLETION_REPROPOSE_COOLDOWN.setter
    def _COMPLETION_REPROPOSE_COOLDOWN(self, value: int) -> None:
        self._task_lifecycle._COMPLETION_REPROPOSE_COOLDOWN = value

    @property
    def _saw_completion(self) -> bool:
        return self._task_lifecycle.saw_completion

    @_saw_completion.setter
    def _saw_completion(self, value: bool) -> None:
        self._task_lifecycle._saw_completion = value

    @property
    def _needs_assign_check(self) -> bool:
        return self._task_lifecycle.needs_assign_check

    @_needs_assign_check.setter
    def _needs_assign_check(self, value: bool) -> None:
        self._task_lifecycle.needs_assign_check = value

    @property
    def _had_substantive_action(self) -> bool:
        return self._decision_exec._had_substantive_action

    @_had_substantive_action.setter
    def _had_substantive_action(self, value: bool) -> None:
        self._decision_exec._had_substantive_action = value

    @property
    def _any_became_active(self) -> bool:
        return self._state_tracker.any_became_active

    @_any_became_active.setter
    def _any_became_active(self, value: bool) -> None:
        self._state_tracker.any_became_active = value

    @property
    def _emit_decisions(self) -> bool:
        return self._decision_exec._emit_decisions

    @_emit_decisions.setter
    def _emit_decisions(self, value: bool) -> None:
        self._decision_exec._emit_decisions = value

    @property
    def _pressure_level(self) -> str:
        return self._pressure_mgr.pressure_level

    @_pressure_level.setter
    def _pressure_level(self, value: str) -> None:
        self._pressure_mgr._pressure_level = value

    @property
    def _suspended_for_pressure(self) -> set[str]:
        return self._pressure_mgr._suspended_for_pressure

    @property
    def _deferred_actions(self) -> list:
        return self._decision_exec._deferred_actions

    @_deferred_actions.setter
    def _deferred_actions(self, value: list) -> None:
        self._decision_exec._deferred_actions = value

    @property
    def _content_fingerprints(self) -> dict[str, int]:
        return self._state_tracker._content_fingerprints

    @property
    def _unchanged_streak(self) -> dict[str, int]:
        return self._state_tracker._unchanged_streak

    @property
    def _last_full_poll(self) -> dict[str, float]:
        return self._state_tracker._last_full_poll

    @property
    def _waiting_content(self) -> dict[str, str]:
        return self._state_tracker._waiting_content

    @property
    def _drone_continued(self) -> set[str]:
        return self._state_tracker._drone_continued

    @property
    def _operator_continued(self) -> set[str]:
        return self._state_tracker._operator_continued

    @property
    def _escalation_timeout(self) -> float:
        return self._decision_exec._escalation_timeout

    @_escalation_timeout.setter
    def _escalation_timeout(self, value: float) -> None:
        self._decision_exec._escalation_timeout = value

    @property
    def _revive_loop_max(self) -> int:
        return self._decision_exec._revive_loop_max

    @_revive_loop_max.setter
    def _revive_loop_max(self, value: int) -> None:
        self._decision_exec._revive_loop_max = value

    @property
    def _revive_loop_window(self) -> float:
        return self._decision_exec._revive_loop_window

    @_revive_loop_window.setter
    def _revive_loop_window(self, value: float) -> None:
        self._decision_exec._revive_loop_window = value

    # Class-level constants — delegate to _task_lifecycle for runtime access
    _AUTO_COMPLETE_MIN_IDLE: ClassVar[int] = 45
    _PROPOSED_COMPLETION_CLEANUP_INTERVAL: ClassVar[int] = 60
    _PROPOSED_COMPLETION_MAX_AGE: ClassVar[float] = 3600.0
    _PROPOSED_COMPLETION_MAX_SIZE: ClassVar[int] = 500

    def _get_provider(self, worker: Worker) -> LLMProvider:
        """Return the LLMProvider for a worker, caching by provider name."""
        name = worker.provider_name
        if name not in self._provider_cache:
            from swarm.providers import get_provider

            self._provider_cache[name] = get_provider(name)
        return self._provider_cache[name]

    def invalidate_provider_cache(self) -> None:
        """Clear cached providers so tuning changes take effect."""
        self._provider_cache.clear()

    def _build_context(self, **kwargs: object) -> str:
        """Build hive context string via the injected context_builder."""
        if self._context_builder is None:
            from swarm.queen.context import build_hive_context

            self._context_builder = build_hive_context
        return self._context_builder(
            list(self.workers),
            drone_log=self.log,
            task_board=self.task_board,
            worker_descriptions=self.worker_descriptions,
            **kwargs,
        )

    # --- Public encapsulation methods ---

    def get_diagnostics(self) -> dict[str, object]:
        """Return pilot health/diagnostic info for status endpoints."""
        task = self._task
        info: dict[str, object] = {
            "running": self._running,
            "enabled": self.enabled,
            "task_alive": task is not None and not task.done(),
            "tick": self._tick,
            "idle_streak": self._idle_streak,
            "suspended_count": len(self._suspended),
            "suspended_workers": sorted(self._suspended),
        }
        if task and task.done():
            try:
                exc = task.exception() if not task.cancelled() else "cancelled"
            except Exception:
                exc = "unknown"
            info["task_exception"] = str(exc) if exc else None
        return info

    def set_focused_workers(self, workers: set[str]) -> None:
        """Set which workers should be polled at accelerated interval."""
        # Wake any newly focused workers that are suspended
        for name in workers - self._focused_workers:
            self.wake_worker(name)
        self._focused_workers = workers
        # Propagate to state tracker
        self._state_tracker._focused_workers = workers

    def set_pending_proposals_check(self, callback: Callable[[], bool] | None) -> None:
        """Register callback to check if pending proposals exist."""
        self._pending_proposals_check = callback
        self._directives._pending_proposals_check = callback
        self._coordination._pending_proposals_check = callback
        self._task_lifecycle._pending_proposals_check = callback

    def set_pending_proposals_for_worker(self, callback: Callable[[str], bool] | None) -> None:
        """Register callback to check if a specific worker has pending proposals."""
        self._pending_proposals_for_worker = callback
        self._task_lifecycle._pending_proposals_for_worker = callback

    def set_poll_intervals(self, base: float, max_val: float) -> None:
        """Update polling intervals without restarting the poll loop."""
        self._base_interval = base
        self._max_interval = max_val

    def set_emit_decisions(self, enabled: bool) -> None:
        """Enable/disable emission of drone_decision events (for test mode)."""
        self._decision_exec.set_emit_decisions(enabled)

    def set_auto_complete_idle(self, seconds: float) -> None:
        """Override the minimum idle time before proposing task completion."""
        self._task_lifecycle.set_auto_complete_idle(seconds)

    def mark_completion_seen(self) -> None:
        """Signal that a task completion occurred during this pilot session."""
        self._task_lifecycle.mark_completion_seen()

    def set_oversight(self, monitor: OversightMonitor) -> None:
        """Set the oversight monitor."""
        self._oversight = monitor
        self._oversight_handler.set_oversight(monitor)

    # --- Delegate to PressureManager ---

    def _signal_worker_async(self, name: str, sig: int) -> None:
        """Send a signal to a worker via the pool, fire-and-forget."""
        self._pressure_mgr._signal_worker_async(name, sig)

    def _suspend_workers(self, names: list[str], reason: str) -> int:
        """Mark workers as pressure-suspended."""
        return self._pressure_mgr._suspend_workers(names, reason)

    def on_pressure_changed(self, level: MemoryPressureLevel) -> None:
        """Respond to a change in system resource pressure."""
        self._pressure_mgr.on_pressure_changed(level)

    def _resume_pressure_suspended(self) -> None:
        """Resume workers that were suspended due to pressure."""
        self._pressure_mgr._resume_pressure_suspended()

    def _suspend_on_high_pressure(self, math_mod: types.ModuleType) -> None:
        """Suspend SLEEPING workers to target 60% active."""
        self._pressure_mgr._suspend_on_high_pressure(math_mod)

    def _suspend_on_critical_pressure(self) -> None:
        """Suspend SLEEPING/RESTING workers except the most recently active."""
        self._pressure_mgr._suspend_on_critical_pressure()

    # --- Delegate to WorkerStateTracker ---

    def mark_operator_continue(self, name: str) -> None:
        """Record that the operator continued this worker via the dashboard button."""
        self._state_tracker.mark_operator_continue(name)

    def wake_worker(self, name: str) -> bool:
        """Wake a suspended worker so it's polled on the next tick."""
        return self._state_tracker.wake_worker(name)

    def _maybe_suspend_worker(self, worker: Worker) -> None:
        """Suspend a sleeping worker if it has been unchanged long enough."""
        self._state_tracker._maybe_suspend_worker(worker)

    def _classify_worker_state(
        self,
        worker: Worker,
        cmd: str,
        content: str,
        styled: StyledContent | None = None,
    ) -> tuple[WorkerState, list | None]:
        """Classify worker output into a state, with exception safety."""
        return self._state_tracker._classify_worker_state(worker, cmd, content, styled=styled)

    def _sync_display_state(self, worker: Worker, state_changed: bool) -> bool:
        """Emit state_changed for display-only transitions."""
        return self._state_tracker._sync_display_state(worker, state_changed)

    def _track_idle(self, worker: Worker) -> None:
        """Update per-worker idle-consecutive counter."""
        self._state_tracker._track_idle(worker)

    def _handle_state_change(self, worker: Worker, prev: WorkerState) -> tuple[bool, bool]:
        """Process a worker state change."""
        return self._state_tracker._handle_state_change(worker, prev)

    def _handle_waiting_exit(self, worker: Worker, prev: WorkerState) -> None:
        """Detect who approved a WAITING worker and clean up cached content."""
        self._state_tracker._handle_waiting_exit(worker, prev)

    def _detect_operator_terminal_approval(self, worker: Worker) -> None:
        """Emit an event when the operator approved a prompt via the terminal."""
        self._state_tracker._detect_operator_terminal_approval(worker)

    @staticmethod
    def _suggest_approval_pattern(content: str, provider: LLMProvider) -> str:
        """Extract a suggested regex pattern from the raw PTY content."""
        return WorkerStateTracker._suggest_approval_pattern(content, provider)

    def _should_throttle_sleeping(self, worker: Worker, now: float | None = None) -> bool:
        """Check if a sleeping worker's full poll should be skipped."""
        return self._state_tracker._should_throttle_sleeping(worker, now=now)

    def _update_content_fingerprint(self, name: str, content: str) -> None:
        """Update content fingerprint and unchanged streak for a worker."""
        self._state_tracker._update_content_fingerprint(name, content)

    def _poll_sleeping_throttled(self, worker: Worker, cmd: str) -> tuple[bool, bool] | None:
        """Lightweight poll for throttled sleeping workers."""
        return self._state_tracker._poll_sleeping_throttled(worker, cmd)

    def _poll_dead_worker(
        self,
        worker: Worker,
        dead_workers: list[Worker],
    ) -> tuple[bool, bool, bool]:
        """Handle polling for a worker whose process is dead or missing."""
        return self._state_tracker._poll_dead_worker(worker, dead_workers)

    def _poll_single_worker(
        self,
        worker: Worker,
        dead_workers: list[Worker],
        now: float | None = None,
    ) -> tuple[bool, bool, bool]:
        """Poll one worker. Returns (had_action, transitioned_to_resting, state_changed)."""
        return self._state_tracker._poll_single_worker(
            worker, dead_workers, now=now, enabled=self.enabled
        )

    def _is_suspended_skip(self, worker: Worker, now: float | None = None) -> bool:
        """Return True if this worker should be skipped."""
        return self._state_tracker._is_suspended_skip(worker, now=now)

    # --- Delegate to DecisionExecutor ---

    def _should_skip_decide(self, worker: Worker, changed: bool) -> bool:
        """Return True if the decision engine should be skipped for this worker."""
        return self._decision_exec._should_skip_decide(worker, changed, self.enabled)

    def _run_decision_sync(self, worker: Worker, content: str, events: list | None = None) -> bool:
        """Evaluate the drone decision for a worker (sync — actions deferred)."""
        return self._decision_exec._run_decision_sync(worker, content, events=events)

    def _is_revive_loop(self, name: str) -> bool:
        """Return True if *name* has been revived too many times within the window."""
        return self._decision_exec._is_revive_loop(name)

    def _record_revive(self, name: str) -> None:
        """Record a successful revive for loop detection."""
        self._decision_exec._record_revive(name)

    async def _execute_deferred_actions(self) -> None:
        """Execute deferred async actions from the sync poll loop."""
        await self._decision_exec._execute_deferred_actions()

    async def _execute_deferred_continue(
        self,
        worker: Worker,
        decision: DroneDecision,
        state_at_decision: WorkerState,
        proc_at_decision: object | None,
        content: str = "",
    ) -> None:
        """Execute a single deferred CONTINUE action with safety checks."""
        await self._decision_exec._execute_deferred_continue(
            worker, decision, state_at_decision, proc_at_decision, content
        )

    async def _safe_worker_action(
        self,
        worker: Worker,
        coro: Awaitable[None],
        action: DroneAction,
        decision: DroneDecision | None = None,
        *,
        include_rule_pattern: bool = False,
        reason: str | None = None,
        prompt_snippet: str = "",
    ) -> bool:
        """Execute *coro* for *worker*, log on success, warn on failure."""
        return await self._decision_exec._safe_worker_action(
            worker,
            coro,
            action,
            decision,
            include_rule_pattern=include_rule_pattern,
            reason=reason,
            prompt_snippet=prompt_snippet,
        )

    # --- Delegate to TaskLifecycle ---

    def clear_proposed_completion(self, task_id: str) -> None:
        """Remove a task from the proposed-completions tracker."""
        self._task_lifecycle.clear_proposed_completion(task_id)

    def _should_eager_assign(self) -> bool:
        """Check if idle-escalation or event-driven flag should trigger assign."""
        return self._task_lifecycle._should_eager_assign()

    def _cleanup_stale_proposed_completions(self) -> None:
        """Evict proposed-completion entries older than 1 hour."""
        self._task_lifecycle._cleanup_stale_proposed_completions()

    def _check_task_completions(self) -> bool:
        """Propose completion for tasks whose assigned worker has been idle."""
        return self._task_lifecycle._check_task_completions()

    async def _auto_assign_tasks(self) -> bool:
        """Ask Queen for assignments and emit proposals for user approval."""
        return await self._task_lifecycle._auto_assign_tasks()

    # --- Delegate to DirectiveExecutor ---

    @staticmethod
    def _has_pending_bash_approval(worker: Worker) -> bool:
        """Check if a worker's terminal shows a bash/command approval prompt."""
        return DirectiveExecutor.has_pending_bash_approval(worker)

    @staticmethod
    def _has_idle_prompt(worker: Worker) -> bool:
        """Check if worker's terminal shows an idle/suggested prompt."""
        return DirectiveExecutor.has_idle_prompt(worker)

    @staticmethod
    def _has_operator_text_at_prompt(worker: Worker) -> bool:
        """Check if worker has operator-typed text at a prompt."""
        return DirectiveExecutor.has_operator_text_at_prompt(worker)

    async def _execute_directives(
        self, directives: list[dict[str, Any]], confidence: float = 0.0
    ) -> bool:
        """Dispatch a list of Queen directives to the appropriate handlers."""
        return await self._directives.execute_directives(directives, confidence=confidence)

    # --- Delegate to OversightHandler ---

    async def _oversight_cycle(self) -> bool:
        """Run oversight signal detection and Queen evaluation."""
        return await self._oversight_handler.oversight_cycle()

    # --- Delegate to CoordinationHandler ---

    def _capture_worker_outputs(self) -> dict[str, str]:
        """Capture worker output for coordination."""
        return self._coordination.capture_worker_outputs()

    async def _coordination_cycle(self) -> bool:
        """Periodic full-hive coordination via Queen."""
        return await self._coordination.coordination_cycle()

    # --- Lifecycle / event registration ---

    def clear_escalation(self, worker_name: str) -> None:
        """Remove a worker from the escalation tracker."""
        self._escalated.pop(worker_name, None)

    def on_proposal(self, callback: ProposalCallback) -> None:
        """Register callback for when the Queen proposes an assignment."""
        self.on("proposal", callback)

    def on_escalate(self, callback: EscalateCallback) -> None:
        """Register callback for escalation events."""
        self.on("escalate", callback)

    def on_workers_changed(self, callback: VoidCallback) -> None:
        """Register callback for when workers list changes (add/remove)."""
        self.on("workers_changed", callback)

    def on_task_assigned(self, callback: TaskAssignedCallback) -> None:
        """Register callback for when a task is auto-assigned to a worker."""
        self.on("task_assigned", callback)

    def on_task_done(self, callback: TaskDoneCallback) -> None:
        """Register callback for when a task appears complete."""
        self.on("task_done", callback)

    def on_state_changed(self, callback: WorkerCallback) -> None:
        """Register callback for any worker state change."""
        self.on("state_changed", callback)

    def on_hive_empty(self, callback: VoidCallback) -> None:
        """Register callback for when all workers are gone."""
        self.on("hive_empty", callback)

    def on_hive_complete(self, callback: VoidCallback) -> None:
        """Register callback for when all tasks are done and workers idle."""
        self.on("hive_complete", callback)

    def is_loop_running(self) -> bool:
        """Check if the pilot poll loop task is currently executing."""
        return self._running and self._task is not None and not self._task.done()

    def needs_restart(self) -> bool:
        """True when the pilot should be running but the loop task has died."""
        return self._running and not self.is_loop_running()

    async def restart_loop(self) -> None:
        """Restart the poll loop task. Safe to call if already running."""
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        self._task = asyncio.create_task(self._loop())
        self._task.add_done_callback(self._on_loop_done)

    def start(self) -> None:
        self.enabled = True
        self._running = True
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop())
            self._task.add_done_callback(self._on_loop_done)

    @staticmethod
    def _on_loop_done(task: asyncio.Task) -> None:
        """Log when the poll loop task finishes unexpectedly."""
        if task.cancelled():
            _log.info("poll loop task was cancelled")
        elif exc := task.exception():
            _log.error("poll loop task died with exception: %s", exc, exc_info=exc)
        else:
            _log.info("poll loop task exited normally")

    def stop(self) -> None:
        """Fully stop the pilot — kills the poll loop."""
        self.enabled = False
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()

    def toggle(self) -> bool:
        """Toggle drone actions on/off. State detection keeps running."""
        self.enabled = not self.enabled
        # Ensure the poll loop is alive even when drones are disabled
        if self._task is None or self._task.done():
            self._running = True
            self._task = asyncio.create_task(self._loop())
        return self.enabled

    async def poll_once(self) -> bool:
        """Run one poll cycle across all workers."""
        # Revive poll loop if it died unexpectedly
        if self._running and (self._task is None or self._task.done()):
            _log.warning("poll loop was dead — restarting")
            self._task = asyncio.create_task(self._loop())
        if self._poll_lock.locked():
            return False  # Another poll is in progress — skip
        async with self._poll_lock:
            had_action, _any_state_changed = await self._poll_once_locked()
            self._consecutive_errors = 0
            return had_action

    def _cleanup_dead_workers(self, dead_workers: list[Worker]) -> None:
        """Remove dead workers from tracking and unassign their tasks."""
        for dw in dead_workers:
            self.workers.remove(dw)
            self._state_tracker.cleanup_dead_worker(dw)
            self._poll_failures.pop(dw.name, None)
            _log.info("removed dead worker: %s", dw.name)
            if self.task_board:
                self.task_board.unassign_worker(dw.name)
        self.emit("workers_changed")

    async def _run_periodic_tasks(self) -> bool:
        """Run periodic background tasks: completions, auto-assign, coordination."""
        had_action = False
        # Periodic cleanup of stale proposed-completion entries
        if self._tick > 0 and self._tick % self._PROPOSED_COMPLETION_CLEANUP_INTERVAL == 0:
            self._cleanup_stale_proposed_completions()
        if self.enabled and self.task_board:
            if self._check_task_completions():
                had_action = True
        # Auto-assign: always attempt, but _should_eager_assign logs intent
        self._needs_assign_check = False
        if self.enabled and self.task_board and self.queen:
            if await self._auto_assign_tasks():
                had_action = True
        if (
            self.enabled
            and self.queen
            and self._tick > 0
            and self._tick % _COORDINATION_INTERVAL == 0
        ):
            if await self._coordination_cycle():
                had_action = True
        # Oversight: signal-triggered Queen monitoring
        if (
            self.enabled
            and self.queen
            and self._oversight
            and self._tick > 0
            and self._tick % self._oversight_interval == 0
        ):
            if await self._oversight_cycle():
                had_action = True
        return had_action

    async def _poll_once_locked(self) -> tuple[bool, bool]:
        """Returns (had_action, any_state_changed)."""
        any_state_changed = False
        dead_workers: list[Worker] = []
        had_action = False
        max_poll_failures = self.drone_config.max_poll_failures
        self._deferred_actions = []
        now = time.time()

        for worker in list(self.workers):
            if self._is_suspended_skip(worker, now=now):
                continue

            try:
                action, _transitioned, changed = self._poll_single_worker(
                    worker, dead_workers, now=now
                )
                had_action |= action
                any_state_changed |= changed
                # Successful poll — clear any failure counter
                self._poll_failures.pop(worker.name, None)
            except (ProcessError, OSError) as exc:
                import time as _time

                prev_fails, _ = self._poll_failures.get(worker.name, (0, 0.0))
                fails = prev_fails + 1
                self._poll_failures[worker.name] = (fails, _time.monotonic())
                # ConnectionError / timeout → transient; likely holder hiccup
                is_transient = isinstance(exc, (ConnectionError, TimeoutError))
                _log.warning(
                    "poll failed for %s (%d/%d, %s)",
                    worker.name,
                    fails,
                    max_poll_failures,
                    "transient" if is_transient else "permanent",
                    exc_info=True,
                )
                # Transient errors get double the threshold before tripping
                threshold = max_poll_failures * 2 if is_transient else max_poll_failures
                if fails >= threshold:
                    _log.warning(
                        "circuit breaker tripped for %s — treating as dead",
                        worker.name,
                    )
                    dead_workers.append(worker)
                    had_action = True

        # Execute deferred async actions (send_enter, revive)
        await self._execute_deferred_actions()

        if dead_workers:
            self._cleanup_dead_workers(dead_workers)

        if await self._run_periodic_tasks():
            had_action = True

        self._tick += 1
        return had_action, any_state_changed

    def _compute_backoff(self) -> float:
        """Compute poll interval based on worker states and idle streak."""
        return compute_backoff(
            workers=self.workers,
            config=self.drone_config,
            idle_streak=self._idle_streak,
            base_interval=self._base_interval,
            max_interval=self._max_interval,
            pressure_level=self._pressure_level,
            focused_workers=self._focused_workers,
            focus_interval=self._focus_interval,
        )

    def _handle_poll_error(self) -> None:
        """Track consecutive poll loop errors with escalating severity."""
        self._consecutive_errors += 1
        if self._consecutive_errors <= 5:
            _log.warning(
                "poll loop error (%d consecutive) — recovering next cycle",
                self._consecutive_errors,
                exc_info=True,
            )
        else:
            _log.error(
                "poll loop error (%d consecutive) — recovering next cycle",
                self._consecutive_errors,
                exc_info=True,
            )
        if self._consecutive_errors == 5:
            self.emit("poll_errors_exceeded", self._consecutive_errors)

    async def _loop(self) -> None:
        _log.info("poll loop started (enabled=%s, workers=%d)", self.enabled, len(self.workers))
        try:
            while self._running:
                backoff = self._base_interval
                try:
                    self._had_substantive_action = False
                    self._any_became_active = False
                    async with self._poll_lock:
                        _had_action, _any_changed = await self._poll_once_locked()

                        # Track idle streak for adaptive backoff.
                        if self._had_substantive_action or self._any_became_active:
                            self._idle_streak = 0
                        else:
                            self._idle_streak += 1

                        # Auto-terminate when all workers are gone
                        if not self.workers:
                            _log.warning("all workers gone — stopping pilot")
                            self.enabled = False
                            self._running = False
                            self.emit("hive_empty")
                            break

                        # Detect hive completion: all tasks done, all workers idle
                        if (
                            self.enabled
                            and self.drone_config.auto_stop_on_complete
                            and self.task_board
                            and self._saw_completion
                            and not self.task_board.available_tasks
                            and not self.task_board.active_tasks
                            and all(
                                w.display_state in (WorkerState.RESTING, WorkerState.SLEEPING)
                                for w in self.workers
                            )
                        ):
                            self._all_done_streak += 1
                            if self._all_done_streak >= 3:
                                _log.info("all tasks done, all workers idle — hive complete")
                                self.enabled = False
                                self._running = False
                                self.emit("hive_complete")
                                break
                        else:
                            self._all_done_streak = 0

                        backoff = self._compute_backoff()
                    self._consecutive_errors = 0
                except Exception:  # broad catch: poll loop must not die
                    self._handle_poll_error()

                await asyncio.sleep(backoff)
        except asyncio.CancelledError:
            _log.debug("poll loop cancelled (shutdown)")
            raise
        except BaseException:
            _log.error("poll loop terminated unexpectedly", exc_info=True)
            raise
        finally:
            _log.info("poll loop exited (running=%s)", self._running)
