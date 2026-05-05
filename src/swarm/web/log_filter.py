"""Severity-hierarchy filter for the dashboard's Logs tab.

Extracted from ``swarm.web.routes.partials`` so the helpers are
importable without dragging in the full web stack — partials.py
imports from ``swarm.web.app`` which round-trips back through
the partials registration, so unit-testing helpers in-place was
hitting a circular import at collection time.

The contract: picking a severity level shows that level and above,
mirroring how Python's logging module treats threshold severities.
Operator picks ``INFO`` → sees INFO + WARNING + ERROR; ``ERROR`` →
sees only ERROR; ``DEBUG`` → sees everything.

Pre-extraction the partials handler did a naive substring match
(``level_filter in ln``), which silently dropped WARNING / ERROR
lines whenever INFO was selected — the bug Brad's dashboard audit
flagged.
"""

from __future__ import annotations

# Maps a chosen filter level to the set of LEVEL tokens that should
# match.  Every log line begins with ``[LEVEL]`` so an exact bracketed
# membership check is unambiguous (a payload word like "Got INFO from
# upstream" can't false-positive a DEBUG line).
LOG_LEVEL_INCLUSIVE: dict[str, frozenset[str]] = {
    "DEBUG": frozenset({"DEBUG", "INFO", "WARNING", "ERROR"}),
    "INFO": frozenset({"INFO", "WARNING", "ERROR"}),
    "WARNING": frozenset({"WARNING", "ERROR"}),
    "ERROR": frozenset({"ERROR"}),
}


def line_matches_level(line: str, allowed: frozenset[str]) -> bool:
    """Return True if ``line`` is at or above the selected severity."""
    return any(f"[{lvl}]" in line for lvl in allowed)
