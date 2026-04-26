"""Tests for the context-pressure drone (item 3 of the 10-repo bundle).

The drone observes ``worker.context_pct`` (already populated every 15 s
by ``SwarmDaemon._usage_refresh_loop`` from session JSONL — see
``docs/audit/context-usage-data-source.md``) and injects ``/compact``
into workers approaching their context window. Two tiers, four
state-aware paths:

* Soft (warn ≤ pct < crit): inject only when RESTING/SLEEPING.
* Hard (pct ≥ crit):
    - WAITING → defer
    - BUZZING → Ctrl-C, then /compact
    - RESTING/SLEEPING → direct /compact
    - STUNG → skip
"""

from __future__ import annotations

import pytest

from swarm.config import DroneConfig
from swarm.drones.context_pressure import ContextPressureWatcher
from swarm.drones.log import DroneLog, LogCategory, SystemAction
from swarm.worker.worker import WorkerState

# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class _FakeWorker:
    """Minimal worker stand-in — only the attributes the drone touches."""

    def __init__(
        self,
        name: str,
        *,
        context_pct: float,
        state: WorkerState = WorkerState.RESTING,
    ) -> None:
        self.name = name
        self.context_pct = context_pct
        self.display_state = state
        self.state = state


class _Recorder:
    """Capture send_to_worker / interrupt_worker calls for assertions."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []
        self.interrupted: list[str] = []

    async def send(self, name: str, message: str, **kwargs: object) -> None:
        self.sent.append((name, message))

    async def interrupt(self, name: str) -> None:
        self.interrupted.append(name)


class _RaisingRecorder(_Recorder):
    """Send / interrupt that raise — used for failure-path tests."""

    async def send(self, name: str, message: str, **kwargs: object) -> None:
        raise RuntimeError("send blew up")

    async def interrupt(self, name: str) -> None:
        raise RuntimeError("interrupt blew up")


def _make_watcher(
    *,
    warn: float = 0.7,
    crit: float = 0.9,
    rec: _Recorder | None = None,
) -> tuple[ContextPressureWatcher, _Recorder, DroneLog]:
    rec = rec or _Recorder()
    cfg = DroneConfig(
        context_warning_threshold=warn,
        context_critical_threshold=crit,
    )
    log = DroneLog()
    watcher = ContextPressureWatcher(
        drone_config=cfg,
        drone_log=log,
        send_to_worker=rec.send,
        interrupt_worker=rec.interrupt,
    )
    return watcher, rec, log


# ---------------------------------------------------------------------------
# Enabled / disabled
# ---------------------------------------------------------------------------


def test_disabled_when_both_thresholds_zero():
    watcher, _, _ = _make_watcher(warn=0.0, crit=0.0)
    assert watcher.enabled is False


def test_enabled_with_warn_only():
    watcher, _, _ = _make_watcher(warn=0.7, crit=0.0)
    assert watcher.enabled is True


def test_enabled_with_crit_only():
    watcher, _, _ = _make_watcher(warn=0.0, crit=0.9)
    assert watcher.enabled is True


@pytest.mark.asyncio
async def test_disabled_sweep_returns_zero_and_does_nothing():
    watcher, rec, log = _make_watcher(warn=0.0, crit=0.0)
    fires = await watcher.sweep([_FakeWorker("a", context_pct=0.95)])
    assert fires == 0
    assert rec.sent == []
    assert log.entries == []


# ---------------------------------------------------------------------------
# Soft tier behaviour
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_soft_tier_resting_injects_compact():
    """75% RESTING worker fires soft: /compact lands in PTY."""
    watcher, rec, log = _make_watcher()
    w = _FakeWorker("api", context_pct=0.75, state=WorkerState.RESTING)
    fires = await watcher.sweep([w])
    assert fires == 1
    assert rec.sent == [("api", "/compact")]
    assert rec.interrupted == []
    # Buzz log under LogCategory.COMPACT with INJECTED action
    assert len(log.entries) == 1
    entry = log.entries[0]
    assert entry.action == SystemAction.CONTEXT_COMPACT_INJECTED
    assert entry.category == LogCategory.COMPACT
    assert "soft tier" in entry.detail
    assert entry.metadata["tier"] == "soft"
    assert entry.metadata["pct"] == pytest.approx(0.75)


@pytest.mark.asyncio
async def test_soft_tier_sleeping_injects_compact():
    """SLEEPING is treated as RESTING for soft tier (idle is idle)."""
    watcher, rec, _ = _make_watcher()
    w = _FakeWorker("api", context_pct=0.75, state=WorkerState.SLEEPING)
    fires = await watcher.sweep([w])
    assert fires == 1
    assert rec.sent == [("api", "/compact")]


@pytest.mark.asyncio
async def test_soft_tier_buzzing_does_not_fire():
    """75% BUZZING worker: don't disturb in-flight work for soft tier."""
    watcher, rec, log = _make_watcher()
    w = _FakeWorker("api", context_pct=0.75, state=WorkerState.BUZZING)
    fires = await watcher.sweep([w])
    assert fires == 0
    assert rec.sent == []
    assert log.entries == []


