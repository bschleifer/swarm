"""Buzz decision rules — determine auto-pilot actions for each worker."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from swarm.config import BuzzConfig
from swarm.worker.state import has_choice_prompt, has_empty_prompt, has_idle_prompt
from swarm.worker.worker import Worker, WorkerState


class Decision(Enum):
    NONE = "none"
    CONTINUE = "continue"     # Send Enter (accept prompt, select default, continue)
    REVIVE = "revive"
    ESCALATE = "escalate"


@dataclass
class BuzzDecision:
    decision: Decision
    reason: str = ""


def decide(
    worker: Worker,
    content: str,
    config: BuzzConfig | None = None,
    escalated: set[str] | None = None,
) -> BuzzDecision:
    """Decide what auto-pilot action to take for a worker.

    Args:
        escalated: per-pilot set tracking which workers have been escalated.
                   If None, escalation tracking is disabled.
    """
    cfg = config or BuzzConfig()
    _esc = escalated if escalated is not None else set()

    if worker.state == WorkerState.STUNG:
        _esc.discard(worker.pane_id)
        if worker.revive_count >= cfg.max_revive_attempts:
            return BuzzDecision(
                Decision.ESCALATE,
                f"crash loop — {worker.revive_count} revives exhausted",
            )
        return BuzzDecision(Decision.REVIVE, "worker exited")

    if worker.state == WorkerState.BUZZING:
        _esc.discard(worker.pane_id)
        return BuzzDecision(Decision.NONE, "actively working")

    # Worker is RESTING — Claude Code uses Enter to accept/continue
    if has_choice_prompt(content):
        return BuzzDecision(Decision.CONTINUE, "choice menu — selecting default")

    if has_empty_prompt(content):
        return BuzzDecision(Decision.CONTINUE, "empty prompt — continuing")

    # Normal idle at Claude Code prompt — don't escalate, just wait
    if has_idle_prompt(content):
        return BuzzDecision(Decision.NONE, "idle at prompt")

    # Unknown/unrecognized prompt state — escalate to Queen
    if worker.resting_duration > cfg.escalation_threshold and worker.pane_id not in _esc:
        _esc.add(worker.pane_id)
        return BuzzDecision(Decision.ESCALATE, f"unrecognized state for {worker.resting_duration:.0f}s")

    return BuzzDecision(Decision.NONE, "resting, monitoring")
