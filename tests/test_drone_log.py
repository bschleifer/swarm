"""Tests for drones/log.py — structured action logging with persistence."""

from swarm.drones.log import (
    DroneAction,
    DroneEntry,
    DroneLog,
    LogCategory,
    SystemAction,
    SystemEntry,
    SystemLog,
)


class TestDroneLog:
    def test_add_entry(self):
        log = DroneLog()
        entry = log.add(DroneAction.CONTINUED, "api", "choice menu")
        assert entry.action == SystemAction.CONTINUED
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
        assert entries_seen[0].action == SystemAction.REVIVED

    def test_callback_error_does_not_break(self):
        """Bad callbacks should not prevent logging."""
        log = DroneLog()
        log.on_entry(lambda e: 1 / 0)  # Will raise ZeroDivisionError
        # Should not raise
        entry = log.add(DroneAction.CONTINUED, "api")
        assert entry is not None

    def test_last(self):
        log = DroneLog()
        assert log.last is None
        log.add(DroneAction.CONTINUED, "api")
        log.add(DroneAction.ESCALATED, "web")
        assert log.last.action == SystemAction.ESCALATED


class TestDroneLogPersistence:
    def test_write_and_load(self, tmp_path):
        """Entries should persist to JSONL file."""
        log_file = tmp_path / "system.jsonl"
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
        assert log2.entries[0].action == SystemAction.CONTINUED
        assert log2.entries[1].action == SystemAction.REVIVED

    def test_corrupt_lines_skipped(self, tmp_path):
        """Corrupt JSONL lines should be skipped during load."""
        log_file = tmp_path / "system.jsonl"
        log_file.write_text(
            '{"timestamp": 1.0, "action": "CONTINUED", "worker_name": "api"}\n'
            "CORRUPT LINE\n"
            '{"timestamp": 2.0, "action": "REVIVED", "worker_name": "web"}\n'
        )
        log = DroneLog(log_file=log_file)
        assert len(log.entries) == 2

    def test_rotation(self, tmp_path):
        """Log should rotate when exceeding max file size."""
        log_file = tmp_path / "system.jsonl"
        # Use tiny max size to trigger rotation
        log = DroneLog(log_file=log_file, max_file_size=100, max_rotations=2)

        # Write enough entries to exceed 100 bytes
        for i in range(20):
            log.add(DroneAction.CONTINUED, f"worker-{i}", "detail " * 5)

        # After writing 20 entries at ~100 byte limit, rotation should have occurred
        rotated = log_file.with_suffix(".jsonl.1")
        assert rotated.exists(), "Expected rotation file .jsonl.1 to exist"

    def test_legacy_migration(self, tmp_path):
        """Should load from legacy drone.jsonl when system.jsonl doesn't exist."""
        legacy_file = tmp_path / "drone.jsonl"
        legacy_file.write_text(
            '{"timestamp": 1.0, "action": "CONTINUED", "worker_name": "api"}\n'
            '{"timestamp": 2.0, "action": "REVIVED", "worker_name": "web"}\n'
        )

        system_file = tmp_path / "system.jsonl"
        log = SystemLog(log_file=system_file)
        assert len(log.entries) == 2
        # Legacy entries default to drone category
        assert log.entries[0].category == LogCategory.DRONE
        assert log.entries[0].is_notification is False

    def test_backward_compat_missing_fields(self, tmp_path):
        """JSONL entries without category/is_notification should default gracefully."""
        log_file = tmp_path / "system.jsonl"
        log_file.write_text(
            '{"timestamp": 1.0, "action": "CONTINUED", "worker_name": "api", "detail": "test"}\n'
        )
        log = SystemLog(log_file=log_file)
        assert len(log.entries) == 1
        assert log.entries[0].category == LogCategory.DRONE
        assert log.entries[0].is_notification is False

    def test_system_entries_persist_with_category(self, tmp_path):
        """SystemAction entries should persist category and is_notification."""
        log_file = tmp_path / "system.jsonl"
        log = SystemLog(log_file=log_file)
        log.add(
            SystemAction.TASK_CREATED,
            "user",
            "Fix the bug",
            category=LogCategory.TASK,
        )
        log.add(
            SystemAction.WORKER_STUNG,
            "api",
            "worker exited",
            category=LogCategory.WORKER,
            is_notification=True,
        )

        log2 = SystemLog(log_file=log_file)
        assert len(log2.entries) == 2
        assert log2.entries[0].action == SystemAction.TASK_CREATED
        assert log2.entries[0].category == LogCategory.TASK
        assert log2.entries[0].is_notification is False
        assert log2.entries[1].action == SystemAction.WORKER_STUNG
        assert log2.entries[1].category == LogCategory.WORKER
        assert log2.entries[1].is_notification is True


