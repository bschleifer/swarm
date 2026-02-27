"""Queen oversight — signal-triggered monitoring and intervention for workers."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any

from swarm.config import OversightConfig
from swarm.logging import get_logger
from swarm.worker.worker import WorkerState

if TYPE_CHECKING:
    from swarm.queen.queen import Queen
    from swarm.tasks.board import TaskBoard
    from swarm.tasks.task import SwarmTask
    from swarm.worker.worker import Worker

_log = get_logger("queen.oversight")


class SignalType(Enum):
    PROLONGED_BUZZING = "prolonged_buzzing"
    TASK_DRIFT = "task_drift"


class Severity(Enum):
    MINOR = "minor"
    MAJOR = "major"
    CRITICAL = "critical"


@dataclass
class OversightSignal:
    """A detected oversight signal requiring evaluation."""

    signal_type: SignalType
    worker_name: str
    description: str
    task_id: str = ""


@dataclass
class OversightResult:
    """Result of a Queen oversight evaluation."""

    signal: OversightSignal
    severity: Severity
    action: str  # "note", "redirect", "flag_human"
    message: str
    reasoning: str
    confidence: float = 0.0


class OversightMonitor:
    """Monitors workers for oversight signals and coordinates Queen evaluation.

    Signals are detected via cheap heuristics (state duration, content checks).
    When a signal fires, the Queen is consulted for semantic analysis and the
    intervention severity is determined.
    """

    def __init__(self, config: OversightConfig) -> None:
        self._config = config
        self._call_timestamps: list[float] = []
        # Workers already flagged for prolonged buzzing (reset when state changes)
        self._buzzing_notified: set[str] = set()
        # Per-worker last drift check timestamp
        self._last_drift_check: dict[str, float] = {}
        # Track interventions for status reporting
        self._interventions: list[dict[str, Any]] = field(default_factory=list)
        self._interventions = []

    @property
    def enabled(self) -> bool:
        return self._config.enabled

    def _within_rate_limit(self) -> bool:
        """Check if we can make another Queen oversight call this hour."""
        now = time.time()
        hour_ago = now - 3600
        self._call_timestamps = [t for t in self._call_timestamps if t > hour_ago]
        return len(self._call_timestamps) < self._config.max_calls_per_hour

    def _record_call(self) -> None:
        self._call_timestamps.append(time.time())

    def check_prolonged_buzzing(
        self, worker: Worker, task: SwarmTask | None
    ) -> OversightSignal | None:
        """Check if a worker has been BUZZING too long without progress."""
        threshold_s = self._config.buzzing_threshold_minutes * 60

        if worker.state != WorkerState.BUZZING:
            # Worker is no longer buzzing — clear the notification flag
            self._buzzing_notified.discard(worker.name)
            return None

        if worker.state_duration < threshold_s:
            return None

        # Already notified for this buzzing period
        if worker.name in self._buzzing_notified:
            return None

        self._buzzing_notified.add(worker.name)
        minutes = worker.state_duration / 60
        desc = f"Worker has been BUZZING for {minutes:.0f} minutes"
        if task:
            desc += f" on task '{task.title}'"

        return OversightSignal(
            signal_type=SignalType.PROLONGED_BUZZING,
            worker_name=worker.name,
            description=desc,
            task_id=task.id if task else "",
        )

    def check_task_drift(
        self,
        worker: Worker,
        task: SwarmTask | None,
        worker_output: str,
    ) -> OversightSignal | None:
        """Check if a worker may have drifted from its assigned task."""
        if not task or not worker_output:
            return None

        # Only check workers actively working
        if worker.state not in (WorkerState.BUZZING, WorkerState.RESTING):
            return None

        now = time.time()
        interval_s = self._config.drift_check_interval_minutes * 60
        last = self._last_drift_check.get(worker.name, 0.0)
        if now - last < interval_s:
            return None

        self._last_drift_check[worker.name] = now

        return OversightSignal(
            signal_type=SignalType.TASK_DRIFT,
            worker_name=worker.name,
            description=f"Periodic drift check for task '{task.title}'",
            task_id=task.id,
        )

    def collect_signals(
        self,
        workers: list[Worker],
        task_board: TaskBoard | None,
        worker_outputs: dict[str, str] | None = None,
    ) -> list[OversightSignal]:
        """Run all heuristic checks and return detected signals."""
        if not self.enabled:
            return []

        signals: list[OversightSignal] = []
        worker_outputs = worker_outputs or {}

        for worker in workers:
            task = None
            if task_board:
                active = task_board.active_tasks_for_worker(worker.name)
                task = active[0] if active else None

            # Signal 1: prolonged buzzing
            sig = self.check_prolonged_buzzing(worker, task)
            if sig:
                signals.append(sig)

            # Signal 2: task drift (only with task + output)
            output = worker_outputs.get(worker.name, "")
            sig = self.check_task_drift(worker, task, output)
            if sig:
                signals.append(sig)

        return signals

    async def evaluate_signal(
        self,
        signal: OversightSignal,
        queen: Queen,
        worker_output: str,
        task_info: str = "",
    ) -> OversightResult | None:
        """Ask the Queen to evaluate a signal and recommend an intervention.

        Returns None if rate-limited or Queen call fails.
        """
        if not self._within_rate_limit():
            _log.info(
                "oversight rate limited for %s (%s)",
                signal.worker_name,
                signal.signal_type.value,
            )
            return None

        prompt = self._build_evaluation_prompt(signal, worker_output, task_info)
        self._record_call()

        try:
            result = await queen.ask(prompt, stateless=True, force=True)
        except Exception:
            _log.warning("oversight Queen call failed for %s", signal.worker_name, exc_info=True)
            return None

        if not isinstance(result, dict) or "error" in result:
            _log.warning("oversight Queen returned error: %s", result)
            return None

        severity_str = result.get("severity", "minor")
        try:
            severity = Severity(severity_str)
        except ValueError:
            severity = Severity.MINOR

        action = result.get("action", "note")
        message = result.get("message", "")
        reasoning = result.get("reasoning", "")
        confidence = float(result.get("confidence", 0.0))

        oversight_result = OversightResult(
            signal=signal,
            severity=severity,
            action=action,
            message=message,
            reasoning=reasoning,
            confidence=confidence,
        )

        self._interventions.append(
            {
                "timestamp": time.time(),
                "worker": signal.worker_name,
                "signal": signal.signal_type.value,
                "severity": severity.value,
                "action": action,
                "message": message,
            }
        )
        # Keep only last 50 interventions
        if len(self._interventions) > 50:
            self._interventions = self._interventions[-50:]

        return oversight_result

    def _build_evaluation_prompt(
        self,
        signal: OversightSignal,
        worker_output: str,
        task_info: str,
    ) -> str:
        """Build the Queen prompt for evaluating an oversight signal."""
        task_section = ""
        if task_info:
            task_section = f"\n## Assigned Task\n{task_info}\n"

        signal_desc = {
            SignalType.PROLONGED_BUZZING: (
                "This worker has been actively processing (BUZZING) for an unusually "
                "long time without committing. They may be stuck, going in circles, "
                "or tackling an overly complex approach."
            ),
            SignalType.TASK_DRIFT: (
                "This worker may have drifted from their assigned task. Review their "
                "recent output to determine if they are still working on the correct "
                "objective or have gone off-track."
            ),
        }

        return f"""You are the Queen performing oversight on a worker.

