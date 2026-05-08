"""ResourceMonitor — system resource snapshot, pressure detection, D-state alerts."""

from __future__ import annotations

import asyncio
import collections
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from swarm.logging import get_logger
from swarm.resources.monitor import _VMSTAT_PATH, parse_vmstat_swap, take_snapshot

if TYPE_CHECKING:
    from swarm.config import ResourceConfig
    from swarm.drones.pilot import DronePilot
    from swarm.notify.bus import NotificationBus
    from swarm.pty.provider import WorkerProcessProvider
    from swarm.resources.monitor import ResourceSnapshot
    from swarm.worker.worker import Worker

_log = get_logger("server.resource_monitor")


_MAX_HISTORY = 288  # 24 hours at 5-min intervals


class ResourceMonitor:
    """Manages resource snapshots, pressure-level tracking, and D-state alerts."""

    def __init__(
        self,
        *,
        broadcast_ws: Callable[[dict[str, Any]], None],
        get_pilot: Callable[[], DronePilot | None],
        get_pool: Callable[[], WorkerProcessProvider | None],
        get_workers: Callable[[], list[Worker]],
        get_resource_config: Callable[[], ResourceConfig],
        notification_bus: Callable[[], NotificationBus],
    ) -> None:
        self._broadcast_ws = broadcast_ws
        self._get_pilot = get_pilot
        self._get_pool = get_pool
        self._get_workers = get_workers
        self._get_resource_config = get_resource_config
        self._get_notification_bus = notification_bus
        self._resource_snapshot: dict[str, object] | None = None
        self._prev_pressure_level: str = "nominal"
        self._history: collections.deque[dict[str, object]] = collections.deque(maxlen=_MAX_HISTORY)
        # Task #352: previous ``(pswpin, pswpout, timestamp)`` reading,
        # used by ``take_snapshot`` to compute per-second swap-I/O rates.
        # ``None`` until the first sample lands; takes one tick to
        # baseline.
        self._prev_swap_io: tuple[int, int, float] | None = None

    @property
    def snapshot(self) -> dict[str, object] | None:
        """Return the most recent resource snapshot dict, or None."""
        return self._resource_snapshot

    @property
    def history(self) -> list[dict[str, object]]:
        """Return historical snapshots (oldest first)."""
        return list(self._history)

    async def collect_worker_pids(self) -> set[int]:
        """Collect live worker PIDs from the pool."""
        pids: set[int] = set()
        pool = self._get_pool()
        if not pool:
            return pids
        try:
            workers_info = await pool.list_workers()
            for w in workers_info:
                if w.get("alive") and w.get("pid"):
                    pids.add(int(w["pid"]))
        except Exception:
            pass
        return pids

    async def collect_worker_names(self) -> dict[int, str]:
        """Collect ``{pid: name}`` for the live workers.

        Used by ``top_workers_by_rss`` (task #352) so the snapshot can
        attribute total RSS to a friendly worker name. Returns ``{}``
        when the pool is unwired or unreachable — the snapshot then
        leaves ``top_workers_by_rss`` empty.
        """
        names: dict[int, str] = {}
        pool = self._get_pool()
        if not pool:
            return names
        try:
            workers_info = await pool.list_workers()
            for w in workers_info:
                if w.get("alive") and w.get("pid") and w.get("name"):
                    names[int(w["pid"])] = str(w["name"])
        except Exception:
            pass
        return names

    def handle_snapshot(self, snap: ResourceSnapshot) -> None:
        """Process a resource snapshot: broadcast, check pressure, alert D-state."""
        snap_dict = snap.to_dict()
        pilot = self._get_pilot()
        snap_dict["suspended_for_pressure"] = pilot.pressure_suspended_workers if pilot else []
        rc = self._get_resource_config()
        snap_dict["thresholds"] = {
            "elevated_mem_pct": rc.elevated_mem_pct,
            "elevated_swap_pct": rc.elevated_swap_pct,
            "high_mem_pct": rc.high_mem_pct,
            "high_swap_pct": rc.high_swap_pct,
            "critical_mem_pct": rc.critical_mem_pct,
            "critical_swap_pct": rc.critical_swap_pct,
        }
        self._resource_snapshot = snap_dict
        self._history.append(
            {
                "timestamp": snap_dict.get("timestamp"),
                "mem_percent": snap_dict.get("mem_percent"),
                "swap_percent": snap_dict.get("swap_percent"),
                "load_1m": snap_dict.get("load_1m"),
                "pressure_level": snap_dict.get("pressure_level"),
            }
        )
        self._broadcast_ws({"type": "resources", **snap_dict})

        # Pressure level change
        level = snap.pressure_level.value
        if level != self._prev_pressure_level:
            _log.info(
                "resource pressure changed: %s -> %s (mem=%.0f%% swap=%.0f%%)",
                self._prev_pressure_level,
                level,
                snap.mem_percent,
                snap.swap_percent,
            )
            self._prev_pressure_level = level
            if pilot:
                pilot.on_pressure_changed(
                    snap.pressure_level,
                    mem_pct=snap.mem_percent,
                    swap_pct=snap.swap_percent,
                )
            notification_bus = self._get_notification_bus()
            if level in ("high", "critical"):
                notification_bus.emit_resource_pressure(level, snap.mem_percent, snap.swap_percent)
        elif level in ("high", "critical") and pilot:
            # Re-evaluate on every tick while pressure stays high
            pilot.on_pressure_changed(
                snap.pressure_level,
                mem_pct=snap.mem_percent,
                swap_pct=snap.swap_percent,
            )

        # D-state alerts
        if snap.dstate_pids:
            self._broadcast_ws(
                {
                    "type": "dstate_alert",
                    "pids": {str(k): v for k, v in snap.dstate_pids.items()},
                }
            )
            workers = self._get_workers()
            pid_to_worker = {w.pid: w.name for w in workers if hasattr(w, "pid")}
            notification_bus = self._get_notification_bus()
            for pid, comm in snap.dstate_pids.items():
                owner = pid_to_worker.get(pid, "unknown")
                notification_bus.emit_dstate_detected(pid, comm, owner)

    async def monitor_loop(self) -> None:
        """Periodically snapshot system resources and broadcast to WS clients."""
        try:
            while True:
                rc = self._get_resource_config()
                await asyncio.sleep(rc.poll_interval)
                try:
                    worker_pids = await self.collect_worker_pids()
                    worker_names = await self.collect_worker_names()
                    snap = await asyncio.to_thread(
                        take_snapshot,
                        worker_pids,
                        dstate_scan=rc.dstate_scan,
                        elevated_swap_pct=rc.elevated_swap_pct,
                        elevated_mem_pct=rc.elevated_mem_pct,
                        high_swap_pct=rc.high_swap_pct,
                        high_mem_pct=rc.high_mem_pct,
                        critical_swap_pct=rc.critical_swap_pct,
                        critical_mem_pct=rc.critical_mem_pct,
                        prev_swap_io=self._prev_swap_io,
                        worker_names=worker_names,
                    )
                    self.handle_snapshot(snap)
                    # Task #352: stash the cumulative-counter snapshot
                    # for the next tick so swap-I/O delta math has a
                    # baseline. We re-read /proc/vmstat directly here
                    # rather than threading the absolute counters
                    # through ``ResourceSnapshot`` — they're state-only,
                    # not part of the API surface.
                    cur_in, cur_out = parse_vmstat_swap(_VMSTAT_PATH)
                    self._prev_swap_io = (cur_in, cur_out, snap.timestamp)
                except Exception:
                    _log.debug("resource monitor tick failed", exc_info=True)
        except asyncio.CancelledError:
            return
