"""Drone decision rules — determine background drones actions for each worker."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from swarm.config import DroneConfig
from swarm.worker.state import has_choice_prompt, has_empty_prompt, has_idle_prompt
from swarm.worker.worker import Worker, WorkerState


class Decision(Enum):
    NONE = "none"
    CONTINUE = "continue"  # Send Enter (accept prompt, select default, continue)
    REVIVE = "revive"
    ESCALATE = "escalate"


@dataclass
class DroneDecision:
    decision: Decision
    reason: str = ""


def decide(
    worker: Worker,
    content: str,
    config: DroneConfig | None = None,
    escalated: set[str] | None = None,
) -> DroneDecision:
    """Decide what background drones action to take for a worker.

    Args:
        escalated: per-pilot set tracking which workers have been escalated.
                   If None, escalation tracking is disabled.
    """
    cfg = config or DroneConfig()
    _esc = escalated if escalated is not None else set()

    if worker.state == WorkerState.STUNG:
        _esc.discard(worker.pane_id)
        if worker.revive_count >= cfg.max_revive_attempts:
            _esc.add(worker.pane_id)
            return DroneDecision(
                Decision.ESCALATE,
                f"crash loop — {worker.revive_count} revives exhausted",
            )
        return DroneDecision(Decision.REVIVE, "worker exited")

    if worker.state == WorkerState.BUZZING:
        _esc.discard(worker.pane_id)
        return DroneDecision(Decision.NONE, "actively working")

    # Worker is RESTING — Claude Code uses Enter to accept/continue
    if has_choice_prompt(content):
        return DroneDecision(Decision.CONTINUE, "choice menu — selecting default")

    if has_empty_prompt(content):
        return DroneDecision(Decision.CONTINUE, "empty prompt — continuing")

    # Normal idle at Claude Code prompt — don't escalate, just wait
    if has_idle_prompt(content):
        return DroneDecision(Decision.NONE, "idle at prompt")

    # Unknown/unrecognized prompt state — escalate to Queen
    if worker.resting_duration > cfg.escalation_threshold and worker.pane_id not in _esc:
        _esc.add(worker.pane_id)
        return DroneDecision(
            Decision.ESCALATE,
            f"unrecognized state for {worker.resting_duration:.0f}s",
        )

    return DroneDecision(Decision.NONE, "resting, monitoring")
