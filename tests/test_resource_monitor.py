"""Tests for the /proc resource monitor — uses monkeypatched file reads."""

from __future__ import annotations

import textwrap

import pytest

from swarm.resources.monitor import (
    MemoryPressureLevel,
    ResourceSnapshot,
    classify_pressure,
    compute_swap_io_rate,
    find_dstate_descendants,
    parse_loadavg,
    parse_meminfo,
    parse_psi_some_avg10,
    parse_vmstat_swap,
    take_snapshot,
    top_workers_by_rss,
)

# ---------------------------------------------------------------------------
# Fixtures: fake /proc content
# ---------------------------------------------------------------------------

FAKE_MEMINFO = textwrap.dedent("""\
    MemTotal:       16384000 kB
    MemFree:         2048000 kB
    MemAvailable:    3276800 kB
    Buffers:          512000 kB
    Cached:          2048000 kB
    SwapCached:        10000 kB
    Active:          8000000 kB
    Inactive:        4000000 kB
    SwapTotal:       8192000 kB
    SwapFree:        6144000 kB
    Dirty:              1024 kB
    Writeback:             0 kB
    AnonPages:      13000000 kB
    Mapped:          1000000 kB
    Shmem:            500000 kB
""")
# mem ~80% (ELEVATED by mem), swap ~25% (below new elevated_swap_pct=40).
# Before the 2026-04-22 threshold tuning, mem was 75% / swap 25% and the
# pressure level fell out of swap >= 25%; that trigger is gone now, so the
# fixture was reshaped to keep the pressure-level assertion meaningful.

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

# /proc/pressure/{cpu,memory,io} — kernel PSI files. Two lines:
#   some avg10=<pct> avg60=<pct> avg300=<pct> total=<microseconds>
#   full avg10=<pct> avg60=<pct> avg300=<pct> total=<microseconds>
# We track only the first line's avg10 — the canonical "are we hurting now"
# signal per https://docs.kernel.org/accounting/psi.html.
FAKE_PSI_MEMORY_QUIET = textwrap.dedent("""\
    some avg10=0.00 avg60=0.00 avg300=0.00 total=0
    full avg10=0.00 avg60=0.00 avg300=0.00 total=0
""")

FAKE_PSI_MEMORY_STALLING = textwrap.dedent("""\
    some avg10=15.32 avg60=8.10 avg300=2.45 total=12345678
    full avg10=8.74 avg60=4.20 avg300=1.10 total=6789012
""")

FAKE_PSI_MEMORY_HEAVY = textwrap.dedent("""\
    some avg10=42.91 avg60=30.00 avg300=15.00 total=98765432
    full avg10=20.00 avg60=15.00 avg300=8.00 total=54321000
""")

# /proc/vmstat — kernel cumulative counters. We only consume pswpin / pswpout.
FAKE_VMSTAT = textwrap.dedent("""\
    nr_free_pages 123456
    nr_zone_inactive_anon 78901
    nr_zone_active_anon 234567
    pgpgin 1000000
    pgpgout 2000000
    pswpin 500
    pswpout 750
    pgfault 99999999
    pgmajfault 12345
""")

FAKE_VMSTAT_LATER = textwrap.dedent("""\
    nr_free_pages 123000
    pswpin 580
    pswpout 900
    pgmajfault 12400
""")


# ---------------------------------------------------------------------------
# Tests: parse_psi_some_avg10
# ---------------------------------------------------------------------------


