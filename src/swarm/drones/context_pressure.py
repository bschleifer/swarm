"""Context-pressure drone — auto-/compact workers approaching their context window.

Item 3 of the 10-repo research bundle (plan
``~/.claude/plans/sequential-churning-meerkat.md``).

Phase 0 (commit ``5fc159e``) confirmed the data source is already solid:
``worker.context_pct`` is populated every 15 s by
``SwarmDaemon._usage_refresh_loop`` from each worker's session JSONL,
provider-aware (1 M for Claude/Gemini, 200 k for Codex), with hysteresis
already present in ``_check_context_pressure``. What was missing: an
*action* layer that turns the pressure signal into a ``/compact``
injection. This drone is that layer.

Two tiers, each with state-aware behaviour so we don't disrupt
in-flight work needlessly:

* **Soft** (``warn ≤ pct < crit``): inject ``/compact`` only when the
  worker is RESTING/SLEEPING — the natural "safe moment". BUZZING and
  WAITING workers wait for either the worker to settle or the hard
  tier to fire.
* **Hard** (``pct ≥ crit``): act regardless of state, but match the
  state's invariants:

    - WAITING (approval prompt visible) → defer; the operator/Queen
      needs to resolve the prompt first. Logged as a deferral; next
      sweep retries.
    - BUZZING (in tool use) → send Ctrl-C, then ``/compact``. We're
      going to lose context anyway when CC's auto-compact kicks in;
      better to interrupt cleanly than land in the middle of a turn.
    - RESTING/SLEEPING → direct ``/compact`` injection.
    - STUNG → skip; the worker process is dead.

Hysteresis keeps the drone from spamming. Once a worker is fired at a
tier, it must drop below ``warn_threshold`` before being re-armed.
``soft → hard`` escalation is allowed: a worker that fires soft at 75 %
and keeps climbing past 90 % will fire hard too (worker may have
ignored the soft compact).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from swarm.drones.log import LogCategory, SystemAction
from swarm.logging import get_logger
from swarm.worker.worker import WorkerState

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from swarm.config.models import DroneConfig
    from swarm.drones.log import DroneLog
    from swarm.worker.worker import Worker


_log = get_logger("drones.context_pressure")

_COMPACT_PROMPT = "/compact"

# Tier severity ranking so escalations short-circuit cleanly.
# A worker fired at "soft" is still eligible for "hard"; a worker
# fired at "hard" is not eligible for any further action until
# pct drops back below ``warn_threshold``.
_TIER_RANK: dict[str, int] = {"normal": 0, "soft": 1, "hard": 2}


class ContextPressureWatcher:
    """Inject ``/compact`` into workers nearing their context window.

    Parameters
    ----------
    drone_config:
        Owns ``context_warning_threshold`` and ``context_critical_threshold``.
        Either or both being ``0`` disables the corresponding tier; both
        being ``0`` disables the watcher entirely.
    drone_log:
        Every fire / defer is appended under ``LogCategory.COMPACT``.
    send_to_worker:
        Async callable ``(name, message, *, _log_operator=False) -> None``
        used to inject the ``/compact`` prompt. Mirrors
        ``SwarmDaemon.send_to_worker``.
    interrupt_worker:
        Async callable ``(name) -> None`` used to send Ctrl-C before the
        compact prompt when the worker is BUZZING. Mirrors
        ``SwarmDaemon.interrupt_worker``.
    """

    def __init__(
        self,
        *,
        drone_config: DroneConfig,
        drone_log: DroneLog,
        send_to_worker: Callable[..., Awaitable[None]],
        interrupt_worker: Callable[[str], Awaitable[None]],
    ) -> None:
        self._config = drone_config
        self._drone_log = drone_log
        self._send_to_worker = send_to_worker
        self._interrupt_worker = interrupt_worker
        # Worker-name → most-recent fired tier ("soft" or "hard").
        # Hysteresis gate: skip workers whose fired tier rank is at
        # or above the current tier rank.
        self._fired: dict[str, str] = {}

    @property
    def warn_threshold(self) -> float:
        return float(self._config.context_warning_threshold or 0.0)

    @property
    def crit_threshold(self) -> float:
        return float(self._config.context_critical_threshold or 0.0)

    @property
    def enabled(self) -> bool:
        return self.warn_threshold > 0 or self.crit_threshold > 0

    async def sweep(self, workers: list[Worker]) -> int:
        """Run one tier-evaluation pass. Returns the number of fires this tick.

        ``fires`` includes hard interrupts and direct injections. Soft
        deferrals (worker not idle) and hard deferrals (worker WAITING)
        are NOT counted as fires — they don't change worker state.
        """
        if not self.enabled:
            return 0
        fires = 0
        for w in workers:
            pct = float(getattr(w, "context_pct", 0.0))
            self._maybe_rearm(w.name, pct)
            tier = self._tier_for(pct)
            if tier == "normal":
                continue
            prev = self._fired.get(w.name, "normal")
            if _TIER_RANK[prev] >= _TIER_RANK[tier]:
                # Already fired at this tier (or higher); skip until
                # the worker drops back below warn_threshold.
                continue
            if await self._fire(w, tier, pct):
                fires += 1
        return fires

    def _tier_for(self, pct: float) -> str:
        if self.crit_threshold > 0 and pct >= self.crit_threshold:
            return "hard"
        if self.warn_threshold > 0 and pct >= self.warn_threshold:
            return "soft"
        return "normal"

    def _maybe_rearm(self, worker_name: str, pct: float) -> None:
        """Clear ``_fired`` state when the worker drops below warn_threshold.

        Called every sweep so a successful compact (which drops pct
        sharply) re-arms the watcher without manual intervention.
        """
        if worker_name not in self._fired:
            return
        if self.warn_threshold > 0 and pct < self.warn_threshold:
            self._fired.pop(worker_name, None)

    async def _fire(self, worker: Worker, tier: str, pct: float) -> bool:
        state = self._worker_state(worker)
        if state == WorkerState.STUNG:
            # Dead worker — no PTY to inject into. Don't mark fired,
            # don't log (state_tracker handles STUNG transitions).
            return False
        if tier == "soft":
            return await self._fire_soft(worker, pct, state)
        return await self._fire_hard(worker, pct, state)

    @staticmethod
    def _worker_state(worker: Worker) -> WorkerState | None:
        """Best-effort state lookup. Tests sometimes only set ``state``."""
        return getattr(worker, "display_state", None) or getattr(worker, "state", None)

    async def _fire_soft(
        self,
        worker: Worker,
        pct: float,
        state: WorkerState | None,
    ) -> bool:
        """Soft tier: inject ``/compact`` only when worker is at rest.

        BUZZING / WAITING workers don't fire soft — we don't disturb
        in-flight work for a soft trigger. They'll either settle and
        fire next sweep, or escalate to hard.
        """
        if state not in (WorkerState.RESTING, WorkerState.SLEEPING):
            return False
        try:
            await self._send_to_worker(worker.name, _COMPACT_PROMPT, _log_operator=False)
        except Exception:
            _log.warning("context_pressure: soft inject failed for %s", worker.name, exc_info=True)
            return False
        self._fired[worker.name] = "soft"
        self._drone_log.add(
            SystemAction.CONTEXT_COMPACT_INJECTED,
            worker.name,
            f"soft tier ({int(pct * 100)}%) — {state.value} → /compact",
            category=LogCategory.COMPACT,
            metadata={"tier": "soft", "pct": pct, "state": state.value},
        )
        return True

    async def _fire_hard(
        self,
        worker: Worker,
        pct: float,
        state: WorkerState | None,
    ) -> bool:
        """Hard tier: act now. State decides the resolution path."""
        if state == WorkerState.WAITING:
            # Approval prompt visible — operator/Queen owns this turn.
            # Log the deferral; next sweep retries. Don't mark fired
            # so the retry isn't gated by hysteresis.
            self._drone_log.add(
                SystemAction.CONTEXT_COMPACT_DEFERRED,
                worker.name,
                f"hard tier ({int(pct * 100)}%) — WAITING; will retry next sweep",
                category=LogCategory.COMPACT,
                metadata={"tier": "hard", "pct": pct, "state": state.value},
            )
            return False
        if state == WorkerState.BUZZING:
            # Mid-turn — interrupt cleanly before compact lands.
            try:
                await self._interrupt_worker(worker.name)
            except Exception:
                _log.warning(
                    "context_pressure: interrupt failed for %s",
                    worker.name,
                    exc_info=True,
                )
                return False
            try:
                await self._send_to_worker(worker.name, _COMPACT_PROMPT, _log_operator=False)
            except Exception:
                _log.warning(
                    "context_pressure: hard inject after interrupt failed for %s",
                    worker.name,
                    exc_info=True,
                )
                return False
            self._fired[worker.name] = "hard"
            self._drone_log.add(
                SystemAction.CONTEXT_COMPACT_INTERRUPTED,
                worker.name,
                f"hard tier ({int(pct * 100)}%) — BUZZING interrupted → /compact",
                category=LogCategory.COMPACT,
                metadata={"tier": "hard", "pct": pct, "state": state.value},
            )
            return True
        # RESTING / SLEEPING (or any non-WAITING/BUZZING/STUNG) — direct inject.
        try:
            await self._send_to_worker(worker.name, _COMPACT_PROMPT, _log_operator=False)
        except Exception:
            _log.warning("context_pressure: hard inject failed for %s", worker.name, exc_info=True)
            return False
        self._fired[worker.name] = "hard"
        state_label = state.value if state is not None else "unknown"
        self._drone_log.add(
            SystemAction.CONTEXT_COMPACT_INJECTED,
            worker.name,
            f"hard tier ({int(pct * 100)}%) — {state_label} → /compact",
            category=LogCategory.COMPACT,
            metadata={"tier": "hard", "pct": pct, "state": state_label},
        )
        return True
