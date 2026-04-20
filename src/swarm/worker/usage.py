"""Read LLM CLI session JSONL files to extract per-worker token usage."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from swarm.logging import get_logger
from swarm.worker.worker import TokenUsage

if TYPE_CHECKING:
    from swarm.providers.base import LLMProvider

_log = get_logger("worker.usage")


# Published Claude pricing per million tokens (as of 2025).
# Used to estimate worker cost from token counts.
@dataclass(frozen=True)
class PricingTier:
    """Per-million-token pricing for an LLM provider."""

    input: float  # per million tokens
    output: float
    cache_read: float
    cache_creation: float


_PRICING: dict[str, PricingTier] = {
    "claude": PricingTier(input=3.0, output=15.0, cache_read=0.30, cache_creation=3.75),
    "gemini": PricingTier(input=1.25, output=10.0, cache_read=0.315, cache_creation=4.50),
    "codex": PricingTier(input=2.50, output=10.0, cache_read=0.25, cache_creation=2.50),
}


def estimate_cost(usage: TokenUsage) -> float:
    """Estimate USD cost from token counts using published Claude pricing."""
    return estimate_cost_for_provider(usage, "claude")


def estimate_cost_for_provider(usage: TokenUsage, provider_name: str) -> float:
    """Estimate USD cost using provider-specific pricing (falls back to Claude)."""
    tier = _PRICING.get(provider_name, _PRICING["claude"])
    return (
        usage.input_tokens * tier.input
        + usage.output_tokens * tier.output
        + usage.cache_read_tokens * tier.cache_read
        + usage.cache_creation_tokens * tier.cache_creation
    ) / 1_000_000


def project_dir(worker_path: str) -> Path:
    """Encode a worker's project path to the Claude Code session directory.

    Claude Code stores sessions in ``~/.claude/projects/`` with the absolute
    path encoded by replacing ``/`` AND ``.`` with ``-``.  Both substitutions
    are required — paths with dots (``~/.swarm/queen/workdir``) encode as
    ``-home-user--swarm-queen-workdir`` (double-dash for ``/.``).  The
    previous implementation only replaced ``/``, so worker sessions in
    dotted paths (the Queen's workdir in particular) were never found and
    their usage panel rows sat at $0.
    """
    encoded = worker_path.replace("/", "-").replace(".", "-")
    return Path.home() / ".claude" / "projects" / encoded


def find_active_session(proj_dir: Path, since: float) -> Path | None:
    """Find the most recently modified JSONL session file.

    The ``since`` parameter used to gate candidates at ``mtime >= since``
    where ``since`` was ``daemon.start_time``.  Problem: on a daemon
    restart (systemctl, Reload), ``start_time`` resets to "now" and
    every pre-existing session file is filtered out as "stale" even
    though the workers are actively writing to them.  Result: usage
    numbers blanked to zero for minutes until the worker happened to
    emit a new turn, and the dashboard looked broken ("where did my
    usage go?").

    New behaviour: always return the most-recently-modified session
    file in the project directory.  ``since`` is retained as a no-op
    parameter so callers don't have to change.  If the worker hasn't
    written a session yet this still returns None.
    """
    if not proj_dir.is_dir():
        return None
    candidates: list[tuple[float, Path]] = []
    for p in proj_dir.glob("*.jsonl"):
        try:
            mtime = p.stat().st_mtime
        except OSError:
            continue
        candidates.append((mtime, p))
    if not candidates:
        return None
    candidates.sort(reverse=True)
    return candidates[0][1]


def _parse_turn_timestamp(raw: object) -> float | None:
    """Parse an ISO-8601 turn timestamp string into unix seconds.

    The Claude Code session format uses ``2026-04-19T00:13:17.102Z`` —
    Z-suffixed UTC, millisecond precision.  Returns None on any parse
    failure so callers can treat the turn as timeless (never filtered
    out by a ``since`` window).
    """
    if not isinstance(raw, str) or not raw:
        return None
    try:
        from datetime import datetime

        # ``fromisoformat`` in 3.11+ handles the Z suffix; normalise for 3.10.
        s = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
        return datetime.fromisoformat(s).timestamp()
    except ValueError:
        return None


def _turn_in_window(
    entry: dict[str, object],
    since: float | None,
    until: float | None,
) -> bool:
    """Return True when the assistant turn falls inside ``[since, until)``.

    No-window case (both None) returns True — callers can skip this
    check entirely via an outer ``if since is None and until is None``
    fast path.  Turns with no parseable timestamp are excluded from any
    bounded window so they don't inflate historical totals.
    """
    if since is None and until is None:
        return True
    ts = _parse_turn_timestamp(entry.get("timestamp"))
    if ts is None:
        return False
    if since is not None and ts < since:
        return False
    if until is not None and ts >= until:
        return False
    return True


def read_session_usage(
    jsonl_path: Path,
    since: float | None = None,
    until: float | None = None,
) -> TokenUsage:
    """Read a session JSONL file and sum token usage from assistant messages.

    Also tracks the last turn's ``input_tokens`` separately — this single
    value is the best proxy for current context window fill (cumulative
    totals grow monotonically and don't reflect compaction).

    When ``since`` / ``until`` are provided, only turns whose ISO-8601
    ``timestamp`` falls within ``[since, until)`` are counted.  Either
    endpoint is optional — passing only ``since`` is an open-ended
    "since T" window; passing both bounds a calendar month.  Turns
    without a parseable timestamp are skipped for the time-filter
    path so they don't inflate historical windows.  When both are
    None the full-session behaviour is preserved.
    """
    total = TokenUsage()
    last_input = 0
    try:
        with jsonl_path.open() as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if entry.get("type") != "assistant":
                    continue
                if not _turn_in_window(entry, since, until):
                    continue
                msg = entry.get("message", {})
                if not isinstance(msg, dict):
                    continue
                usage = msg.get("usage", {})
                if not isinstance(usage, dict):
                    continue
                turn_input = usage.get("input_tokens", 0)
                total.add(
                    TokenUsage(
                        input_tokens=turn_input,
                        output_tokens=usage.get("output_tokens", 0),
                        cache_read_tokens=usage.get("cache_read_input_tokens", 0),
                        cache_creation_tokens=usage.get("cache_creation_input_tokens", 0),
                    )
                )
                last_input = turn_input
    except OSError:
        _log.debug("failed to read session file: %s", jsonl_path)
    total.cost_usd = estimate_cost(total)
    total.last_turn_input_tokens = last_input
    return total


# Context window sizes per provider (tokens).
# These are the effective context window sizes available for conversation.
_CONTEXT_WINDOWS: dict[str, int] = {
    "claude": 1_000_000,  # Claude Opus/Sonnet with 1M context
    "gemini": 1_000_000,
    "codex": 200_000,
}


def estimate_context_usage(usage: TokenUsage, provider_name: str = "claude") -> float:
    """Estimate what fraction of the context window has been consumed (0.0 - 1.0).

    Uses the most recent turn's ``input_tokens`` as the best proxy for current
    context window fill.  Cumulative totals grow monotonically and don't reflect
    compaction, so a single turn's input_tokens is far more accurate.
    Falls back to cumulative input_tokens when last_turn is unavailable.
    """
    window = _CONTEXT_WINDOWS.get(provider_name, _CONTEXT_WINDOWS["claude"])
    if window <= 0:
        return 0.0
    context_tokens = usage.last_turn_input_tokens or usage.input_tokens
    return min(1.0, context_tokens / window)


def cache_read_ratio(usage: TokenUsage) -> float:
    """Fraction of cache tokens that were reads vs creations (0.0 - 1.0).

    Higher is better — means the prompt cache is being reused effectively.
    Returns 0.0 if no cache activity.
    """
    total = usage.cache_read_tokens + usage.cache_creation_tokens
    if total == 0:
        return 0.0
    return usage.cache_read_tokens / total


def get_worker_usage(
    worker_path: str,
    since: float,
    provider: LLMProvider | None = None,
    *,
    window_since: float | None = None,
    window_until: float | None = None,
) -> TokenUsage:
    """Get accumulated token usage for a worker from its session files.

    Uses ``provider.session_dir()`` when available; falls back to the
    Claude Code default path encoding.

    When ``window_since`` is None (default), returns the current
    session's full lifetime usage — preserves the historical contract
    used by ``_usage_refresh_loop`` and the unadorned ``/api/usage``.

    When ``window_since`` is a unix timestamp, aggregates per-turn usage
    from every session file in the project directory whose mtime is at
    or after ``window_since``, filtering individual turns by their
    ``timestamp`` field.  This is the path used by UI time filters
    (24h / 7d / 30d / last month / this month) — a long window can
    span multiple session files so the most-recent-only shortcut isn't
    enough.

    ``since`` (first positional arg) is the legacy "daemon start_time"
    parameter kept for compatibility; it's effectively a no-op since
    ``find_active_session`` now returns the most-recent session file
    regardless of age.
    """
    if provider is not None:
        sess_dir = provider.session_dir(worker_path)
        if sess_dir is None:
            return TokenUsage()
        proj = sess_dir
    else:
        proj = project_dir(worker_path)

    if window_since is None and window_until is None:
        session = find_active_session(proj, since)
        if not session:
            return TokenUsage()
        return read_session_usage(session)

    # Time-windowed path — aggregate across all session files that
    # could contain turns within the window.
    if not proj.is_dir():
        return TokenUsage()
    total = TokenUsage()
    last_input = 0
    latest_ts = 0.0
    for p in proj.glob("*.jsonl"):
        try:
            mtime = p.stat().st_mtime
        except OSError:
            continue
        # Files whose last-modified is before the window's start can't
        # contain in-window turns (turn timestamps are monotonic per
        # file).  Files with mtime after window_until MAY still contain
        # in-window turns at the start of the file — don't exclude them.
        if window_since is not None and mtime < window_since:
            continue
        partial = read_session_usage(p, since=window_since, until=window_until)
        total.add(partial)
        # Keep the last_turn_input_tokens from the most-recently-touched
        # session so the context-pct estimate still reflects "now".
        if mtime > latest_ts and partial.last_turn_input_tokens:
            last_input = partial.last_turn_input_tokens
            latest_ts = mtime
    total.cost_usd = estimate_cost(total)
    total.last_turn_input_tokens = last_input
    return total