class TestParsePsi:
    def test_quiet_psi_is_zero(self, tmp_path):
        p = tmp_path / "memory"
        p.write_text(FAKE_PSI_MEMORY_QUIET)
        assert parse_psi_some_avg10(str(p)) == 0.0

    def test_stalling_psi(self, tmp_path):
        p = tmp_path / "memory"
        p.write_text(FAKE_PSI_MEMORY_STALLING)
        assert parse_psi_some_avg10(str(p)) == pytest.approx(15.32)

    def test_missing_file_returns_none(self):
        """``CONFIG_PSI=n`` kernels have no ``/proc/pressure/*`` files."""
        assert parse_psi_some_avg10("/proc/pressure/_does_not_exist_") is None

    def test_empty_file_returns_none(self, tmp_path):
        p = tmp_path / "memory"
        p.write_text("")
        assert parse_psi_some_avg10(str(p)) is None

    def test_malformed_returns_none(self, tmp_path):
        p = tmp_path / "memory"
        p.write_text("garbage\nmore garbage\n")
        assert parse_psi_some_avg10(str(p)) is None

    def test_only_full_line_returns_none(self, tmp_path):
        """We require the ``some`` line — ``full`` alone is incomplete."""
        p = tmp_path / "memory"
        p.write_text("full avg10=5.00 avg60=2.00 avg300=1.00 total=12345\n")
        assert parse_psi_some_avg10(str(p)) is None


# ---------------------------------------------------------------------------
# Tests: parse_vmstat_swap + compute_swap_io_rate
# ---------------------------------------------------------------------------


class TestParseVmstatSwap:
    def test_basic_parse(self, tmp_path):
        p = tmp_path / "vmstat"
        p.write_text(FAKE_VMSTAT)
        pswpin, pswpout = parse_vmstat_swap(str(p))
        assert pswpin == 500
        assert pswpout == 750

    def test_missing_file(self):
        pswpin, pswpout = parse_vmstat_swap("/nonexistent")
        assert pswpin == 0
        assert pswpout == 0

    def test_missing_keys_default_to_zero(self, tmp_path):
        p = tmp_path / "vmstat"
        p.write_text("nr_free_pages 123\n")  # no pswp* lines
        pswpin, pswpout = parse_vmstat_swap(str(p))
        assert pswpin == 0
        assert pswpout == 0


class TestComputeSwapIoRate:
    def test_no_prev_yields_zero(self):
        rate_in, rate_out = compute_swap_io_rate(
            prev_in=None, prev_out=None, prev_ts=None, cur_in=500, cur_out=750, cur_ts=100.0
        )
        # First sample has no baseline — rate is unknown, return 0 (the
        # honest answer; we'll have a real value next tick).
        assert rate_in == 0.0
        assert rate_out == 0.0

    def test_delta_per_second(self):
        rate_in, rate_out = compute_swap_io_rate(
            prev_in=500, prev_out=750, prev_ts=100.0, cur_in=580, cur_out=900, cur_ts=110.0
        )
        # 80 pages over 10s → 8.0 pages/sec
        assert rate_in == pytest.approx(8.0)
        # 150 pages over 10s → 15.0 pages/sec
        assert rate_out == pytest.approx(15.0)

    def test_counter_rollover_or_reboot_yields_zero(self):
        """Cumulative counter going DOWN means a reboot or wrap — don't
        report a negative rate; treat as zero and re-baseline next tick."""
        rate_in, rate_out = compute_swap_io_rate(
            prev_in=10000, prev_out=20000, prev_ts=100.0, cur_in=50, cur_out=100, cur_ts=110.0
        )
        assert rate_in == 0.0
        assert rate_out == 0.0

    def test_zero_dt_yields_zero(self):
        """Defensive: identical timestamps shouldn't divide by zero."""
        rate_in, rate_out = compute_swap_io_rate(
            prev_in=500, prev_out=750, prev_ts=100.0, cur_in=580, cur_out=900, cur_ts=100.0
        )
        assert rate_in == 0.0
        assert rate_out == 0.0


# ---------------------------------------------------------------------------
# Tests: top_workers_by_rss
# ---------------------------------------------------------------------------


