"""Tests for the /proc resource monitor — uses monkeypatched file reads."""

from __future__ import annotations

import textwrap

import pytest

from swarm.resources.monitor import (
    MemoryPressureLevel,
    ResourceSnapshot,
    classify_pressure,
    find_dstate_descendants,
    parse_loadavg,
    parse_meminfo,
    take_snapshot,
)

# ---------------------------------------------------------------------------
# Fixtures: fake /proc content
# ---------------------------------------------------------------------------

FAKE_MEMINFO = textwrap.dedent("""\
    MemTotal:       16384000 kB
    MemFree:         2048000 kB
    MemAvailable:    4096000 kB
    Buffers:          512000 kB
    Cached:          2048000 kB
    SwapCached:        10000 kB
    Active:          8000000 kB
    Inactive:        4000000 kB
    SwapTotal:       8192000 kB
    SwapFree:        6144000 kB
    Dirty:              1024 kB
    Writeback:             0 kB
    AnonPages:       6000000 kB
    Mapped:          1000000 kB
    Shmem:            500000 kB
""")

FAKE_MEMINFO_NO_SWAP = textwrap.dedent("""\
    MemTotal:       16384000 kB
    MemFree:         8000000 kB
    MemAvailable:   10000000 kB
    SwapTotal:             0 kB
    SwapFree:              0 kB
""")

FAKE_MEMINFO_CRITICAL = textwrap.dedent("""\
    MemTotal:       16384000 kB
    MemFree:          100000 kB
    MemAvailable:     500000 kB
    SwapTotal:       8192000 kB
    SwapFree:         500000 kB
""")

FAKE_LOADAVG = "2.50 1.75 1.25 3/512 12345\n"


# ---------------------------------------------------------------------------
# Tests: parse_meminfo
# ---------------------------------------------------------------------------


class TestParseMeminfo:
    def test_basic_parse(self, tmp_path):
        p = tmp_path / "meminfo"
        p.write_text(FAKE_MEMINFO)
        result = parse_meminfo(str(p))
        assert result["MemTotal"] == 16384000
        assert result["MemAvailable"] == 4096000
        assert result["SwapTotal"] == 8192000
        assert result["SwapFree"] == 6144000

    def test_missing_file(self):
        result = parse_meminfo("/nonexistent/meminfo")
        assert result == {}

    def test_empty_file(self, tmp_path):
        p = tmp_path / "meminfo"
        p.write_text("")
        result = parse_meminfo(str(p))
        assert result == {}

    def test_malformed_lines(self, tmp_path):
        p = tmp_path / "meminfo"
        p.write_text("no_colon_here\nGood:  1234 kB\nBad: notanumber kB\n")
        result = parse_meminfo(str(p))
        assert result == {"Good": 1234}


# ---------------------------------------------------------------------------
# Tests: parse_loadavg
# ---------------------------------------------------------------------------


class TestParseLoadavg:
    def test_basic_parse(self, tmp_path):
        p = tmp_path / "loadavg"
        p.write_text(FAKE_LOADAVG)
        result = parse_loadavg(str(p))
        assert result == (2.50, 1.75, 1.25)

    def test_missing_file(self):
        result = parse_loadavg("/nonexistent/loadavg")
        assert result == (0.0, 0.0, 0.0)

    def test_short_content(self, tmp_path):
        p = tmp_path / "loadavg"
        p.write_text("1.0 2.0\n")
        result = parse_loadavg(str(p))
        assert result == (0.0, 0.0, 0.0)


# ---------------------------------------------------------------------------
# Tests: classify_pressure
# ---------------------------------------------------------------------------


