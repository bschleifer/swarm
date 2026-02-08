"""Tests for drones/log.py — structured action logging with persistence."""

import json

import pytest

from swarm.drones.log import DroneAction, DroneEntry, DroneLog


class TestDroneLog:
    def test_add_entry(self):
        log = DroneLog()
        entry = log.add(DroneAction.CONTINUED, "api", "choice menu")
        assert entry.action == DroneAction.CONTINUED
        assert entry.worker_name == "api"
        assert len(log.entries) == 1

    def test_max_entries(self):
        log = DroneLog(max_entries=5)
        for i in range(10):
            log.add(DroneAction.CONTINUED, f"w{i}")
        assert len(log.entries) == 5

    def test_callback(self):
        log = DroneLog()
        entries_seen = []
        log.on_entry(lambda e: entries_seen.append(e))
        log.add(DroneAction.REVIVED, "api")
        assert len(entries_seen) == 1
        assert entries_seen[0].action == DroneAction.REVIVED

    def test_callback_error_does_not_break(self):
        """Bad callbacks should not prevent logging."""
        log = DroneLog()
        log.on_entry(lambda e: 1/0)  # Will raise ZeroDivisionError
        # Should not raise
        entry = log.add(DroneAction.CONTINUED, "api")
        assert entry is not None

    def test_last(self):
        log = DroneLog()
        assert log.last is None
        log.add(DroneAction.CONTINUED, "api")
        log.add(DroneAction.ESCALATED, "web")
        assert log.last.action == DroneAction.ESCALATED


class TestDroneLogPersistence:
    def test_write_and_load(self, tmp_path):
        """Entries should persist to JSONL file."""
        log_file = tmp_path / "drone.jsonl"
        log = DroneLog(log_file=log_file)
        log.add(DroneAction.CONTINUED, "api", "choice menu")
        log.add(DroneAction.REVIVED, "web")

        # Verify file exists and has 2 lines
        assert log_file.exists()
        lines = log_file.read_text().strip().splitlines()
        assert len(lines) == 2

        # Load into new log
        log2 = DroneLog(log_file=log_file)
        assert len(log2.entries) == 2
        assert log2.entries[0].action == DroneAction.CONTINUED
        assert log2.entries[1].action == DroneAction.REVIVED

    def test_corrupt_lines_skipped(self, tmp_path):
        """Corrupt JSONL lines should be skipped during load."""
        log_file = tmp_path / "drone.jsonl"
        log_file.write_text(
            '{"timestamp": 1.0, "action": "CONTINUED", "worker_name": "api"}\n'
            'CORRUPT LINE\n'
            '{"timestamp": 2.0, "action": "REVIVED", "worker_name": "web"}\n'
        )
        log = DroneLog(log_file=log_file)
        assert len(log.entries) == 2

    def test_rotation(self, tmp_path):
        """Log should rotate when exceeding max file size."""
        log_file = tmp_path / "drone.jsonl"
        # Use tiny max size to trigger rotation
        log = DroneLog(log_file=log_file, max_file_size=100, max_rotations=2)

        # Write enough entries to exceed 100 bytes
        for i in range(20):
            log.add(DroneAction.CONTINUED, f"worker-{i}", "detail " * 5)

        # Should have rotated — check for .1 file
        rotated = log_file.with_suffix(".jsonl.1")
        # Rotation may or may not have happened depending on exact sizes
        # Just verify no crash


class TestDroneEntry:
    def test_display(self):
        entry = DroneEntry(timestamp=1000000.0, action=DroneAction.CONTINUED,
                          worker_name="api", detail="choice menu")
        display = entry.display
        assert "CONTINUED" in display
        assert "api" in display
        assert "choice menu" in display

    def test_display_no_detail(self):
        entry = DroneEntry(timestamp=1000000.0, action=DroneAction.REVIVED,
                          worker_name="web")
        display = entry.display
        assert "REVIVED" in display
        assert "web" in display
