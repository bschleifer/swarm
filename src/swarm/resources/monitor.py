"""System resource monitoring via /proc — no external dependencies.

Task #352 (2026-05-08) extended this module with three new pressure
signals that better track *actual* worker performance than standing
percentages do:

* **PSI** (``/proc/pressure/{cpu,memory,io}``) — the kernel reporting
  what fraction of the last 10s processes were stalled on each
  resource. ``psi_mem_avg10`` is the canonical "are we hurting now"
  signal and overrides the percentage heuristics in
  :func:`classify_pressure`.
* **Swap I/O rate** (``pswpin`` / ``pswpout`` from ``/proc/vmstat``) —
  pages-per-second going in / out of swap. Only swap *traffic* matters;
  standing swap is normal Linux cold-page behaviour.
* **Top workers by RSS** — when pressure is non-NOMINAL, surface the
  heaviest few worker process trees so the operator has a target
  instead of just an alert. Skipped under NOMINAL to keep the
  per-tick cost trivial.

All three are additive on the existing :class:`ResourceSnapshot` —
:meth:`to_dict` continues to emit ``mem_percent`` / ``swap_percent`` /
``pressure_level`` for any consumer that snapshotted the old shape.
"""

from __future__ import annotations

import collections
import os
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class MemoryPressureLevel(Enum):
    """Graduated memory pressure levels."""

    NOMINAL = "nominal"
    ELEVATED = "elevated"
    HIGH = "high"
    CRITICAL = "critical"


# Module-level path constants — overridden by tests via monkeypatch so
# /proc reads can be redirected to fixture directories without touching
# the real filesystem.
_PROC_ROOT = "/proc"
_PSI_DIR = "/proc/pressure"
_VMSTAT_PATH = "/proc/vmstat"

# PSI override thresholds — kernel-reported memory stall percentages
# beyond which we force at least the corresponding pressure level.
# 10/30 picked to match the htop / btop convention. Tuned conservatively:
# below 10% the system is recovering even under spikes; above 30% the
# kernel is reporting that processes are essentially blocked on
# memory operations and worker dispatch should slow down.
_PSI_MEM_ELEVATED_THRESHOLD = 10.0
_PSI_MEM_HIGH_THRESHOLD = 30.0