@pytest.mark.asyncio
async def test_soft_tier_waiting_does_not_fire():
    """75% WAITING worker: operator owns the prompt; soft skips."""
    watcher, rec, _ = _make_watcher()
    w = _FakeWorker("api", context_pct=0.75, state=WorkerState.WAITING)
    fires = await watcher.sweep([w])
    assert fires == 0
    assert rec.sent == []


@pytest.mark.asyncio
async def test_soft_then_idle_fires_on_next_sweep():
    """BUZZING → still pressured → settles to RESTING → soft fires next tick."""
    watcher, rec, _ = _make_watcher()
    w = _FakeWorker("api", context_pct=0.75, state=WorkerState.BUZZING)
    assert await watcher.sweep([w]) == 0
    # Worker settles
    w.display_state = WorkerState.RESTING
    w.state = WorkerState.RESTING
    assert await watcher.sweep([w]) == 1
    assert rec.sent == [("api", "/compact")]


# ---------------------------------------------------------------------------
# Hard tier behaviour — by state
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hard_tier_resting_direct_inject():
    watcher, rec, log = _make_watcher()
    w = _FakeWorker("api", context_pct=0.92, state=WorkerState.RESTING)
    fires = await watcher.sweep([w])
    assert fires == 1
    assert rec.sent == [("api", "/compact")]
    assert rec.interrupted == []
    assert log.entries[0].action == SystemAction.CONTEXT_COMPACT_INJECTED
    assert log.entries[0].metadata["tier"] == "hard"


@pytest.mark.asyncio
async def test_hard_tier_buzzing_interrupts_then_compacts():
    watcher, rec, log = _make_watcher()
    w = _FakeWorker("api", context_pct=0.92, state=WorkerState.BUZZING)
    fires = await watcher.sweep([w])
    assert fires == 1
    # Order matters: Ctrl-C BEFORE /compact
    assert rec.interrupted == ["api"]
    assert rec.sent == [("api", "/compact")]
    assert log.entries[0].action == SystemAction.CONTEXT_COMPACT_INTERRUPTED
    assert "BUZZING interrupted" in log.entries[0].detail


@pytest.mark.asyncio
async def test_hard_tier_waiting_defers_no_inject():
    """WAITING means an approval prompt is up — don't interrupt the operator."""
    watcher, rec, log = _make_watcher()
    w = _FakeWorker("api", context_pct=0.92, state=WorkerState.WAITING)
    fires = await watcher.sweep([w])
    # Deferral does NOT count as a fire (worker state unchanged)
    assert fires == 0
    assert rec.sent == []
    assert rec.interrupted == []
    assert log.entries[0].action == SystemAction.CONTEXT_COMPACT_DEFERRED
    assert "WAITING" in log.entries[0].detail


@pytest.mark.asyncio
async def test_hard_tier_waiting_retries_when_state_clears():
    """Deferred WAITING → state flips to RESTING → next sweep fires."""
    watcher, rec, _ = _make_watcher()
    w = _FakeWorker("api", context_pct=0.92, state=WorkerState.WAITING)
    # First sweep: deferred
    assert await watcher.sweep([w]) == 0
    assert rec.sent == []
    # Operator resolves the prompt; worker is now RESTING
    w.display_state = WorkerState.RESTING
    w.state = WorkerState.RESTING
    # Next sweep fires
    assert await watcher.sweep([w]) == 1
    assert rec.sent == [("api", "/compact")]


@pytest.mark.asyncio
async def test_hard_tier_stung_skipped():
    """STUNG: process is dead, no PTY to inject into."""
    watcher, rec, log = _make_watcher()
    w = _FakeWorker("api", context_pct=0.95, state=WorkerState.STUNG)
    fires = await watcher.sweep([w])
    assert fires == 0
    assert rec.sent == []
    assert log.entries == []


# ---------------------------------------------------------------------------
# Hysteresis
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hysteresis_soft_fires_only_once_at_same_tier():
    """Two consecutive sweeps at the same soft pct → single inject."""
    watcher, rec, _ = _make_watcher()
    w = _FakeWorker("api", context_pct=0.75, state=WorkerState.RESTING)
    assert await watcher.sweep([w]) == 1
    assert await watcher.sweep([w]) == 0
    assert rec.sent == [("api", "/compact")]


