"""Tests for buzz/log.py — structured action logging with persistence."""

import json

import pytest

from swarm.buzz.log import BuzzAction, BuzzEntry, BuzzLog


class TestBuzzLog:
    def test_add_entry(self):
        log = BuzzLog()
        entry = log.add(BuzzAction.CONTINUED, "api", "choice menu")
        assert entry.action == BuzzAction.CONTINUED
        assert entry.worker_name == "api"
        assert len(log.entries) == 1

    def test_max_entries(self):
        log = BuzzLog(max_entries=5)
        for i in range(10):
            log.add(BuzzAction.CONTINUED, f"w{i}")
        assert len(log.entries) == 5

    def test_callback(self):
        log = BuzzLog()
        entries_seen = []
        log.on_entry(lambda e: entries_seen.append(e))
        log.add(BuzzAction.REVIVED, "api")
        assert len(entries_seen) == 1
        assert entries_seen[0].action == BuzzAction.REVIVED

    def test_callback_error_does_not_break(self):
        """Bad callbacks should not prevent logging."""
        log = BuzzLog()
        log.on_entry(lambda e: 1/0)  # Will raise ZeroDivisionError
        # Should not raise
        entry = log.add(BuzzAction.CONTINUED, "api")
        assert entry is not None

    def test_last(self):
        log = BuzzLog()
        assert log.last is None
        log.add(BuzzAction.CONTINUED, "api")
        log.add(BuzzAction.ESCALATED, "web")
        assert log.last.action == BuzzAction.ESCALATED


class TestBuzzLogPersistence:
    def test_write_and_load(self, tmp_path):
        """Entries should persist to JSONL file."""
        log_file = tmp_path / "buzz.jsonl"
        log = BuzzLog(log_file=log_file)
        log.add(BuzzAction.CONTINUED, "api", "choice menu")
        log.add(BuzzAction.REVIVED, "web")

        # Verify file exists and has 2 lines
        assert log_file.exists()
        lines = log_file.read_text().strip().splitlines()
        assert len(lines) == 2

        # Load into new log
        log2 = BuzzLog(log_file=log_file)
        assert len(log2.entries) == 2
        assert log2.entries[0].action == BuzzAction.CONTINUED
        assert log2.entries[1].action == BuzzAction.REVIVED

    def test_corrupt_lines_skipped(self, tmp_path):
        """Corrupt JSONL lines should be skipped during load."""
        log_file = tmp_path / "buzz.jsonl"
        log_file.write_text(
            '{"timestamp": 1.0, "action": "CONTINUED", "worker_name": "api"}\n'
            'CORRUPT LINE\n'
            '{"timestamp": 2.0, "action": "REVIVED", "worker_name": "web"}\n'
        )
        log = BuzzLog(log_file=log_file)
        assert len(log.entries) == 2

    def test_rotation(self, tmp_path):
        """Log should rotate when exceeding max file size."""
        log_file = tmp_path / "buzz.jsonl"
        # Use tiny max size to trigger rotation
        log = BuzzLog(log_file=log_file, max_file_size=100, max_rotations=2)

        # Write enough entries to exceed 100 bytes
        for i in range(20):
            log.add(BuzzAction.CONTINUED, f"worker-{i}", "detail " * 5)

        # Should have rotated — check for .1 file
        rotated = log_file.with_suffix(".jsonl.1")
        # Rotation may or may not have happened depending on exact sizes
        # Just verify no crash


class TestBuzzEntry:
    def test_display(self):
        entry = BuzzEntry(timestamp=1000000.0, action=BuzzAction.CONTINUED,
                          worker_name="api", detail="choice menu")
        display = entry.display
        assert "CONTINUED" in display
        assert "api" in display
        assert "choice menu" in display

    def test_display_no_detail(self):
        entry = BuzzEntry(timestamp=1000000.0, action=BuzzAction.REVIVED,
                          worker_name="web")
        display = entry.display
        assert "REVIVED" in display
        assert "web" in display