class TestClassifyPressure:
    def test_nominal(self):
        assert classify_pressure(50.0, 10.0) == MemoryPressureLevel.NOMINAL

    def test_elevated_by_swap(self):
        assert classify_pressure(50.0, 25.0) == MemoryPressureLevel.ELEVATED

    def test_elevated_by_mem(self):
        assert classify_pressure(80.0, 10.0) == MemoryPressureLevel.ELEVATED

    def test_high_requires_both_swap_and_mem(self):
        """Swap alone with low memory should NOT trigger HIGH."""
        # swap 57% + mem 21% → ELEVATED (not HIGH) — normal Linux behavior
        assert classify_pressure(21.0, 57.0) == MemoryPressureLevel.ELEVATED

    def test_high_by_swap_and_mem(self):
        """HIGH requires swap >= 50% AND mem >= 60%."""
        assert classify_pressure(65.0, 57.0) == MemoryPressureLevel.HIGH

    def test_high_by_mem_alone(self):
        """Memory alone can still trigger HIGH."""
        assert classify_pressure(90.0, 10.0) == MemoryPressureLevel.HIGH

    def test_critical_requires_both_swap_and_mem(self):
        """Swap alone with low memory should NOT trigger CRITICAL."""
        # swap 80% + mem 65% → HIGH (swap exceeds critical threshold but mem < 70%)
        assert classify_pressure(65.0, 80.0) == MemoryPressureLevel.HIGH

    def test_critical_by_swap_and_mem(self):
        """CRITICAL requires swap >= 75% AND mem >= 70%."""
        assert classify_pressure(75.0, 80.0) == MemoryPressureLevel.CRITICAL

    def test_critical_by_mem_alone(self):
        assert classify_pressure(95.0, 10.0) == MemoryPressureLevel.CRITICAL

    def test_custom_thresholds(self):
        # With very low thresholds, even mild usage triggers CRITICAL
        # mem 30% >= critical_mem 25% → CRITICAL
        assert (
            classify_pressure(
                30.0,
                5.0,
                elevated_swap_pct=2.0,
                elevated_mem_pct=10.0,
                high_swap_pct=3.0,
                high_mem_pct=20.0,
                critical_swap_pct=4.0,
                critical_mem_pct=25.0,
            )
            == MemoryPressureLevel.CRITICAL
        )

    def test_zero_swap(self):
        # No swap at all — pressure comes from mem only
        assert classify_pressure(50.0, 0.0) == MemoryPressureLevel.NOMINAL
        assert classify_pressure(95.0, 0.0) == MemoryPressureLevel.CRITICAL

    def test_boundary_values(self):
        # Exactly at threshold should trigger (>=)
        assert classify_pressure(80.0, 0.0) == MemoryPressureLevel.ELEVATED
        assert classify_pressure(79.9, 0.0) == MemoryPressureLevel.NOMINAL


# ---------------------------------------------------------------------------
# Tests: ResourceSnapshot
# ---------------------------------------------------------------------------


class TestResourceSnapshot:
    def test_to_dict(self):
        snap = ResourceSnapshot(
            timestamp=1700000000.0,
            mem_total_mb=16000.0,
            mem_available_mb=4000.0,
            mem_used_mb=12000.0,
            mem_percent=75.0,
            swap_total_mb=8000.0,
            swap_used_mb=2000.0,
            swap_percent=25.0,
            load_1m=2.5,
            load_5m=1.75,
            load_15m=1.25,
            cpu_count=8,
            pressure_level=MemoryPressureLevel.ELEVATED,
            dstate_pids={1234: "npm", 5678: "tsc"},
        )
        d = snap.to_dict()
        assert d["pressure_level"] == "elevated"
        assert d["mem_percent"] == 75.0
        assert d["dstate_pids"] == {"1234": "npm", "5678": "tsc"}
        assert d["cpu_count"] == 8

    def test_frozen(self):
        snap = ResourceSnapshot(
            timestamp=0,
            mem_total_mb=0,
            mem_available_mb=0,
            mem_used_mb=0,
            mem_percent=0,
            swap_total_mb=0,
            swap_used_mb=0,
            swap_percent=0,
            load_1m=0,
            load_5m=0,
            load_15m=0,
            cpu_count=1,
            pressure_level=MemoryPressureLevel.NOMINAL,
        )
        with pytest.raises(AttributeError):
            snap.mem_percent = 99.0  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Tests: take_snapshot (with monkeypatched /proc)
