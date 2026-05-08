"""Tests for the Dreamer drone — pattern-mining over buzz_log + messages.

The Dreamer is a periodic drone that scans the buzz log for recurring
failure / oversight patterns and writes them into ``queen_learnings``
tagged ``discovered_by_dreamer:{key}`` so workers and the Queen surface
them through the existing learnings tools.

v1 is deterministic — no LLM call. Bucketing is by (action, normalized
detail prefix); a bucket becomes a learning when it crosses
``min_pattern_count`` AND involves at least 2 distinct workers (so a
single chatty worker can't manufacture a "pattern" by itself).
"""

from __future__ import annotations

import time
from typing import Any
from unittest.mock import MagicMock

import pytest

from swarm.config import DroneConfig
from swarm.drones.dreamer import Dreamer
from swarm.drones.log import LogCategory, SystemAction


def _buzz(
    *,
    action: str,
    worker: str,
    detail: str,
    timestamp: float,
    category: str = "drone",
) -> dict[str, Any]:
    """Construct a fake buzz_log row matching ``BuzzStore.query`` output."""
    return {
        "id": 0,
        "timestamp": timestamp,
        "action": action,
        "worker_name": worker,
        "detail": detail,
        "category": category,
        "is_notification": False,
        "metadata": {},
        "repeat_count": 1,
    }