class TestDroneActionEnum:
    def test_operator_action(self):
        assert DroneAction.OPERATOR.value == "OPERATOR"

    def test_approved_action(self):
        assert DroneAction.APPROVED.value == "APPROVED"

    def test_rejected_action(self):
        assert DroneAction.REJECTED.value == "REJECTED"

    def test_operator_persists(self, tmp_path):
        log_file = tmp_path / "system.jsonl"
        log = DroneLog(log_file=log_file)
        log.add(DroneAction.OPERATOR, "api", "continued (manual)")
        log.add(DroneAction.APPROVED, "web", "proposal approved: Fix bug")
        log.add(DroneAction.REJECTED, "api", "proposal rejected: Add feature")

        log2 = DroneLog(log_file=log_file)
        assert len(log2.entries) == 3
        assert log2.entries[0].action == SystemAction.OPERATOR
        assert log2.entries[1].action == SystemAction.APPROVED
        assert log2.entries[2].action == SystemAction.REJECTED


class TestDroneEntry:
    def test_display(self):
        entry = DroneEntry(
            timestamp=1000000.0,
            action=DroneAction.CONTINUED,
            worker_name="api",
            detail="choice menu",
        )
        display = entry.display
        assert "CONTINUED" in display
        assert "api" in display
        assert "choice menu" in display

    def test_display_no_detail(self):
        entry = DroneEntry(timestamp=1000000.0, action=DroneAction.REVIVED, worker_name="web")
        display = entry.display
        assert "REVIVED" in display
        assert "web" in display


class TestSystemEntry:
    def test_display(self):
        entry = SystemEntry(
            timestamp=1000000.0,
            action=SystemAction.TASK_CREATED,
            worker_name="user",
            detail="Fix the bug",
            category=LogCategory.TASK,
        )
        display = entry.display
        assert "TASK_CREATED" in display
        assert "user" in display
        assert "Fix the bug" in display

    def test_defaults(self):
        entry = SystemEntry(
            timestamp=1.0,
            action=SystemAction.CONTINUED,
            worker_name="api",
        )
        assert entry.category == LogCategory.DRONE
        assert entry.is_notification is False


class TestSystemLogFilters:
    def test_drone_entries(self):
        log = SystemLog()
        log.add(DroneAction.CONTINUED, "api", category=LogCategory.DRONE)
        log.add(SystemAction.TASK_CREATED, "user", category=LogCategory.TASK)
        log.add(DroneAction.ESCALATED, "web", category=LogCategory.DRONE)

        drone = log.drone_entries
        assert len(drone) == 2
        assert drone[0].action == SystemAction.CONTINUED
        assert drone[1].action == SystemAction.ESCALATED

    def test_notification_entries(self):
        log = SystemLog()
        log.add(DroneAction.CONTINUED, "api")  # not a notification
        log.add(
            SystemAction.WORKER_STUNG,
            "api",
            category=LogCategory.WORKER,
            is_notification=True,
        )
        log.add(
            SystemAction.TASK_FAILED,
            "user",
            category=LogCategory.TASK,
            is_notification=True,
        )

        notifs = log.notification_entries
        assert len(notifs) == 2
        assert notifs[0].action == SystemAction.WORKER_STUNG
        assert notifs[1].action == SystemAction.TASK_FAILED

    def test_drone_action_auto_converts(self):
        """DroneAction should be automatically converted to SystemAction."""
        log = SystemLog()
        entry = log.add(DroneAction.CONTINUED, "api")
        assert isinstance(entry.action, SystemAction)
        assert entry.action == SystemAction.CONTINUED
        assert entry.category == LogCategory.DRONE

    def test_system_action_default_category(self):
        """SystemAction entries default to SYSTEM category."""
        log = SystemLog()
        entry = log.add(SystemAction.CONFIG_CHANGED, "system")
        assert entry.category == LogCategory.SYSTEM