@dataclass(frozen=True)
class ResourceSnapshot:
    """Point-in-time system resource snapshot."""

    timestamp: float
    mem_total_mb: float
    mem_available_mb: float
    mem_used_mb: float
    mem_percent: float
    swap_total_mb: float
    swap_used_mb: float
    swap_percent: float
    load_1m: float
    load_5m: float
    load_15m: float
    cpu_count: int
    pressure_level: MemoryPressureLevel
    dstate_pids: dict[int, str] = field(default_factory=dict)
    # Task #352: PSI + swap I/O + top-by-RSS. New fields are additive
    # with safe defaults so legacy construction sites continue to work.
    psi_available: bool = False
    psi_cpu_avg10: float = 0.0
    psi_mem_avg10: float = 0.0
    psi_io_avg10: float = 0.0
    swap_in_per_sec: float = 0.0
    swap_out_per_sec: float = 0.0
    top_workers_by_rss: list[tuple[str, int]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-safe dictionary.

        Legacy keys (``mem_percent``, ``swap_percent``, ``pressure_level``)
        are preserved for backwards compatibility — Phase 6 of the task
        #352 contract. New PSI / swap-I/O fields are additive.
        """
        return {
            "timestamp": self.timestamp,
            "mem_total_mb": round(self.mem_total_mb, 1),
            "mem_available_mb": round(self.mem_available_mb, 1),
            "mem_used_mb": round(self.mem_used_mb, 1),
            "mem_percent": round(self.mem_percent, 1),
            "swap_total_mb": round(self.swap_total_mb, 1),
            "swap_used_mb": round(self.swap_used_mb, 1),
            "swap_percent": round(self.swap_percent, 1),
            "load_1m": round(self.load_1m, 2),
            "load_5m": round(self.load_5m, 2),
            "load_15m": round(self.load_15m, 2),
            "cpu_count": self.cpu_count,
            "pressure_level": self.pressure_level.value,
            "dstate_pids": {str(k): v for k, v in self.dstate_pids.items()},
            # Task #352 fields:
            "psi_available": self.psi_available,
            "psi_cpu_avg10": round(self.psi_cpu_avg10, 2),
            "psi_mem_avg10": round(self.psi_mem_avg10, 2),
            "psi_io_avg10": round(self.psi_io_avg10, 2),
            "swap_in_per_sec": round(self.swap_in_per_sec, 2),
            "swap_out_per_sec": round(self.swap_out_per_sec, 2),
            # Tuples become lists in JSON. Each entry stays
            # [name, rss_kb] so the dashboard JS can index without a
            # dataclass shim.
            "top_workers_by_rss": [[name, rss] for name, rss in self.top_workers_by_rss],
        }


def parse_meminfo(path: str = "/proc/meminfo") -> dict[str, int]:
    """Parse /proc/meminfo and return values in kB."""
    result: dict[str, int] = {}
    try:
        with open(path) as f:
            for line in f:
                parts = line.split(":")
                if len(parts) != 2:
                    continue
                key = parts[0].strip()
                val_parts = parts[1].strip().split()
                if val_parts:
                    try:
                        result[key] = int(val_parts[0])
                    except ValueError:
                        continue
    except OSError:
        pass
    return result


def parse_loadavg(path: str = "/proc/loadavg") -> tuple[float, float, float]:
    """Parse /proc/loadavg and return (1min, 5min, 15min) load averages."""
    try:
        with open(path) as f:
            parts = f.read().strip().split()
            if len(parts) >= 3:
                return float(parts[0]), float(parts[1]), float(parts[2])
    except (OSError, ValueError, IndexError):
        pass
    return 0.0, 0.0, 0.0


def parse_psi_some_avg10(path: str) -> float | None:
    """Parse a ``/proc/pressure/<resource>`` file and return ``some avg10``.

    The PSI file format (kernel ≥ 4.20, ``CONFIG_PSI=y``) is two lines:

    .. code-block:: text

        some avg10=0.00 avg60=0.00 avg300=0.00 total=0
        full avg10=0.00 avg60=0.00 avg300=0.00 total=0

    We return the ``some.avg10`` value (% of last 10 s the system stalled
    on this resource for at least one task). Returns ``None`` when:

    * The file doesn't exist (``CONFIG_PSI=n`` kernels);
    * The file is empty / unreadable;
    * The ``some`` line is missing or malformed.

    Returning ``None`` lets callers distinguish "kernel says zero" from
    "kernel doesn't tell us" — both produce 0.0 in the snapshot but only
    the former should mark ``psi_available=True``.
    """
    try:
        with open(path) as f:
            text = f.read()
    except OSError:
        return None
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("some "):
            continue
        for token in line.split():
            if token.startswith("avg10="):
                try:
                    return float(token.split("=", 1)[1])
                except (IndexError, ValueError):
                    return None
    return None


def parse_vmstat_swap(path: str = "/proc/vmstat") -> tuple[int, int]:
    """Read ``pswpin`` / ``pswpout`` cumulative counters from /proc/vmstat.

    Each is a non-resetting count of pages swapped in / out since boot.
    Caller diffs successive readings via :func:`compute_swap_io_rate` to
    derive a per-second rate.
    """
    pswpin = 0
    pswpout = 0
    try:
        with open(path) as f:
            for raw in f:
                parts = raw.split()
                if len(parts) < 2:
                    continue
                key, value = parts[0], parts[1]
                if key == "pswpin":
                    try:
                        pswpin = int(value)
                    except ValueError:
                        pass
                elif key == "pswpout":
                    try:
                        pswpout = int(value)
                    except ValueError:
                        pass
    except OSError:
        pass
    return pswpin, pswpout


def compute_swap_io_rate(
    *,
    prev_in: int | None,
    prev_out: int | None,
    prev_ts: float | None,
    cur_in: int,
    cur_out: int,
    cur_ts: float,
) -> tuple[float, float]:
    """Per-second swap-I/O rate from two cumulative-counter readings.

    Returns ``(in_per_sec, out_per_sec)``. Edge cases:

    * Missing previous reading → ``(0.0, 0.0)`` (caller hasn't sampled
      twice yet — no baseline).
    * ``cur < prev`` (counter wrap or reboot) → ``(0.0, 0.0)`` instead
      of a negative rate. The next tick re-baselines.
    * Identical timestamps (``cur_ts <= prev_ts``) → ``(0.0, 0.0)`` to
      avoid a divide-by-zero or nonsensical rate.
    """
    if prev_in is None or prev_out is None or prev_ts is None:
        return 0.0, 0.0
    dt = cur_ts - prev_ts
    if dt <= 0:
        return 0.0, 0.0
    delta_in = cur_in - prev_in
    delta_out = cur_out - prev_out
    if delta_in < 0 or delta_out < 0:
        return 0.0, 0.0
    return (delta_in / dt, delta_out / dt)


def _read_proc_status_rss(pid: int) -> int:
    """Return ``VmRSS`` in kB from ``/proc/<pid>/status``, or 0 on error."""
    try:
        with open(f"{_PROC_ROOT}/{pid}/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    parts = line.split()
                    if len(parts) >= 2:
                        try:
                            return int(parts[1])
                        except ValueError:
                            return 0
    except OSError:
        pass
    return 0


def _build_parent_map() -> dict[int, list[int]] | None:
    """Walk ``/proc`` once and return a ``{ppid: [pid, ...]}`` map.

    Returns ``None`` if ``/proc`` itself is unreadable. Per-pid stat
    parse failures are silently skipped — the snapshot path tolerates
    partial maps so a single zombie or vanished process can't poison
    the whole walk.
    """
    parent_map: dict[int, list[int]] = {}
    try:
        entries = os.listdir(_PROC_ROOT)
    except OSError:
        return None
    for entry in entries:
        if not entry.isdigit():
            continue
        pid = int(entry)
        try:
            with open(f"{_PROC_ROOT}/{pid}/stat") as f:
                stat_line = f.read()
            close_paren = stat_line.rfind(")")
            if close_paren == -1:
                continue
            fields = stat_line[close_paren + 2 :].split()
            if len(fields) >= 2:
                ppid = int(fields[1])
                parent_map.setdefault(ppid, []).append(pid)
        except (OSError, ValueError, IndexError):
            continue
    return parent_map


def _walk_descendants(root_pid: int, parent_map: dict[int, list[int]]) -> set[int]:
    """Return ``{root_pid}`` plus all transitive children per ``parent_map``."""
    descendants: set[int] = {root_pid}
    queue: collections.deque[int] = collections.deque([root_pid])
    while queue:
        pid = queue.popleft()
        for child in parent_map.get(pid, []):
            if child not in descendants:
                descendants.add(child)
                queue.append(child)
    return descendants


def top_workers_by_rss(
    worker_names: dict[int, str],
    *,
    top_n: int = 5,
) -> list[tuple[str, int]]:
    """Return the ``top_n`` heaviest workers by total process-tree RSS.

    For each ``(pid, name)`` in ``worker_names``, walk the descendant
    tree from /proc and sum ``VmRSS`` (in MB) across the worker and its
    children. The result is sorted descending by RSS and clamped to
    ``top_n``. Returns ``[]`` when the input map is empty or
    ``/proc`` is unreadable — the dashboard hides the section in
    those cases.
    """
    if not worker_names:
        return []

    parent_map = _build_parent_map()
    if parent_map is None:
        return []

    totals: list[tuple[str, int]] = []
    for root_pid, name in worker_names.items():
        descendants = _walk_descendants(root_pid, parent_map)
        # Sum kB then convert to MB. Integer MB is enough for a "top
        # consumers" widget; sub-MB precision is noise.
        rss_kb = sum(_read_proc_status_rss(p) for p in descendants)
        totals.append((name, rss_kb // 1024))

    totals.sort(key=lambda item: item[1], reverse=True)
    return totals[:top_n]


def _get_descendants(root_pids: set[int]) -> set[int]:
    """Walk /proc to find all descendant PIDs of the given root PIDs."""
    parent_map: dict[int, list[int]] = {}
    try:
        for entry in os.listdir(_PROC_ROOT):
            if not entry.isdigit():
                continue
            pid = int(entry)
            try:
                with open(f"{_PROC_ROOT}/{pid}/stat") as f:
                    stat_line = f.read()
                close_paren = stat_line.rfind(")")
                if close_paren == -1:
                    continue
                fields = stat_line[close_paren + 2 :].split()
                if len(fields) >= 2:
                    ppid = int(fields[1])
                    parent_map.setdefault(ppid, []).append(pid)
            except (OSError, ValueError, IndexError):
                continue
    except OSError:
        return set()

    descendants: set[int] = set()
    queue = collections.deque(root_pids)
    while queue:
        pid = queue.popleft()
        for child in parent_map.get(pid, []):
            if child not in descendants:
                descendants.add(child)
                queue.append(child)
    return descendants


def find_dstate_descendants(root_pids: set[int]) -> dict[int, str]:
    """Find processes in D-state (uninterruptible sleep) among descendants.

    Performs a single-pass scan: builds the descendant set and checks for
    D-state simultaneously, avoiding a redundant second read of /proc/*/stat.

    Args:
        root_pids: PIDs of worker processes whose descendants to scan.

    Returns:
        Dict mapping PID -> comm for processes in D-state.
    """
    if not root_pids:
        return {}

    # Single-pass: read every /proc/*/stat once, collecting parent→child
    # relationships AND D-state info (comm + state char) in one pass.
    parent_map: dict[int, list[int]] = {}
    stat_cache: dict[int, tuple[str, str]] = {}  # pid → (comm, state_char)
    try:
        for entry in os.listdir(_PROC_ROOT):
            if not entry.isdigit():
                continue
            pid = int(entry)
            try:
                with open(f"{_PROC_ROOT}/{pid}/stat") as f:
                    stat_line = f.read()
                open_paren = stat_line.index("(")
                close_paren = stat_line.rindex(")")
                comm = stat_line[open_paren + 1 : close_paren]
                fields = stat_line[close_paren + 2 :].split()
                if len(fields) >= 2:
                    state_char = fields[0]
                    ppid = int(fields[1])
                    parent_map.setdefault(ppid, []).append(pid)
                    stat_cache[pid] = (comm, state_char)
            except (OSError, ValueError, IndexError):
                continue
    except OSError:
        return {}

    # Walk descendant tree
    descendants: set[int] = set(root_pids)
    queue = collections.deque(root_pids)
    while queue:
        pid = queue.popleft()
        for child in parent_map.get(pid, []):
            if child not in descendants:
                descendants.add(child)
                queue.append(child)

    # Filter to D-state from cache (no second /proc read)
    return {
        pid: comm
        for pid in descendants
        if (entry := stat_cache.get(pid)) is not None
        for comm, state in [entry]
        if state == "D"
    }


def classify_pressure(
    mem_pct: float,
    swap_pct: float,
    *,
    elevated_swap_pct: float = 40.0,
    elevated_mem_pct: float = 80.0,
    high_swap_pct: float = 70.0,
    high_mem_pct: float = 90.0,
    critical_swap_pct: float = 85.0,
    critical_mem_pct: float = 95.0,
    psi_mem_avg10: float = 0.0,
) -> MemoryPressureLevel:
    """Classify memory pressure from memory + swap percentages and PSI.

    Swap alone is NOT pressure — cold pages kept in swap are normal Linux
    behaviour even when memory is abundant.  Pressure escalates only when
    memory is also strained:

    - CRITICAL: memory alone above ``critical_mem_pct`` (95%), OR swap above
      ``critical_swap_pct`` (85%) while memory is also above ``high_mem_pct`` (90%).
    - HIGH: memory alone above ``high_mem_pct`` (90%), OR swap above
      ``high_swap_pct`` (70%) while memory is also above ``elevated_mem_pct`` (80%).
    - ELEVATED: either dimension above its elevated threshold (mem 80% / swap 40%).
      Informational only — no worker suspension.

    **PSI override** (task #352): when ``psi_mem_avg10`` is set, the
    kernel is reporting that processes actually stalled on memory for
    that fraction of the last 10 s. That trumps percentage heuristics:
    ``>= 10`` forces at least ELEVATED, ``>= 30`` forces at least HIGH.
    The override is a *floor* — it can only raise the level the percent
    logic computed, never demote. PSI=0 (the default, also returned when
    the kernel doesn't have CONFIG_PSI) has no effect.
    """
    if (swap_pct >= critical_swap_pct and mem_pct >= high_mem_pct) or mem_pct >= critical_mem_pct:
        base = MemoryPressureLevel.CRITICAL
    elif (swap_pct >= high_swap_pct and mem_pct >= elevated_mem_pct) or mem_pct >= high_mem_pct:
        base = MemoryPressureLevel.HIGH
    elif swap_pct >= elevated_swap_pct or mem_pct >= elevated_mem_pct:
        base = MemoryPressureLevel.ELEVATED
    else:
        base = MemoryPressureLevel.NOMINAL

    psi_floor = MemoryPressureLevel.NOMINAL
    if psi_mem_avg10 >= _PSI_MEM_HIGH_THRESHOLD:
        psi_floor = MemoryPressureLevel.HIGH
    elif psi_mem_avg10 >= _PSI_MEM_ELEVATED_THRESHOLD:
        psi_floor = MemoryPressureLevel.ELEVATED

    return _max_level(base, psi_floor)


_LEVEL_ORDER: dict[MemoryPressureLevel, int] = {
    MemoryPressureLevel.NOMINAL: 0,
    MemoryPressureLevel.ELEVATED: 1,
    MemoryPressureLevel.HIGH: 2,
    MemoryPressureLevel.CRITICAL: 3,
}


def _max_level(a: MemoryPressureLevel, b: MemoryPressureLevel) -> MemoryPressureLevel:
    """Return the more severe of two pressure levels."""
    return a if _LEVEL_ORDER[a] >= _LEVEL_ORDER[b] else b


def _read_psi_snapshot() -> tuple[bool, float, float, float]:
    """Read all three PSI files. Returns ``(available, cpu, mem, io)``.

    ``available`` is True when at least one of the files yields a
    parseable ``some avg10`` reading. CPU/mem/io default to 0.0 when
    their individual file is missing or malformed — same shape the
    ``CONFIG_PSI=n`` kernels produce, so callers don't have to branch
    on per-resource availability.
    """
    cpu = parse_psi_some_avg10(f"{_PSI_DIR}/cpu")
    mem = parse_psi_some_avg10(f"{_PSI_DIR}/memory")
    io = parse_psi_some_avg10(f"{_PSI_DIR}/io")
    available = any(v is not None for v in (cpu, mem, io))
    return available, cpu or 0.0, mem or 0.0, io or 0.0


def take_snapshot(
    worker_pids: set[int],
    *,
    enabled: bool = True,
    dstate_scan: bool = True,
    elevated_swap_pct: float = 40.0,
    elevated_mem_pct: float = 80.0,
    high_swap_pct: float = 70.0,
    high_mem_pct: float = 90.0,
    critical_swap_pct: float = 85.0,
    critical_mem_pct: float = 95.0,
    prev_swap_io: tuple[int, int, float] | None = None,
    worker_names: dict[int, str] | None = None,
    now_override: float | None = None,
) -> ResourceSnapshot:
    """Collect a full system resource snapshot.

    All /proc reads are done synchronously — they are fast virtual-FS reads.

    Task #352 additions:

    * ``prev_swap_io`` — previous ``(pswpin, pswpout, timestamp)`` reading.
      ``ResourceMonitor`` (server side) holds this across ticks. None on
      the first call → swap-I/O rate is reported as 0.0.
    * ``worker_names`` — optional ``{pid: name}`` map. When provided AND
      pressure is non-NOMINAL, the snapshot includes ``top_workers_by_rss``.
      Skipped under NOMINAL to keep the per-tick cost trivial.
    * ``now_override`` — test seam so swap-I/O rate calculations don't
      depend on wall time. Defaults to ``time.time()``.
    """
    meminfo = parse_meminfo()
    load_1m, load_5m, load_15m = parse_loadavg()

    mem_total_kb = meminfo.get("MemTotal", 0)
    mem_available_kb = meminfo.get("MemAvailable", 0)
    swap_total_kb = meminfo.get("SwapTotal", 0)
    swap_free_kb = meminfo.get("SwapFree", 0)

    mem_total_mb = mem_total_kb / 1024
    mem_available_mb = mem_available_kb / 1024
    mem_used_mb = mem_total_mb - mem_available_mb
    mem_percent = (mem_used_mb / mem_total_mb * 100) if mem_total_mb > 0 else 0.0

    swap_used_kb = swap_total_kb - swap_free_kb
    swap_total_mb = swap_total_kb / 1024
    swap_used_mb = swap_used_kb / 1024
    swap_percent = (swap_used_kb / swap_total_kb * 100) if swap_total_kb > 0 else 0.0

    psi_available, psi_cpu, psi_mem, psi_io = _read_psi_snapshot()

    pressure_level = classify_pressure(
        mem_percent,
        swap_percent,
        elevated_swap_pct=elevated_swap_pct,
        elevated_mem_pct=elevated_mem_pct,
        high_swap_pct=high_swap_pct,
        high_mem_pct=high_mem_pct,
        critical_swap_pct=critical_swap_pct,
        critical_mem_pct=critical_mem_pct,
        psi_mem_avg10=psi_mem,
    )

    dstate_pids: dict[int, str] = {}
    if dstate_scan:
        dstate_pids = find_dstate_descendants(worker_pids)

    cur_in, cur_out = parse_vmstat_swap(_VMSTAT_PATH)
    now = now_override if now_override is not None else time.time()
    prev_in, prev_out, prev_ts = (
        (prev_swap_io[0], prev_swap_io[1], prev_swap_io[2])
        if prev_swap_io is not None
        else (None, None, None)
    )
    swap_in_rate, swap_out_rate = compute_swap_io_rate(
        prev_in=prev_in,
        prev_out=prev_out,
        prev_ts=prev_ts,
        cur_in=cur_in,
        cur_out=cur_out,
        cur_ts=now,
    )

    # Top-by-RSS is only worth computing when pressure is non-NOMINAL —
    # under NOMINAL the dashboard hides the section anyway, so the
    # /proc/<pid>/status walk would just be wasted work each tick.
    top: list[tuple[str, int]] = []
    if worker_names and pressure_level is not MemoryPressureLevel.NOMINAL:
        top = top_workers_by_rss(worker_names, top_n=5)

    try:
        cpu_count = os.cpu_count() or 1
    except Exception:
        cpu_count = 1

    return ResourceSnapshot(
        timestamp=now,
        mem_total_mb=mem_total_mb,
        mem_available_mb=mem_available_mb,
        mem_used_mb=mem_used_mb,
        mem_percent=mem_percent,
        swap_total_mb=swap_total_mb,
        swap_used_mb=swap_used_mb,
        swap_percent=swap_percent,
        load_1m=load_1m,
        load_5m=load_5m,
        load_15m=load_15m,
        cpu_count=cpu_count,
        pressure_level=pressure_level,
        dstate_pids=dstate_pids,
        psi_available=psi_available,
        psi_cpu_avg10=psi_cpu,
        psi_mem_avg10=psi_mem,
        psi_io_avg10=psi_io,
        swap_in_per_sec=swap_in_rate,
        swap_out_per_sec=swap_out_rate,
        top_workers_by_rss=top,
    )