class TestTopWorkersByRss:
    def test_no_names_returns_empty(self):
        # When the caller can't supply name-mapped PIDs, we don't try to
        # invent names — return an empty list. The dashboard hides the
        # row entirely in that case.
        assert top_workers_by_rss({}, top_n=3) == []

    def test_orders_by_rss_descending(self, tmp_path, monkeypatch):
        """Three workers with known RSS sums sort largest first."""
        proc_root = tmp_path / "proc"
        proc_root.mkdir()
        # Fake three flat process trees (no children) for simplicity.
        # status:VmRSS is in kB; we'll convert to MB in the result.
        for pid, rss_kb in [(101, 2_048_000), (102, 512_000), (103, 8_192_000)]:
            d = proc_root / str(pid)
            d.mkdir()
            (d / "status").write_text(f"Name:\tworker\nVmRSS:\t{rss_kb} kB\n")
            # No children: stat produces an empty children list when we
            # walk parent_map.
            (d / "stat").write_text(f"{pid} (worker) S 1 1 1 0 -1 0 0 0 0 0 0 0 0 0 20 0 1 0\n")
        # The walker also reads other /proc entries to find children;
        # provide a minimal init-process row so the listdir at least
        # doesn't see only worker pids.
        (proc_root / "1").mkdir()
        (proc_root / "1" / "stat").write_text("1 (init) S 0 1 1 0 -1 0 0 0 0 0 0 0 0 0 20 0 1 0\n")
        (proc_root / "1" / "status").write_text("Name:\tinit\nVmRSS:\t1024 kB\n")

        monkeypatch.setattr("swarm.resources.monitor._PROC_ROOT", str(proc_root))
        result = top_workers_by_rss(
            {101: "alpha", 102: "beta", 103: "gamma"},
            top_n=3,
        )
        # Largest first
        assert [name for name, _ in result] == ["gamma", "alpha", "beta"]
        # MB conversion: 8_192_000 kB ≈ 8000 MB
        names = {name: mb for name, mb in result}
        assert names["gamma"] == pytest.approx(8000.0, rel=0.01)
        assert names["alpha"] == pytest.approx(2000.0, rel=0.01)

    def test_top_n_clamps(self, tmp_path, monkeypatch):
        """``top_n=2`` returns only the two heaviest."""
        proc_root = tmp_path / "proc"
        proc_root.mkdir()
        for pid, rss_kb in [(201, 100_000), (202, 200_000), (203, 300_000)]:
            d = proc_root / str(pid)
            d.mkdir()
            (d / "status").write_text(f"Name:\tw\nVmRSS:\t{rss_kb} kB\n")
            (d / "stat").write_text(f"{pid} (w) S 1 1 1 0 -1 0 0 0 0 0 0 0 0 0 20 0 1 0\n")
        (proc_root / "1").mkdir()
        (proc_root / "1" / "stat").write_text("1 (init) S 0 1 1 0 -1 0 0 0 0 0 0 0 0 0 20 0 1 0\n")
        (proc_root / "1" / "status").write_text("Name:\tinit\nVmRSS:\t1024 kB\n")

        monkeypatch.setattr("swarm.resources.monitor._PROC_ROOT", str(proc_root))
        result = top_workers_by_rss(
            {201: "a", 202: "b", 203: "c"},
            top_n=2,
        )
        assert len(result) == 2
        assert [name for name, _ in result] == ["c", "b"]


# ---------------------------------------------------------------------------
# Tests: parse_meminfo
# ---------------------------------------------------------------------------


