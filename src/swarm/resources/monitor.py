"""System resource monitoring via /proc — no external dependencies."""

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

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-safe dictionary."""
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


def _get_descendants(root_pids: set[int]) -> set[int]:
    """Walk /proc to find all descendant PIDs of the given root PIDs."""
    parent_map: dict[int, list[int]] = {}
    try:
        for entry in os.listdir("/proc"):
            if not entry.isdigit():
                continue
            pid = int(entry)
            try:
                with open(f"/proc/{pid}/stat") as f:
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
        for entry in os.listdir("/proc"):
            if not entry.isdigit():
                continue
            pid = int(entry)
            try:
                with open(f"/proc/{pid}/stat") as f:
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
    elevated_swap_pct: float = 25.0,
    elevated_mem_pct: float = 80.0,
    high_swap_pct: float = 50.0,
    high_mem_pct: float = 90.0,
    critical_swap_pct: float = 75.0,
    critical_mem_pct: float = 95.0,
) -> MemoryPressureLevel:
    """Classify memory pressure from memory and swap percentages.

    HIGH and CRITICAL require BOTH swap AND memory pressure (or memory alone
    above its threshold).  Swap alone with low memory is normal Linux behavior
    (cold pages kept in swap even when RAM is available) and should not trigger
    suspension.
    """
    if (swap_pct >= critical_swap_pct and mem_pct >= 70.0) or mem_pct >= critical_mem_pct:
        return MemoryPressureLevel.CRITICAL
    if (swap_pct >= high_swap_pct and mem_pct >= 60.0) or mem_pct >= high_mem_pct:
        return MemoryPressureLevel.HIGH
    if swap_pct >= elevated_swap_pct or mem_pct >= elevated_mem_pct:
        return MemoryPressureLevel.ELEVATED
    return MemoryPressureLevel.NOMINAL


def take_snapshot(
    worker_pids: set[int],
    *,
    enabled: bool = True,
    dstate_scan: bool = True,
    elevated_swap_pct: float = 25.0,
    elevated_mem_pct: float = 80.0,
    high_swap_pct: float = 50.0,
    high_mem_pct: float = 90.0,
    critical_swap_pct: float = 75.0,
    critical_mem_pct: float = 95.0,
) -> ResourceSnapshot:
    """Collect a full system resource snapshot.

    All /proc reads are done synchronously — they are fast virtual-FS reads.
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

    pressure_level = classify_pressure(
        mem_percent,
        swap_percent,
        elevated_swap_pct=elevated_swap_pct,
        elevated_mem_pct=elevated_mem_pct,
        high_swap_pct=high_swap_pct,
        high_mem_pct=high_mem_pct,
        critical_swap_pct=critical_swap_pct,
        critical_mem_pct=critical_mem_pct,
    )

    dstate_pids: dict[int, str] = {}
    if dstate_scan:
        dstate_pids = find_dstate_descendants(worker_pids)

    try:
        cpu_count = os.cpu_count() or 1
    except Exception:
        cpu_count = 1

    return ResourceSnapshot(
        timestamp=time.time(),
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
    )
