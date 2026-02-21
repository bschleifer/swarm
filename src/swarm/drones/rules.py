"""Drone decision rules — determine background drones actions for each worker."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from swarm.config import DroneConfig
from swarm.worker.state import (
    get_choice_summary,
    has_accept_edits_prompt,
    has_choice_prompt,
    has_empty_prompt,
    has_idle_prompt,
    has_plan_prompt,
    is_user_question,
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
    rule_pattern: str = ""  # regex pattern that matched (test mode enrichment)
    rule_index: int = -1  # index in approval_rules (-1 = no match)
    source: str = ""  # "builtin", "rule", or "escalation" — distinguishes decision origin


# Patterns that ALWAYS escalate — never auto-approve regardless of user rules.
# Must be specific to genuinely destructive operations. Do NOT include words
# like "production" or "database" that appear in normal connection strings.
_ALWAYS_ESCALATE = re.compile(
    r"DROP\s+(TABLE|DATABASE|INDEX|SCHEMA|COLUMN)"
    r"|TRUNCATE\s"
    r"|ALTER\s+(TABLE|DATABASE)\s"
    r"|DELETE\s+FROM\s+\S+\s*;"  # DELETE without WHERE
    r"|rm\s+-rf\s"
    r"|git\s+(push\s+.*--force|reset\s+--hard)"
    r"|git\s+push\s+\S+\s+(main|master)\b"
    r"|--no-verify",
    re.IGNORECASE,
)


# Built-in patterns for safe read-only operations that should never need
# Queen escalation.  Checked before approval_rules in _decide_choice so
# common tool prompts (Bash ls, Glob, Grep, etc.) are fast-approved.
_BUILTIN_SAFE_PATTERNS = re.compile(
    r"Bash\(.*(ls|cat|head|tail|find|wc|stat|file|which|pwd|echo|date)\b"
    r"|Bash\(.*git\s+(status|log|diff|show|branch|remote|tag)\b"
    r"|Bash\(.*uv\s+run\s+(pytest|ruff)\b"
    r"|Glob\("
    r"|Grep\("
    r"|Read\("
    r"|WebSearch\("
    r"|WebFetch\(",
    re.IGNORECASE,
)

_RE_READ_PATH = re.compile(r"Read\((.+?)\)")


def _is_allowed_read(content: str, allowed_paths: list[str]) -> bool:
    """Check if a Read operation targets an allowed directory.

    Uses the *last* ``Read(path)`` match in the worker output so that older
    Read operations higher in the scrollback don't shadow the current prompt.

    Uses Path.resolve() to prevent path traversal (e.g. ``../../../etc/passwd``).
    """
    matches = _RE_READ_PATH.findall(content)
    if not matches:
        return False
    # Check the last match — the one closest to the active prompt
    target = Path(os.path.expanduser(matches[-1])).resolve()
    for prefix in allowed_paths:
        allowed = Path(os.path.expanduser(prefix)).resolve()
        try:
            target.relative_to(allowed)
            return True
        except ValueError:
            continue
    return False


def _check_approval_rules(choice_text: str, config: DroneConfig) -> tuple[Decision, str, int]:
    """First-match-wins rule evaluation.  Falls back to ESCALATE (safe default).

    Built-in safety patterns always escalate regardless of user rules.

    Returns (decision, matched_pattern, matched_index).
    """
    # Safety net: always escalate dangerous operations
    if _ALWAYS_ESCALATE.search(choice_text):
        return Decision.ESCALATE, "_ALWAYS_ESCALATE", -1

    for idx, rule in enumerate(config.approval_rules):
        if re.search(rule.pattern, choice_text, re.IGNORECASE | re.MULTILINE):
            decision = Decision.ESCALATE if rule.action == "escalate" else Decision.CONTINUE
            return decision, rule.pattern, idx
    # No match → escalate (fail-safe); users can add explicit approve rules
    return Decision.ESCALATE, "", -1


def _decide_choice(worker: Worker, content: str, cfg: DroneConfig, _esc: set[str]) -> DroneDecision:
    """Decide action for a worker showing a choice menu."""
    selected = get_choice_summary(content)
    label = f"choice menu — selected '{selected}'" if selected else "choice menu"

    # AskUserQuestion prompts require user decision — never auto-continue
    if is_user_question(content):
        if worker.name not in _esc:
            _esc.add(worker.name)
            return DroneDecision(Decision.ESCALATE, f"user question: {label}", source="escalation")
        return DroneDecision(Decision.NONE, "user question — already escalated, awaiting user")

    # Trim to last 30 lines for pattern matching — prevents stale output
    # (e.g. old "plan" text) from triggering rules on unrelated prompts.
    lines = content.strip().splitlines()
    prompt_area = "\n".join(lines[-30:])

    # Read operations from allowed directories — auto-approve without rules check
    if cfg.allowed_read_paths and _is_allowed_read(content, cfg.allowed_read_paths):
        return DroneDecision(
            Decision.CONTINUE, f"read from allowed path: {label}", source="builtin"
        )

    # Built-in safe operations (ls, Read, Glob, Grep, etc.) — fast-approve
    # before hitting approval_rules to avoid unnecessary Queen escalation.
    # Safety patterns are still checked first (in _check_approval_rules).
    if _BUILTIN_SAFE_PATTERNS.search(prompt_area) and not _ALWAYS_ESCALATE.search(prompt_area):
        return DroneDecision(Decision.CONTINUE, f"safe operation: {label}", source="builtin")

    # Standard permission/tool prompts — check approval rules, then auto-continue.
    # Use trimmed prompt_area so rules match on the current prompt, not stale
    # output higher in the buffer.
    if cfg.approval_rules:
        ruling, matched_pattern, matched_index = _check_approval_rules(prompt_area, cfg)
        if ruling == Decision.ESCALATE:
            if worker.name not in _esc:
                _esc.add(worker.name)
                return DroneDecision(
                    Decision.ESCALATE,
                    f"choice requires approval: {label}",
                    rule_pattern=matched_pattern,
                    rule_index=matched_index,
                    source="rule",
                )
            return DroneDecision(Decision.NONE, "choice — already escalated, awaiting user")
        return DroneDecision(
            Decision.CONTINUE,
            label,
            rule_pattern=matched_pattern,
            rule_index=matched_index,
            source="rule",
        )
    return DroneDecision(Decision.CONTINUE, label, source="builtin")


def _decide_resting(
    worker: Worker, content: str, cfg: DroneConfig, _esc: set[str]
) -> DroneDecision:
    """Decide action for a RESTING worker based on worker output."""
    # Plan approval prompts always escalate — never auto-approve plans
    if has_plan_prompt(content):
        if worker.name not in _esc:
            _esc.add(worker.name)
            return DroneDecision(
                Decision.ESCALATE, "plan requires user approval", source="escalation"
            )
        return DroneDecision(Decision.NONE, "plan — already escalated, awaiting user")

    if has_choice_prompt(content):
        return _decide_choice(worker, content, cfg, _esc)

    if has_empty_prompt(content):
        return DroneDecision(Decision.CONTINUE, "empty prompt — continuing", source="builtin")

    if has_accept_edits_prompt(content):
        return DroneDecision(
            Decision.CONTINUE, "accept edits prompt — auto-accepting", source="builtin"
        )

    if has_idle_prompt(content):
        return DroneDecision(Decision.NONE, "idle at prompt")

    # Unknown/unrecognized prompt state — escalate to Queen
    if worker.resting_duration > cfg.escalation_threshold and worker.name not in _esc:
        _esc.add(worker.name)
        return DroneDecision(
            Decision.ESCALATE,
            f"unrecognized state for {worker.resting_duration:.0f}s",
            source="escalation",
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
        if worker.revive_count >= cfg.max_revive_attempts:
            if worker.name not in _esc:
                _esc.add(worker.name)
                return DroneDecision(
                    Decision.ESCALATE,
                    f"crash loop — {worker.revive_count} revives exhausted",
                )
            return DroneDecision(Decision.NONE, "crash loop — already escalated, awaiting user")
        return DroneDecision(Decision.REVIVE, "worker exited")

    if worker.state == WorkerState.BUZZING:
        _esc.discard(worker.name)
        return DroneDecision(Decision.NONE, "actively working")

    # Both RESTING and WAITING workers need prompt evaluation
    return _decide_resting(worker, content, cfg, _esc)