class TestParseMeminfo:
    def test_basic_parse(self, tmp_path):
        p = tmp_path / "meminfo"
        p.write_text(FAKE_MEMINFO)
        result = parse_meminfo(str(p))
        assert result["MemTotal"] == 16384000
        assert result["MemAvailable"] == 3276800
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
        # swap >= elevated_swap_pct (40) alone -> ELEVATED (informational)
        assert classify_pressure(50.0, 40.0) == MemoryPressureLevel.ELEVATED

    def test_elevated_by_mem(self):
        assert classify_pressure(80.0, 10.0) == MemoryPressureLevel.ELEVATED

    def test_high_requires_both_swap_and_mem(self):
        """Swap alone with moderate memory should NOT trigger HIGH."""
        # swap 75% + mem 62% → ELEVATED (not HIGH) — this is the reported
        # regression from the 2026-04-22 dev-machine incident.  mem must be
        # >= elevated_mem_pct (80) for the swap-coupled HIGH to fire.
        assert classify_pressure(62.0, 75.0) == MemoryPressureLevel.ELEVATED

    def test_high_by_swap_and_mem(self):
        """HIGH requires swap >= high_swap_pct (70) AND mem >= elevated_mem_pct (80)."""
        assert classify_pressure(82.0, 72.0) == MemoryPressureLevel.HIGH

    def test_high_by_mem_alone(self):
        """Memory alone at high_mem_pct still triggers HIGH."""
        assert classify_pressure(90.0, 10.0) == MemoryPressureLevel.HIGH

    def test_critical_requires_both_swap_and_mem(self):
        """Swap high with moderate memory should NOT trigger CRITICAL."""
        # swap 88% crosses critical_swap_pct (85) but mem 85 < high_mem_pct (90).
        # Should classify as HIGH (swap >= 70 AND mem >= 80).
        assert classify_pressure(85.0, 88.0) == MemoryPressureLevel.HIGH

    def test_critical_by_swap_and_mem(self):
        """CRITICAL requires swap >= critical_swap_pct (85) AND mem >= high_mem_pct (90)."""
        assert classify_pressure(92.0, 88.0) == MemoryPressureLevel.CRITICAL

    def test_critical_by_mem_alone(self):
        assert classify_pressure(95.0, 10.0) == MemoryPressureLevel.CRITICAL

    def test_swap_sticky_does_not_suspend(self):
        """Regression: sticky swap on a low-pressure dev machine.

        Observed 2026-04-22: mem=62%, swap=60%.  Old logic suspended all 5
        workers.  New logic keeps them running — swap alone without genuine
        memory pressure is not a reason to stop work.
        """
        result = classify_pressure(62.0, 60.0)
        assert result == MemoryPressureLevel.ELEVATED
        assert result != MemoryPressureLevel.HIGH

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

    # PSI override (task #352): the kernel reporting that processes
    # *actually stalled* trumps percentage heuristics. PSI never demotes;
    # it can only raise the level the percent logic computed.

    def test_psi_mem_promotes_nominal_to_elevated(self):
        """PSI mem stall >= 10% must force at least ELEVATED."""
        result = classify_pressure(20.0, 5.0, psi_mem_avg10=15.0)
        assert result == MemoryPressureLevel.ELEVATED

    def test_psi_mem_promotes_to_high(self):
        """PSI mem stall >= 30% must force at least HIGH."""
        result = classify_pressure(20.0, 5.0, psi_mem_avg10=35.0)
        assert result == MemoryPressureLevel.HIGH

    def test_psi_does_not_demote(self):
        """A high percentage-based level stays high even when PSI is calm."""
        # mem 95% would normally hit CRITICAL; PSI=0 must not demote it.
        result = classify_pressure(95.0, 10.0, psi_mem_avg10=0.0)
        assert result == MemoryPressureLevel.CRITICAL

    def test_psi_zero_means_no_override(self):
        """PSI=0 (or unavailable / disabled kernel) defaults to no effect."""
        # Verifies the override is a *floor*, not a replacement.
        assert classify_pressure(50.0, 10.0, psi_mem_avg10=0.0) == MemoryPressureLevel.NOMINAL


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
        assert snap.mem_available_mb == pytest.approx(3200.0, rel=0.01)
        assert snap.swap_total_mb == pytest.approx(8000.0, rel=0.01)
        assert snap.load_1m == pytest.approx(2.50)
        # mem ~80% -> ELEVATED by mem_pct (swap ~25% is below the new
        # elevated_swap_pct=40 threshold).
        assert snap.pressure_level == MemoryPressureLevel.ELEVATED

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

    def test_snapshot_includes_psi_when_available(self, tmp_path, monkeypatch):
        """Task #352: PSI fields populate from /proc/pressure/* when readable."""
        meminfo_path = tmp_path / "meminfo"
        meminfo_path.write_text(FAKE_MEMINFO)
        loadavg_path = tmp_path / "loadavg"
        loadavg_path.write_text(FAKE_LOADAVG)
        psi_dir = tmp_path / "pressure"
        psi_dir.mkdir()
        (psi_dir / "cpu").write_text(FAKE_PSI_MEMORY_QUIET)
        (psi_dir / "memory").write_text(FAKE_PSI_MEMORY_STALLING)
        (psi_dir / "io").write_text(FAKE_PSI_MEMORY_QUIET)

        monkeypatch.setattr(
            "swarm.resources.monitor.parse_meminfo",
            lambda path="/proc/meminfo": parse_meminfo(str(meminfo_path)),
        )
        monkeypatch.setattr(
            "swarm.resources.monitor.parse_loadavg",
            lambda path="/proc/loadavg": parse_loadavg(str(loadavg_path)),
        )
        monkeypatch.setattr("swarm.resources.monitor._PSI_DIR", str(psi_dir))

        snap = take_snapshot(set(), dstate_scan=False)
        assert snap.psi_available is True
        assert snap.psi_cpu_avg10 == pytest.approx(0.0)
        assert snap.psi_mem_avg10 == pytest.approx(15.32)
        assert snap.psi_io_avg10 == pytest.approx(0.0)
        # Mem% ~80 alone would be ELEVATED; PSI mem 15.32 keeps it ELEVATED
        # (PSI promotes nominal→elevated, doesn't demote).
        assert snap.pressure_level in {
            MemoryPressureLevel.ELEVATED,
            MemoryPressureLevel.HIGH,
        }

    def test_snapshot_psi_unavailable_kernels(self, tmp_path, monkeypatch):
        """``CONFIG_PSI=n`` kernels: ``psi_available=False`` and zeros."""
        meminfo_path = tmp_path / "meminfo"
        meminfo_path.write_text(FAKE_MEMINFO_NO_SWAP)
        loadavg_path = tmp_path / "loadavg"
        loadavg_path.write_text(FAKE_LOADAVG)
        # Point _PSI_DIR at a non-existent directory.
        monkeypatch.setattr("swarm.resources.monitor._PSI_DIR", str(tmp_path / "no_psi"))
        monkeypatch.setattr(
            "swarm.resources.monitor.parse_meminfo",
            lambda path="/proc/meminfo": parse_meminfo(str(meminfo_path)),
        )
        monkeypatch.setattr(
            "swarm.resources.monitor.parse_loadavg",
            lambda path="/proc/loadavg": parse_loadavg(str(loadavg_path)),
        )

        snap = take_snapshot(set(), dstate_scan=False)
        assert snap.psi_available is False
        assert snap.psi_cpu_avg10 == 0.0
        assert snap.psi_mem_avg10 == 0.0
        assert snap.psi_io_avg10 == 0.0

    def test_snapshot_swap_io_rate_from_prev(self, tmp_path, monkeypatch):
        """Stateful diffing: caller passes prev counters → take_snapshot
        computes per-second rates."""
        meminfo_path = tmp_path / "meminfo"
        meminfo_path.write_text(FAKE_MEMINFO_NO_SWAP)
        loadavg_path = tmp_path / "loadavg"
        loadavg_path.write_text(FAKE_LOADAVG)
        vmstat_path = tmp_path / "vmstat"
        vmstat_path.write_text(FAKE_VMSTAT_LATER)

        monkeypatch.setattr(
            "swarm.resources.monitor.parse_meminfo",
            lambda path="/proc/meminfo": parse_meminfo(str(meminfo_path)),
        )
        monkeypatch.setattr(
            "swarm.resources.monitor.parse_loadavg",
            lambda path="/proc/loadavg": parse_loadavg(str(loadavg_path)),
        )
        monkeypatch.setattr("swarm.resources.monitor._VMSTAT_PATH", str(vmstat_path))

        snap = take_snapshot(
            set(),
            dstate_scan=False,
            prev_swap_io=(500, 750, 100.0),
            now_override=110.0,
        )
        # 80 pages / 10 s = 8.0
        assert snap.swap_in_per_sec == pytest.approx(8.0)
        # 150 pages / 10 s = 15.0
        assert snap.swap_out_per_sec == pytest.approx(15.0)

    def test_snapshot_skips_top_workers_when_nominal(self, tmp_path, monkeypatch):
        """When pressure is NOMINAL, top_workers_by_rss stays empty
        (cheap fast-path — no /proc/<pid>/status reads)."""
        meminfo_path = tmp_path / "meminfo"
        meminfo_path.write_text(FAKE_MEMINFO_NO_SWAP)  # → NOMINAL
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

        snap = take_snapshot(
            set(),
            dstate_scan=False,
            worker_names={101: "alpha", 102: "beta"},
        )
        assert snap.pressure_level == MemoryPressureLevel.NOMINAL
        # Cheap path: skip RSS scan when nothing is wrong.
        assert snap.top_workers_by_rss == []


