"""Drone decision rules — determine background drones actions for each worker."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

from swarm.config import DroneConfig
from swarm.worker.state import (
    get_choice_summary,
    has_choice_prompt,
    has_empty_prompt,
    has_idle_prompt,
    has_plan_prompt,
)
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


def _check_approval_rules(choice_text: str, config: DroneConfig) -> Decision:
    """First-match-wins rule evaluation. Falls back to CONTINUE."""
    for rule in config.approval_rules:
        if re.search(rule.pattern, choice_text, re.IGNORECASE):
            return Decision.ESCALATE if rule.action == "escalate" else Decision.CONTINUE
    return Decision.CONTINUE


def _decide_resting(
    worker: Worker, content: str, cfg: DroneConfig, _esc: set[str]
) -> DroneDecision:
    """Decide action for a RESTING worker based on pane content."""
    # Plan approval prompts always escalate — never auto-approve plans
    if has_plan_prompt(content):
        return DroneDecision(Decision.ESCALATE, "plan requires user approval")

    if has_choice_prompt(content):
        selected = get_choice_summary(content)
        label = f"choice menu — selected '{selected}'" if selected else "choice menu"
        if cfg.approval_rules:
            ruling = _check_approval_rules(selected or content, cfg)
            if ruling == Decision.ESCALATE:
                return DroneDecision(Decision.ESCALATE, f"choice requires approval: {label}")
        return DroneDecision(Decision.CONTINUE, label)

    if has_empty_prompt(content):
        return DroneDecision(Decision.CONTINUE, "empty prompt — continuing")

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

    return _decide_resting(worker, content, cfg, _esc)