class _BuzzStub:
    """Minimal BuzzStore stand-in.

    Only ``query(category=..., since=..., limit=...)`` is exercised. The
    stub returns whatever rows fall inside the time window — categorical
    and action filters are applied client-side by the dreamer for v1
    simplicity.
    """

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def query(
        self,
        *,
        worker_name: str | None = None,
        action: str | None = None,
        category: str | None = None,
        since: float | None = None,
        until: float | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        out = list(self._rows)
        if since is not None:
            out = [r for r in out if r["timestamp"] >= since]
        if until is not None:
            out = [r for r in out if r["timestamp"] <= until]
        if category is not None:
            out = [r for r in out if r["category"] == category]
        if action is not None:
            out = [r for r in out if r["action"] == action]
        if worker_name is not None:
            out = [r for r in out if r["worker_name"] == worker_name]
        return out[:limit]


class _LearningsStub:
    """Minimal QueenChatStore stand-in for the learnings surface.

    Captures ``add_learning`` calls for assertions, and supports
    ``query_learnings(applied_to=...)`` for the dedupe path.
    """

    def __init__(self) -> None:
        self.added: list[dict[str, Any]] = []
        # pre-seeded learnings keyed by ``applied_to`` for dedupe tests
        self._existing: list[MagicMock] = []

    def seed(self, *, applied_to: str, age_seconds: float = 0.0) -> None:
        m = MagicMock()
        m.applied_to = applied_to
        m.created_at = time.time() - age_seconds
        m.context = "seed"
        m.correction = "seed"
        self._existing.append(m)

    def add_learning(
        self,
        *,
        context: str,
        correction: str,
        applied_to: str = "",
        thread_id: str | None = None,
    ) -> MagicMock:
        record = {
            "context": context,
            "correction": correction,
            "applied_to": applied_to,
            "thread_id": thread_id,
            "created_at": time.time(),
        }
        self.added.append(record)
        m = MagicMock()
        m.id = len(self.added)
        m.context = context
        m.correction = correction
        m.applied_to = applied_to
        m.thread_id = thread_id
        m.created_at = record["created_at"]
        return m

    def query_learnings(
        self,
        *,
        applied_to: str | None = None,
        search: str | None = None,
        limit: int = 50,
    ) -> list[MagicMock]:
        if applied_to is None:
            return list(self._existing)
        return [r for r in self._existing if r.applied_to == applied_to][:limit]


def _dreamer(
    *,
    rows: list[dict[str, Any]] | None = None,
    learnings: _LearningsStub | None = None,
    interval: float = 60.0,
    lookback_hours: float = 24.0,
    min_pattern_count: int = 3,
) -> tuple[Dreamer, _LearningsStub, MagicMock]:
    buzz = _BuzzStub(rows or [])
    store = learnings or _LearningsStub()
    drone_log = MagicMock()
    cfg = DroneConfig(
        dreamer_interval_seconds=interval,
        dreamer_lookback_hours=lookback_hours,
        dreamer_min_pattern_count=min_pattern_count,
    )
    d = Dreamer(
        drone_config=cfg,
        buzz_store=buzz,
        learnings_store=store,
        drone_log=drone_log,
    )
    return d, store, drone_log


# ---------------------------------------------------------------------------
# Enabled / disabled
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dreamer_disabled_when_interval_zero() -> None:
    d, store, drone_log = _dreamer(interval=0.0)
    assert d.enabled is False
    written = await d.sweep(now=1000.0)
    assert written == 0
    assert store.added == []


@pytest.mark.asyncio
async def test_dreamer_due_throttling() -> None:
    """Calling sweep more often than the interval is a no-op."""
    now_ts = time.time()
    rows = [
        _buzz(
            action="VERIFIER_TIER2_REOPENED",
            worker="alpha",
            detail="diff missed acceptance criterion 'returns 200 for new tasks'",
            timestamp=now_ts - 60,
        ),
        _buzz(
            action="VERIFIER_TIER2_REOPENED",
            worker="beta",
            detail="diff missed acceptance criterion 'returns 200 for new tasks'",
            timestamp=now_ts - 90,
        ),
        _buzz(
            action="VERIFIER_TIER2_REOPENED",
            worker="alpha",
            detail="diff missed acceptance criterion 'returns 200 for new tasks'",
            timestamp=now_ts - 120,
        ),
    ]
    d, store, _ = _dreamer(rows=rows, interval=600.0)
    first = await d.sweep(now=1000.0)
    assert first == 1
    # second call within the interval — nothing more written
    second = await d.sweep(now=1100.0)
    assert second == 0
    assert len(store.added) == 1


# ---------------------------------------------------------------------------
# Pattern detection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dreamer_writes_learning_for_clustered_failures() -> None:
    """Three matching reopens across two workers → exactly one learning."""
    now_ts = time.time()
    rows = [
        _buzz(
            action="VERIFIER_TIER2_REOPENED",
            worker="alpha",
            detail="diff missed criterion 'returns 200 for new tasks' on #101",
            timestamp=now_ts - 1000,
        ),
        _buzz(
            action="VERIFIER_TIER2_REOPENED",
            worker="beta",
            detail="diff missed criterion 'returns 200 for new tasks' on #102",
            timestamp=now_ts - 2000,
        ),
        _buzz(
            action="VERIFIER_TIER2_REOPENED",
            worker="alpha",
            detail="diff missed criterion 'returns 200 for new tasks' on #103",
            timestamp=now_ts - 3000,
        ),
    ]
    d, store, _ = _dreamer(rows=rows)

    written = await d.sweep(now=1000.0)

    assert written == 1
    assert len(store.added) == 1
    learning = store.added[0]
    assert learning["applied_to"].startswith("discovered_by_dreamer:")
    assert "VERIFIER_TIER2_REOPENED" in learning["applied_to"]
    # The normalized signature key in the tag must NOT carry per-task numbers,
    # otherwise every task hash would mint its own pattern and dedupe fails.
    assert "#101" not in learning["applied_to"]
    assert "#102" not in learning["applied_to"]


@pytest.mark.asyncio
async def test_dreamer_skips_under_threshold() -> None:
    """Two occurrences with min_pattern_count=3 → no rows written."""
    now_ts = time.time()
    rows = [
        _buzz(
            action="VERIFIER_TIER2_REOPENED",
            worker="alpha",
            detail="missed criterion X",
            timestamp=now_ts - 100,
        ),
        _buzz(
            action="VERIFIER_TIER2_REOPENED",
            worker="beta",
            detail="missed criterion X",
            timestamp=now_ts - 200,
        ),
    ]
    d, store, _ = _dreamer(rows=rows, min_pattern_count=3)

    written = await d.sweep(now=1000.0)

    assert written == 0
    assert store.added == []


@pytest.mark.asyncio
async def test_dreamer_requires_distinct_workers() -> None:
    """Five hits all from one worker → no learning (single-worker chatter)."""
    now_ts = time.time()
    rows = [
        _buzz(
            action="OVERSIGHT_INTERVENTION",
            worker="alpha",
            detail="redirected away from refactor",
            timestamp=now_ts - 100 * i,
        )
        for i in range(1, 6)
    ]
    d, store, _ = _dreamer(rows=rows)

    written = await d.sweep(now=1000.0)

    assert written == 0
    assert store.added == []


@pytest.mark.asyncio
async def test_dreamer_lookback_window() -> None:
    """Entries older than ``dreamer_lookback_hours`` are excluded."""
    now_ts = time.time()
    # Three matching events but all older than the 24-hour lookback
    old = now_ts - (25 * 3600)
    rows = [
        _buzz(
            action="VERIFIER_TIER2_REOPENED",
            worker="alpha",
            detail="missed criterion X",
            timestamp=old,
        ),
        _buzz(
            action="VERIFIER_TIER2_REOPENED",
            worker="beta",
            detail="missed criterion X",
            timestamp=old - 60,
        ),
        _buzz(
            action="VERIFIER_TIER2_REOPENED",
            worker="alpha",
            detail="missed criterion X",
            timestamp=old - 120,
        ),
    ]
    d, store, _ = _dreamer(rows=rows, lookback_hours=24.0)

    written = await d.sweep(now=1000.0)

    assert written == 0
    assert store.added == []


# ---------------------------------------------------------------------------
# Dedupe / refresh
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dreamer_dedupes_existing_recent_pattern() -> None:
    """A fresh dreamer learning for the same key suppresses re-writing."""
    now_ts = time.time()
    rows = [
        _buzz(
            action="VERIFIER_TIER2_REOPENED",
            worker="alpha",
            detail="missed criterion X",
            timestamp=now_ts - 100 * i,
        )
        for i in range(1, 4)
    ] + [
        _buzz(
            action="VERIFIER_TIER2_REOPENED",
            worker="beta",
            detail="missed criterion X",
            timestamp=now_ts - 50,
        ),
    ]
    learnings = _LearningsStub()
    d, _, _ = _dreamer(rows=rows, learnings=learnings)
    # Probe the key the dreamer would compute, then pre-seed it.
    key = d.signature_key("VERIFIER_TIER2_REOPENED", "missed criterion X")
    learnings.seed(
        applied_to=f"discovered_by_dreamer:VERIFIER_TIER2_REOPENED:{key}",
        age_seconds=60.0,
    )

    written = await d.sweep(now=1000.0)

    assert written == 0
    assert learnings.added == []


@pytest.mark.asyncio
async def test_dreamer_refreshes_stale_pattern() -> None:
    """A dreamer learning older than 7 days is refreshed (new row written)."""
    now_ts = time.time()
    rows = [
        _buzz(
            action="VERIFIER_TIER2_REOPENED",
            worker="alpha",
            detail="missed criterion X",
            timestamp=now_ts - 100 * i,
        )
        for i in range(1, 4)
    ] + [
        _buzz(
            action="VERIFIER_TIER2_REOPENED",
            worker="beta",
            detail="missed criterion X",
            timestamp=now_ts - 50,
        ),
    ]
    learnings = _LearningsStub()
    d, _, _ = _dreamer(rows=rows, learnings=learnings)
    key = d.signature_key("VERIFIER_TIER2_REOPENED", "missed criterion X")
    learnings.seed(
        applied_to=f"discovered_by_dreamer:VERIFIER_TIER2_REOPENED:{key}",
        age_seconds=8 * 86400.0,
    )

    written = await d.sweep(now=1000.0)

    assert written == 1
    assert len(learnings.added) == 1


# ---------------------------------------------------------------------------
# Buzz log emission
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dreamer_logs_pattern_discovered_to_buzz() -> None:
    """A successful sweep logs exactly one PATTERN_DISCOVERED entry."""
    now_ts = time.time()
    rows = [
        _buzz(
            action="VERIFIER_TIER2_REOPENED",
            worker=name,
            detail="missed criterion X",
            timestamp=now_ts - 100 * i,
        )
        for i, name in enumerate(["alpha", "beta", "alpha"], start=1)
    ]
    d, _, drone_log = _dreamer(rows=rows)

    written = await d.sweep(now=1000.0)

    assert written == 1
    pattern_calls = [
        c
        for c in drone_log.add.call_args_list
        if c.args and c.args[0] == SystemAction.PATTERN_DISCOVERED
    ]
    assert len(pattern_calls) == 1
    # Categorized under DRONE alongside the existing AUTO_NUDGE family
    kwargs = pattern_calls[0].kwargs
    assert kwargs.get("category") == LogCategory.DRONE


# ---------------------------------------------------------------------------
# Signature normalization
# ---------------------------------------------------------------------------


def test_signature_strips_task_numbers_and_timestamps() -> None:
    d, _, _ = _dreamer()
    a = d.signature_key("TASK_FAILED", "task #123 failed at 2026-05-08T10:23:45 — bad commit")
    b = d.signature_key("TASK_FAILED", "task #999 failed at 2026-05-08T11:55:01 — bad commit")
    assert a == b


def test_signature_distinguishes_different_actions() -> None:
    d, _, _ = _dreamer()
    a = d.signature_key("TASK_FAILED", "missed criterion X")
    b = d.signature_key("VERIFIER_TIER2_REOPENED", "missed criterion X")
    assert a != b
