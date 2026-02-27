"""Drone auto-tuning — learns from user overrides to suggest config changes.

Analyzes the SQLite decision log for patterns where the user's actions
contradict what the drone decided, and generates actionable suggestions
for improving the swarm.yaml drone configuration.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum

from swarm.drones.store import LogStore
from swarm.logging import get_logger

_log = get_logger("drones.tuning")

# Minimum number of overrides before suggesting a config change
_MIN_OVERRIDES_FOR_SUGGESTION = 3
# Default lookback window for analysis (7 days)
_DEFAULT_ANALYSIS_WINDOW_DAYS = 7


class OverrideType(Enum):
    """Types of user overrides that indicate drone misconfiguration."""

    REJECTED_APPROVAL = "rejected_approval"  # User interrupted/rejected a drone auto-approval
    APPROVED_AFTER_SKIP = "approved_after_skip"  # User manually approved when drone didn't act
    REDIRECTED_WORKER = "redirected_worker"  # User sent instructions drone didn't flag as needed


class SuggestionStatus(Enum):
    PENDING = "pending"
    APPLIED = "applied"
    DISMISSED = "dismissed"


@dataclass
class TuningSuggestion:
    """A suggested configuration change based on override patterns."""

    id: str
    description: str
    config_path: str  # dot-separated path in swarm.yaml, e.g. "drones.escalation_threshold"
    current_value: object
    suggested_value: object
    reason: str
    override_count: int
    total_decisions: int
    override_rate: float  # 0.0 - 1.0
    status: SuggestionStatus = SuggestionStatus.PENDING
    created_at: float = field(default_factory=time.time)


def record_override(
    store: LogStore,
    *,
    worker_name: str,
    override_type: OverrideType,
    detail: str = "",
) -> int | None:
    """Record a user override against the most recent drone decision.

    Finds the most recent non-overridden drone decision for the worker
    and marks it as overridden.

    Returns the store row ID if an entry was marked, None otherwise.
    """
    # Map override types to the drone actions they typically contradict
    action_filters = {
        OverrideType.REJECTED_APPROVAL: ["CONTINUED"],
        OverrideType.APPROVED_AFTER_SKIP: [
            "ESCALATED",
            "PROPOSED_ASSIGNMENT",
            "PROPOSED_COMPLETION",
        ],
        OverrideType.REDIRECTED_WORKER: None,  # Any recent action
    }

    return store.mark_recent_overridden(
        worker_name,
        f"{override_type.value}: {detail}" if detail else override_type.value,
        within_seconds=300.0,
        action_filter=action_filters.get(override_type),
    )


def analyze_overrides(
    store: LogStore,
    *,
    days: int = _DEFAULT_ANALYSIS_WINDOW_DAYS,
) -> list[TuningSuggestion]:
    """Analyze override patterns and generate tuning suggestions.

    Examines the decision log for systematic patterns where the user's
    overrides indicate the drone config needs adjustment.
    """
    since = time.time() - (days * 86400)
    suggestions: list[TuningSuggestion] = []

    # Pattern 1: High escalation override rate
    # "You approve >60% of escalations" → threshold too low
    _check_escalation_overrides(store, since, suggestions)

    # Pattern 2: High auto-approval override rate
    # "You interrupt >40% of auto-approvals" → rules too permissive
    _check_approval_overrides(store, since, suggestions)

    # Pattern 3: Frequent worker redirections
    # "You redirect worker X >3 times" → oversight signals missed
    _check_redirect_overrides(store, since, suggestions)

    return suggestions


def _check_escalation_overrides(
    store: LogStore,
    since: float,
    suggestions: list[TuningSuggestion],
) -> None:
    """Check if escalation threshold is too conservative."""
    total_escalations = store.count(action="ESCALATED", since=since)
    if total_escalations < _MIN_OVERRIDES_FOR_SUGGESTION:
        return

    overridden_escalations = store.count(action="ESCALATED", since=since, overridden=True)
    if overridden_escalations < _MIN_OVERRIDES_FOR_SUGGESTION:
        return

    rate = overridden_escalations / total_escalations
    if rate > 0.6:
        suggestions.append(
            TuningSuggestion(
                id=f"escalation_threshold_{int(time.time())}",
                description="Escalation threshold may be too conservative",
                config_path="drones.escalation_threshold",
                current_value="(current)",
                suggested_value="(increase by 30%)",
                reason=(
                    f"You manually approved {overridden_escalations} of "
                    f"{total_escalations} escalations ({rate:.0%}) in the last "
                    f"{int((time.time() - since) / 86400)} days. Consider raising "
                    f"the escalation threshold so fewer decisions need your input."
                ),
                override_count=overridden_escalations,
                total_decisions=total_escalations,
                override_rate=rate,
            )
        )


def _check_approval_overrides(
    store: LogStore,
    since: float,
    suggestions: list[TuningSuggestion],
) -> None:
    """Check if auto-approval rules are too permissive."""
    total_approvals = store.count(action="CONTINUED", since=since)
    if total_approvals < _MIN_OVERRIDES_FOR_SUGGESTION:
        return

    overridden_approvals = store.count(action="CONTINUED", since=since, overridden=True)
    if overridden_approvals < _MIN_OVERRIDES_FOR_SUGGESTION:
        return

    rate = overridden_approvals / total_approvals
    if rate > 0.4:
        suggestions.append(
            TuningSuggestion(
                id=f"approval_rules_{int(time.time())}",
                description="Auto-approval rules may be too permissive",
                config_path="drones.approval_rules",
                current_value="(current rules)",
                suggested_value="(add escalation rules for overridden patterns)",
                reason=(
                    f"You interrupted or rejected {overridden_approvals} of "
                    f"{total_approvals} auto-approvals ({rate:.0%}) in the last "
                    f"{int((time.time() - since) / 86400)} days. Consider adding "
                    f"escalation rules for the patterns you keep overriding."
                ),
                override_count=overridden_approvals,
                total_decisions=total_approvals,
                override_rate=rate,
            )
        )


def _check_redirect_overrides(
    store: LogStore,
    since: float,
    suggestions: list[TuningSuggestion],
) -> None:
    """Check for workers that frequently need manual redirection."""
    # Get all redirections from the override log
    redirects = store.query(
        since=since,
        overridden=True,
        limit=500,
    )
    redirect_entries = [
        r
        for r in redirects
        if r.get("override_action", "").startswith(OverrideType.REDIRECTED_WORKER.value)
    ]
    if len(redirect_entries) < _MIN_OVERRIDES_FOR_SUGGESTION:
        return

    # Count per worker
    worker_counts: dict[str, int] = {}
    for r in redirect_entries:
        wn = r["worker_name"]
        worker_counts[wn] = worker_counts.get(wn, 0) + 1

    for worker_name, count in worker_counts.items():
        if count >= _MIN_OVERRIDES_FOR_SUGGESTION:
            suggestions.append(
                TuningSuggestion(
                    id=f"redirect_{worker_name}_{int(time.time())}",
                    description=f"Worker '{worker_name}' frequently needs redirection",
                    config_path="queen.oversight.enabled",
                    current_value=False,
                    suggested_value=True,
                    reason=(
                        f"You manually redirected worker '{worker_name}' {count} times "
                        f"in the last {int((time.time() - since) / 86400)} days. "
                        f"Consider enabling Queen oversight for this worker."
                    ),
                    override_count=count,
                    total_decisions=count,
                    override_rate=1.0,
                )
            )
