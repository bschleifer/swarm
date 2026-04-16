"""Analyze MCP tool usage from the buzz log.

The buzz log records every MCP tool call as an entry whose ``detail``
field starts with ``"mcp:<tool_name> → <result_snippet>"``. This module
turns a raw stream of those entries into a per-tool summary so the
operator can see which tools are hot, which are error-prone, and
where ``swarm_*`` descriptions might need rewriting.

Per Anthropic's *Writing effective tools for agents — with agents*,
agent-built tools improve when their real-world usage is measured and
the descriptions are iterated. This module is the data-collection half
of that loop; the rewrite half (feeding these reports to a headless
Claude) is a future phase.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

# Matches the leading ``mcp:<tool>`` prefix written by
# ``src/swarm/mcp/tools.py:handle_tool_call``. The separator is a
# Unicode arrow (→) so splits on plain ``-`` don't false-match.
_MCP_PREFIX = re.compile(r"^mcp:([a-z0-9_]+)\s*(?:→\s*(.*))?$")

# Substrings that indicate an error result, case-insensitive. Kept
# loose because handlers return free-text messages ("Missing 'to' or
# 'content'", "No active task found.", etc.) without a structured
# status field.
_ERROR_SIGNALS = ("error", "unknown", "failed", "missing", "invalid", "no active")


@dataclass
class ToolStats:
    """Usage statistics for a single MCP tool over the analysis window."""

    tool: str
    calls: int = 0
    errors: int = 0
    workers: set[str] = field(default_factory=set)
    first_timestamp: float | None = None
    last_timestamp: float | None = None
    # Up to ``MAX_ERROR_SAMPLES`` distinct error snippets for quick triage.
    error_samples: list[str] = field(default_factory=list)

    MAX_ERROR_SAMPLES = 5

    @property
    def error_rate(self) -> float:
        return self.errors / self.calls if self.calls else 0.0

    def record(self, entry: dict[str, Any], snippet: str) -> None:
        self.calls += 1
        ts = entry.get("timestamp")
        if ts is not None:
            if self.first_timestamp is None or ts < self.first_timestamp:
                self.first_timestamp = ts
            if self.last_timestamp is None or ts > self.last_timestamp:
                self.last_timestamp = ts
        worker = entry.get("worker_name") or ""
        if worker:
            self.workers.add(worker)
        if _is_error_snippet(snippet):
            self.errors += 1
            self._sample_error(snippet)

    def _sample_error(self, snippet: str) -> None:
        snippet = snippet.strip()
        if not snippet or snippet in self.error_samples:
            return
        if len(self.error_samples) < self.MAX_ERROR_SAMPLES:
            self.error_samples.append(snippet)

    def to_report_row(self) -> dict[str, Any]:
        return {
            "tool": self.tool,
            "calls": self.calls,
            "errors": self.errors,
            "error_rate": round(self.error_rate, 3),
            "workers": sorted(self.workers),
            "first_timestamp": self.first_timestamp,
            "last_timestamp": self.last_timestamp,
            "error_samples": list(self.error_samples),
        }


def _is_error_snippet(snippet: str) -> bool:
    s = snippet.lower()
    return any(sig in s for sig in _ERROR_SIGNALS)


def parse_mcp_detail(detail: str) -> tuple[str, str] | None:
    """Parse an ``mcp:<tool> → <snippet>`` detail string.

    Returns ``(tool, snippet)`` or ``None`` if the detail isn't a
    recognised MCP call. Snippet is empty when the handler returned
    no text content.
    """
    m = _MCP_PREFIX.match(detail.strip())
    if not m:
        return None
    return m.group(1), (m.group(2) or "").strip()


def aggregate(entries: Iterable[dict[str, Any]]) -> list[ToolStats]:
    """Fold buzz-log entries into per-tool ``ToolStats``.

    Entries that don't match the ``mcp:*`` pattern are ignored — callers
    can pass the whole buzz log without pre-filtering.
    """
    stats: dict[str, ToolStats] = {}
    for entry in entries:
        detail = entry.get("detail") or ""
        parsed = parse_mcp_detail(detail)
        if parsed is None:
            continue
        tool, snippet = parsed
        s = stats.setdefault(tool, ToolStats(tool=tool))
        s.record(entry, snippet)
    # Stable sort: most-called first, ties broken alphabetically.
    return sorted(stats.values(), key=lambda s: (-s.calls, s.tool))