## Signal Detected
Type: {signal.signal_type.value}
Worker: {signal.worker_name}
{signal.description}

{signal_desc.get(signal.signal_type, "")}
{task_section}
## Recent Worker Output
```
{worker_output[-3000:]}
```

Evaluate the situation and respond with ONLY a JSON object:
{{
  "severity": "minor" | "major" | "critical",
  "action": "note" | "redirect" | "flag_human",
  "message": "message to send to the worker or human operator",
  "reasoning": "why you chose this severity and action",
  "confidence": 0.0 to 1.0
}}

Severity guide:
- "minor": Worker is slightly off-track but recoverable with a gentle note
- "major": Worker has gone significantly off-track and needs redirection (pause + new instructions)
- "critical": Requires human attention (security concern, data loss risk, uncertainty)

Action guide:
- "note": Send a corrective note to the worker (minor issues)
- "redirect": Pause the worker and send redirect instructions (major issues)
- "flag_human": Flag for human review on the dashboard (critical issues)

IMPORTANT: If the worker appears to be making genuine progress (even if slow),
use severity "minor" with action "note" and a supportive message. Only escalate
if there is clear evidence of being stuck or drifting."""

    def get_status(self) -> dict[str, Any]:
        """Return oversight monitor status for API/dashboard."""
        now = time.time()
        hour_ago = now - 3600
        recent_calls = [t for t in self._call_timestamps if t > hour_ago]

        return {
            "enabled": self._config.enabled,
            "calls_this_hour": len(recent_calls),
            "max_calls_per_hour": self._config.max_calls_per_hour,
            "buzzing_threshold_minutes": self._config.buzzing_threshold_minutes,
            "drift_check_interval_minutes": self._config.drift_check_interval_minutes,
            "buzzing_notified": sorted(self._buzzing_notified),
            "recent_interventions": self._interventions[-10:],
        }

    def reset_worker(self, worker_name: str) -> None:
        """Reset oversight state for a worker (e.g., after state change)."""
        self._buzzing_notified.discard(worker_name)
        self._last_drift_check.pop(worker_name, None)
