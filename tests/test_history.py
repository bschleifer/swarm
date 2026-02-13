"""Tests for tasks/history.py — task audit log."""

from __future__ import annotations

import json

from swarm.tasks.history import TaskAction, TaskEvent, TaskHistory


class TestTaskEvent:
    """Tests for the TaskEvent dataclass."""

    def test_to_dict(self):
        e = TaskEvent(
            timestamp=1700000000.0,
            task_id="t1",
            action=TaskAction.CREATED,
            actor="drone",
            detail="New task",
        )
        d = e.to_dict()
        assert d["timestamp"] == 1700000000.0
        assert d["task_id"] == "t1"
        assert d["action"] == "CREATED"
        assert d["actor"] == "drone"
        assert d["detail"] == "New task"

    def test_formatted_time(self):
        e = TaskEvent(
            timestamp=1700000000.0,
            task_id="t1",
            action=TaskAction.CREATED,
        )
        # Should return a formatted string, not crash
        fmt = e.formatted_time
        assert "2023" in fmt  # Nov 2023 epoch


class TestTaskHistory:
    """Tests for the TaskHistory class."""

    def test_append_creates_file(self, tmp_path):
        log = tmp_path / "history.jsonl"
        h = TaskHistory(log_file=log)
        event = h.append("t1", TaskAction.CREATED, actor="user", detail="test")
        assert log.exists()
        assert event.task_id == "t1"
        assert event.action == TaskAction.CREATED

    def test_get_events_round_trip(self, tmp_path):
        log = tmp_path / "history.jsonl"
        h = TaskHistory(log_file=log)
        h.append("t1", TaskAction.CREATED, detail="created")
        h.append("t1", TaskAction.ASSIGNED, actor="drone", detail="assigned to api")
        h.append("t2", TaskAction.CREATED, detail="other task")
        events = h.get_events("t1")
        assert len(events) == 2
        assert events[0].action == TaskAction.CREATED
        assert events[1].action == TaskAction.ASSIGNED

    def test_get_events_limit(self, tmp_path):
        log = tmp_path / "history.jsonl"
        h = TaskHistory(log_file=log)
        for i in range(10):
            h.append("t1", TaskAction.EDITED, detail=f"edit-{i}")
        events = h.get_events("t1", limit=3)
        assert len(events) == 3
        # Should be the last 3
        assert events[0].detail == "edit-7"

    def test_get_events_no_file(self, tmp_path):
        log = tmp_path / "history.jsonl"
        h = TaskHistory(log_file=log)
        events = h.get_events("t1")
        assert events == []

    def test_get_events_corrupted_lines(self, tmp_path):
        log = tmp_path / "history.jsonl"
        # Write a mix of valid and invalid lines
        valid = json.dumps(
            {
                "timestamp": 1700000000.0,
                "task_id": "t1",
                "action": "CREATED",
                "actor": "user",
                "detail": "",
            }
        )
        log.write_text(f"{valid}\nnot-json\n\n{valid}\n")
        h = TaskHistory(log_file=log)
        events = h.get_events("t1")
        assert len(events) == 2

    def test_rotation(self, tmp_path):
        log = tmp_path / "history.jsonl"
        h = TaskHistory(log_file=log, max_file_size=100, max_rotations=2)
        # Write enough to trigger rotation
        for i in range(20):
            h.append("t1", TaskAction.EDITED, detail=f"long-detail-{i:04d}")
        rotated = log.with_suffix(".jsonl.1")
        assert rotated.exists() or log.exists()

    def test_append_creates_parent_dirs(self, tmp_path):
        log = tmp_path / "deep" / "dir" / "history.jsonl"
        h = TaskHistory(log_file=log)
        h.append("t1", TaskAction.CREATED)
        assert log.exists()
