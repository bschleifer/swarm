"""Drone decision rules — determine background drones actions for each worker."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING

from swarm.config import DroneConfig
from swarm.worker.worker import Worker, WorkerState

if TYPE_CHECKING:
    from swarm.providers.base import LLMProvider


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


_RE_READ_PATH = re.compile(r"Read\((.+?)\)")


def _get_safe_patterns(provider: LLMProvider | None) -> re.Pattern[str]:
    """Return the safe-tool regex, using provider override if available."""
    if provider is not None:
        return provider.safe_tool_patterns()
    from swarm.providers import get_provider

    return get_provider().safe_tool_patterns()


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


def _mark_escalated(_esc: dict[str, float], name: str) -> None:
    """Record escalation timestamp for a worker."""
    import time

    _esc[name] = time.monotonic()


def _decide_choice(
    worker: Worker,
    content: str,
    cfg: DroneConfig,
    _esc: dict[str, float],
    provider: LLMProvider | None = None,
) -> DroneDecision:
    """Decide action for a worker showing a choice menu."""
    # Use provider methods when available, fall back to default provider
    if provider is None:
        from swarm.providers import get_provider

        provider = get_provider()
    _get_choice_summary = provider.get_choice_summary
    _is_user_question = provider.is_user_question

    selected = _get_choice_summary(content)
    label = f"choice menu — selected '{selected}'" if selected else "choice menu"

    # AskUserQuestion prompts require user decision — never auto-continue
    if _is_user_question(content):
        if worker.name not in _esc:
            _mark_escalated(_esc, worker.name)
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
    safe_patterns = _get_safe_patterns(provider)
    if safe_patterns.search(prompt_area) and not _ALWAYS_ESCALATE.search(prompt_area):
        return DroneDecision(Decision.CONTINUE, f"safe operation: {label}", source="builtin")

    # Standard permission/tool prompts — check approval rules, then auto-continue.
    # Use trimmed prompt_area so rules match on the current prompt, not stale
    # output higher in the buffer.
    if cfg.approval_rules:
        ruling, matched_pattern, matched_index = _check_approval_rules(prompt_area, cfg)
        if ruling == Decision.ESCALATE:
            if worker.name not in _esc:
                _mark_escalated(_esc, worker.name)
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


def _decide_accept_edits(
    worker: Worker,
    content: str,
    _esc: dict[str, float],
) -> DroneDecision:
    """Decide action for an 'accept edits' prompt.

    File-only edits are safe to auto-accept.  Prompts that include bash
    commands (e.g. "accept edits on · 2 bashes") require operator approval.
    """
    tail = "\n".join(content.strip().splitlines()[-5:]).lower()
    if "bash" in tail:
        if worker.name not in _esc:
            _mark_escalated(_esc, worker.name)
        return DroneDecision(
            Decision.ESCALATE,
            "accept edits includes bash commands — needs operator approval",
            source="builtin",
        )
    return DroneDecision(
        Decision.CONTINUE,
        "accept edits (files only) — auto-accepting",
        source="builtin",
    )


def _decide_idle_state(
    worker: Worker,
    content: str,
    cfg: DroneConfig,
    _esc: dict[str, float],
    provider: LLMProvider | None = None,
) -> DroneDecision:
    """Decide action for a RESTING worker based on worker output."""
    # Use provider methods when available, fall back to default provider
    if provider is None:
        from swarm.providers import get_provider

        provider = get_provider()
    _has_plan_prompt = provider.has_plan_prompt
    _has_choice_prompt = provider.has_choice_prompt
    _has_empty_prompt = provider.has_empty_prompt
    _has_accept_edits_prompt = provider.has_accept_edits_prompt
    _has_idle_prompt = provider.has_idle_prompt

    # Plan approval prompts always escalate — never auto-approve plans
    if _has_plan_prompt(content):
        if worker.name not in _esc:
            _mark_escalated(_esc, worker.name)
            return DroneDecision(
                Decision.ESCALATE, "plan requires user approval", source="escalation"
            )
        return DroneDecision(Decision.NONE, "plan — already escalated, awaiting user")

    if _has_choice_prompt(content):
        return _decide_choice(worker, content, cfg, _esc, provider=provider)

    # Check idle/suggestion hints BEFORE empty prompt — a suggestion at the
    # idle prompt can look like an empty prompt line, but `? for shortcuts`
    # (or `ctrl+t to hide`) in the tail means the user has a suggestion
    # pre-filled.  Only the operator should press Enter on those.
    # (Use a narrow hints-only check here; the full has_idle_prompt is broader
    # and would false-positive on normal `>` prompts.)
    tail_lower = "\n".join(content.strip().splitlines()[-5:]).lower()
    if "? for shortcuts" in tail_lower or "ctrl+t to hide" in tail_lower:
        return DroneDecision(Decision.NONE, "idle at prompt")

    if _has_empty_prompt(content):
        return DroneDecision(Decision.NONE, "empty prompt — idle")

    if _has_accept_edits_prompt(content):
        return _decide_accept_edits(worker, content, _esc)

    if _has_idle_prompt(content):
        return DroneDecision(Decision.NONE, "idle at prompt")

    # Unknown/unrecognized prompt state — escalate to Queen
    if worker.resting_duration > cfg.escalation_threshold and worker.name not in _esc:
        _mark_escalated(_esc, worker.name)
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
    escalated: dict[str, float] | None = None,
    provider: LLMProvider | None = None,
) -> DroneDecision:
    """Decide what background drones action to take for a worker.

    Args:
        escalated: per-pilot dict tracking which workers have been escalated
                   (name → monotonic escalation time).
                   If None, escalation tracking is disabled.
        provider: LLM provider for provider-specific detection patterns.
                  If None, uses Claude Code defaults via state.py.
    """
    cfg = config or DroneConfig()
    _esc = escalated if escalated is not None else {}

    if worker.state == WorkerState.STUNG:
        if worker.revive_count >= cfg.max_revive_attempts:
            if worker.name not in _esc:
                _mark_escalated(_esc, worker.name)
                return DroneDecision(
                    Decision.ESCALATE,
                    f"crash loop — {worker.revive_count} revives exhausted",
                )
            return DroneDecision(Decision.NONE, "crash loop — already escalated, awaiting user")
        return DroneDecision(Decision.REVIVE, "worker exited")

    if worker.state == WorkerState.BUZZING:
        _esc.pop(worker.name, None)
        return DroneDecision(Decision.NONE, "actively working")

    # Both RESTING and WAITING workers need prompt evaluation
    return _decide_idle_state(worker, content, cfg, _esc, provider=provider)