class TestMetadata:
    def test_add_with_metadata(self):
        """Entries should accept and store metadata."""
        log = SystemLog()
        entry = log.add(
            SystemAction.QUEEN_ESCALATION,
            "api",
            "analyzed: continue (conf=90%)",
            category=LogCategory.QUEEN,
            metadata={"queen_action": "continue", "confidence": 0.9, "duration_s": 1.2},
        )
        assert entry.metadata["queen_action"] == "continue"
        assert entry.metadata["confidence"] == 0.9
        assert entry.metadata["duration_s"] == 1.2

    def test_default_metadata_is_empty(self):
        """Entries without metadata should default to empty dict."""
        log = SystemLog()
        entry = log.add(DroneAction.CONTINUED, "api")
        assert entry.metadata == {}

    def test_metadata_round_trips_through_jsonl(self, tmp_path):
        """Metadata should persist to JSONL and reload correctly."""
        log_file = tmp_path / "system.jsonl"
        log = SystemLog(log_file=log_file)
        log.add(
            SystemAction.QUEEN_COMPLETION,
            "api",
            "completion: done=True conf=85%",
            category=LogCategory.QUEEN,
            metadata={"done": True, "confidence": 0.85, "task_id": "t1"},
        )

        log2 = SystemLog(log_file=log_file)
        assert len(log2.entries) == 1
        assert log2.entries[0].metadata["done"] is True
        assert log2.entries[0].metadata["confidence"] == 0.85
        assert log2.entries[0].metadata["task_id"] == "t1"

    def test_backward_compat_missing_metadata(self, tmp_path):
        """JSONL entries without metadata field should default to empty dict."""
        log_file = tmp_path / "system.jsonl"
        log_file.write_text(
            '{"timestamp": 1.0, "action": "CONTINUED", "worker_name": "api", "detail": "test"}\n'
        )
        log = SystemLog(log_file=log_file)
        assert log.entries[0].metadata == {}

    def test_empty_metadata_not_serialized(self, tmp_path):
        """Entries with no metadata should not write metadata key to JSONL."""
        import json

        log_file = tmp_path / "system.jsonl"
        log = SystemLog(log_file=log_file)
        log.add(DroneAction.CONTINUED, "api", "test")

        line = json.loads(log_file.read_text().strip())
        assert "metadata" not in line


class TestNewDroneActionEnums:
    def test_new_actions_exist(self):
        """New DroneAction enum values should be defined."""
        assert DroneAction.AUTO_ASSIGNED.value == "AUTO_ASSIGNED"
        assert DroneAction.PROPOSED_ASSIGNMENT.value == "PROPOSED_ASSIGNMENT"
        assert DroneAction.PROPOSED_COMPLETION.value == "PROPOSED_COMPLETION"
        assert DroneAction.PROPOSED_MESSAGE.value == "PROPOSED_MESSAGE"
        assert DroneAction.QUEEN_CONTINUED.value == "QUEEN_CONTINUED"
        assert DroneAction.QUEEN_PROPOSED_DONE.value == "QUEEN_PROPOSED_DONE"

    def test_new_system_actions_exist(self):
        """New SystemAction enum values should mirror DroneAction additions."""
        assert SystemAction.AUTO_ASSIGNED.value == "AUTO_ASSIGNED"
        assert SystemAction.PROPOSED_ASSIGNMENT.value == "PROPOSED_ASSIGNMENT"
        assert SystemAction.PROPOSED_COMPLETION.value == "PROPOSED_COMPLETION"
        assert SystemAction.PROPOSED_MESSAGE.value == "PROPOSED_MESSAGE"
        assert SystemAction.QUEEN_CONTINUED.value == "QUEEN_CONTINUED"
        assert SystemAction.QUEEN_PROPOSED_DONE.value == "QUEEN_PROPOSED_DONE"

    def test_new_drone_actions_convert_to_system(self):
        """New DroneAction values should auto-convert to SystemAction."""
        log = SystemLog()
        entry = log.add(DroneAction.AUTO_ASSIGNED, "api", "auto-assigned: Fix bug")
        assert entry.action == SystemAction.AUTO_ASSIGNED

    def test_new_actions_persist(self, tmp_path):
        """New action types should round-trip through JSONL."""
        log_file = tmp_path / "system.jsonl"
        log = SystemLog(log_file=log_file)
        log.add(DroneAction.PROPOSED_COMPLETION, "api", "task appears done")
        log.add(DroneAction.QUEEN_CONTINUED, "web", "Queen: needs nudge")

        log2 = SystemLog(log_file=log_file)
        assert log2.entries[0].action == SystemAction.PROPOSED_COMPLETION
        assert log2.entries[1].action == SystemAction.QUEEN_CONTINUED


class TestDroneLogAlias:
    def test_alias(self):
        """DroneLog should be an alias for SystemLog."""
        assert DroneLog is SystemLog