# ---------------------------------------------------------------------------


class TestTakeSnapshot:
    def test_snapshot_from_fake_proc(self, tmp_path, monkeypatch):
        meminfo_path = tmp_path / "meminfo"
        meminfo_path.write_text(FAKE_MEMINFO)
        loadavg_path = tmp_path / "loadavg"
        loadavg_path.write_text(FAKE_LOADAVG)

        # Monkeypatch the parse functions to use our fake files
        monkeypatch.setattr(
            "swarm.resources.monitor.parse_meminfo",
            lambda path="/proc/meminfo": parse_meminfo(str(meminfo_path)),
        )
        monkeypatch.setattr(
            "swarm.resources.monitor.parse_loadavg",
            lambda path="/proc/loadavg": parse_loadavg(str(loadavg_path)),
        )

        snap = take_snapshot(set(), dstate_scan=False)
        assert snap.mem_total_mb == pytest.approx(16000.0, rel=0.01)
        assert snap.mem_available_mb == pytest.approx(4000.0, rel=0.01)
        assert snap.swap_total_mb == pytest.approx(8000.0, rel=0.01)
        assert snap.load_1m == pytest.approx(2.50)
        assert snap.pressure_level == MemoryPressureLevel.ELEVATED  # swap ~25%

    def test_snapshot_critical(self, tmp_path, monkeypatch):
        meminfo_path = tmp_path / "meminfo"
        meminfo_path.write_text(FAKE_MEMINFO_CRITICAL)
        loadavg_path = tmp_path / "loadavg"
        loadavg_path.write_text(FAKE_LOADAVG)

        monkeypatch.setattr(
            "swarm.resources.monitor.parse_meminfo",
            lambda path="/proc/meminfo": parse_meminfo(str(meminfo_path)),
        )
        monkeypatch.setattr(
            "swarm.resources.monitor.parse_loadavg",
            lambda path="/proc/loadavg": parse_loadavg(str(loadavg_path)),
        )

        snap = take_snapshot(set(), dstate_scan=False)
        assert snap.pressure_level == MemoryPressureLevel.CRITICAL
        assert snap.mem_percent > 95.0

    def test_snapshot_no_swap(self, tmp_path, monkeypatch):
        meminfo_path = tmp_path / "meminfo"
        meminfo_path.write_text(FAKE_MEMINFO_NO_SWAP)
        loadavg_path = tmp_path / "loadavg"
        loadavg_path.write_text("0.1 0.2 0.3 1/100 999\n")

        monkeypatch.setattr(
            "swarm.resources.monitor.parse_meminfo",
            lambda path="/proc/meminfo": parse_meminfo(str(meminfo_path)),
        )
        monkeypatch.setattr(
            "swarm.resources.monitor.parse_loadavg",
            lambda path="/proc/loadavg": parse_loadavg(str(loadavg_path)),
        )

        snap = take_snapshot(set(), dstate_scan=False)
        assert snap.swap_percent == 0.0
        assert snap.pressure_level == MemoryPressureLevel.NOMINAL


# ---------------------------------------------------------------------------
# Tests: find_dstate_descendants
# ---------------------------------------------------------------------------


class TestFindDstateDescendants:
    def test_empty_pids(self):
        result = find_dstate_descendants(set())
        assert result == {}

    def test_no_proc_access(self, monkeypatch):
        # When /proc is not accessible, should return empty dict gracefully
        monkeypatch.setattr(
            "swarm.resources.monitor._get_descendants",
            lambda pids: set(),
        )
        result = find_dstate_descendants({1})
        # Will try to read /proc/1/stat which likely fails — should not crash
        assert isinstance(result, dict)