@pytest.mark.asyncio
async def test_hysteresis_rearm_when_pct_drops_below_warn():
    """Compact succeeds → pct drops below warn → re-armed for next approach."""
    watcher, rec, _ = _make_watcher()
    w = _FakeWorker("api", context_pct=0.75, state=WorkerState.RESTING)
    await watcher.sweep([w])
    # Worker compacted, pct drops below warn threshold
    w.context_pct = 0.40
    await watcher.sweep([w])
    # Pressure climbs again
    w.context_pct = 0.75
    fires = await watcher.sweep([w])
    assert fires == 1
    assert len(rec.sent) == 2


@pytest.mark.asyncio
async def test_soft_to_hard_escalation_allowed():
    """Worker fires soft, ignores compact, climbs to hard → hard fires too."""
    watcher, rec, log = _make_watcher()
    w = _FakeWorker("api", context_pct=0.75, state=WorkerState.RESTING)
    assert await watcher.sweep([w]) == 1  # soft fires
    # Worker didn't compact — pct climbs further
    w.context_pct = 0.92
    assert await watcher.sweep([w]) == 1  # hard fires too
    assert len(rec.sent) == 2  # both compact attempts
    assert log.entries[0].metadata["tier"] == "soft"
    assert log.entries[1].metadata["tier"] == "hard"


@pytest.mark.asyncio
async def test_hard_then_climbing_does_not_re_fire():
    """Already fired hard, pct still high → no re-fire (until rearm)."""
    watcher, rec, _ = _make_watcher()
    w = _FakeWorker("api", context_pct=0.92, state=WorkerState.RESTING)
    assert await watcher.sweep([w]) == 1
    w.context_pct = 0.95
    assert await watcher.sweep([w]) == 0
    assert len(rec.sent) == 1


# ---------------------------------------------------------------------------
# Threshold edges
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_below_warn_threshold_no_action():
    watcher, rec, log = _make_watcher()
    w = _FakeWorker("api", context_pct=0.69, state=WorkerState.RESTING)
    fires = await watcher.sweep([w])
    assert fires == 0
    assert rec.sent == []
    assert log.entries == []


@pytest.mark.asyncio
async def test_exactly_at_warn_threshold_fires_soft():
    watcher, rec, _ = _make_watcher(warn=0.7)
    w = _FakeWorker("api", context_pct=0.7, state=WorkerState.RESTING)
    assert await watcher.sweep([w]) == 1
    assert rec.sent == [("api", "/compact")]


@pytest.mark.asyncio
async def test_exactly_at_crit_threshold_fires_hard():
    watcher, rec, log = _make_watcher(crit=0.9)
    w = _FakeWorker("api", context_pct=0.9, state=WorkerState.RESTING)
    assert await watcher.sweep([w]) == 1
    assert log.entries[0].metadata["tier"] == "hard"


# ---------------------------------------------------------------------------
# Failure isolation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_failure_does_not_mark_fired():
    """If send_to_worker raises, don't gate retries on a stale fired flag."""
    rec = _RaisingRecorder()
    watcher, _, _ = _make_watcher(rec=rec)
    w = _FakeWorker("api", context_pct=0.75, state=WorkerState.RESTING)
    fires = await watcher.sweep([w])
    assert fires == 0
    # Replace recorder's send with a working one
    good = _Recorder()
    watcher._send_to_worker = good.send
    fires = await watcher.sweep([w])
    assert fires == 1
    assert good.sent == [("api", "/compact")]


@pytest.mark.asyncio
async def test_interrupt_failure_does_not_inject_compact():
    """Hard BUZZING + interrupt fails: don't push /compact (out-of-order)."""
    rec = _RaisingRecorder()
    watcher, _, _ = _make_watcher(rec=rec)
    w = _FakeWorker("api", context_pct=0.92, state=WorkerState.BUZZING)
    fires = await watcher.sweep([w])
    assert fires == 0
    # No partial state on the worker — hysteresis intact
    assert watcher._fired == {}


# ---------------------------------------------------------------------------
# Multi-worker isolation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_one_bad_worker_does_not_block_others():
    """A worker that defers (WAITING) doesn't prevent others from firing."""
    watcher, rec, _ = _make_watcher()
    workers = [
        _FakeWorker("a", context_pct=0.92, state=WorkerState.WAITING),  # defers
        _FakeWorker("b", context_pct=0.92, state=WorkerState.RESTING),  # fires
        _FakeWorker("c", context_pct=0.5, state=WorkerState.RESTING),  # below warn
    ]
    fires = await watcher.sweep(workers)
    assert fires == 1
    assert rec.sent == [("b", "/compact")]