# ---------------------------------------------------------------------------
# Tests: ResourceSnapshot.to_dict — backwards-compat keys + new keys
# ---------------------------------------------------------------------------


class TestResourceSnapshotPhase352Serialization:
    def test_to_dict_keeps_legacy_keys(self):
        """Phase 3 of task #352 contract: existing API consumers may
        depend on ``mem_percent`` / ``swap_percent`` / ``pressure_level``,
        so they MUST stay in ``to_dict()`` even after the new metrics
        land. New fields are additive only."""
        snap = ResourceSnapshot(
            timestamp=1.0,
            mem_total_mb=100.0,
            mem_available_mb=50.0,
            mem_used_mb=50.0,
            mem_percent=50.0,
            swap_total_mb=10.0,
            swap_used_mb=2.0,
            swap_percent=20.0,
            load_1m=0.5,
            load_5m=0.4,
            load_15m=0.3,
            cpu_count=4,
            pressure_level=MemoryPressureLevel.NOMINAL,
        )
        d = snap.to_dict()
        for legacy in ("mem_percent", "swap_percent", "pressure_level", "load_1m"):
            assert legacy in d, f"legacy key '{legacy}' missing from to_dict()"

    def test_to_dict_includes_new_psi_swap_io_fields(self):
        snap = ResourceSnapshot(
            timestamp=1.0,
            mem_total_mb=100.0,
            mem_available_mb=50.0,
            mem_used_mb=50.0,
            mem_percent=50.0,
            swap_total_mb=10.0,
            swap_used_mb=2.0,
            swap_percent=20.0,
            load_1m=0.5,
            load_5m=0.4,
            load_15m=0.3,
            cpu_count=4,
            pressure_level=MemoryPressureLevel.NOMINAL,
            psi_available=True,
            psi_cpu_avg10=1.5,
            psi_mem_avg10=12.0,
            psi_io_avg10=0.0,
            swap_in_per_sec=2.5,
            swap_out_per_sec=0.0,
            top_workers_by_rss=[("alpha", 4096)],
        )
        d = snap.to_dict()
        assert d["psi_available"] is True
        assert d["psi_mem_avg10"] == pytest.approx(12.0)
        assert d["swap_in_per_sec"] == pytest.approx(2.5)
        # Top workers are tuples in the dataclass; serialize as list-of-lists
        # (or list-of-objects) so JSON is happy.
        top = d["top_workers_by_rss"]
        assert top and top[0][0] == "alpha"


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
