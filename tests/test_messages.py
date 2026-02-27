"""Tests for swarm.server.messages — task message building."""

from __future__ import annotations

from swarm.server.messages import attachment_lines, build_task_message, task_detail_parts
from swarm.tasks.task import SwarmTask, TaskType


class TestTaskDetailParts:
    def test_title_only(self):
        task = SwarmTask(title="Fix bug")
        parts = task_detail_parts(task)
        assert parts == ["Fix bug"]

    def test_with_number(self):
        task = SwarmTask(title="Fix bug", number=42)
        parts = task_detail_parts(task)
        assert parts == ["#42: Fix bug"]

    def test_with_description(self):
        task = SwarmTask(title="Fix bug", description="Something is broken")
        parts = task_detail_parts(task)
        assert len(parts) == 2
        assert "Something is broken" in parts

    def test_with_tags(self):
        task = SwarmTask(title="Fix bug", tags=["backend", "urgent"])
        parts = task_detail_parts(task)
        assert any("backend" in p and "urgent" in p for p in parts)

    def test_full_task(self):
        task = SwarmTask(
            title="Fix bug",
            number=7,
            description="Login fails",
            tags=["auth"],
        )
        parts = task_detail_parts(task)
        assert len(parts) == 3
        assert parts[0] == "#7: Fix bug"


class TestAttachmentLines:
    def test_no_attachments(self):
        task = SwarmTask(title="Fix bug")
        assert attachment_lines(task) == ""

    def test_with_attachments(self):
        task = SwarmTask(title="Fix bug", attachments=["/path/to/file.py", "/path/to/spec.md"])
        result = attachment_lines(task)
        assert "Attachments" in result
        assert "/path/to/file.py" in result
        assert "/path/to/spec.md" in result


class TestBuildTaskMessage:
    def test_chore_uses_inline_workflow(self):
        task = SwarmTask(title="Clean up logs", task_type=TaskType.CHORE)
        msg = build_task_message(task)
        assert "Clean up logs" in msg

    def test_bug_uses_skill_command(self):
        task = SwarmTask(title="Fix login", task_type=TaskType.BUG)
        msg = build_task_message(task, supports_slash_commands=True)
        # Bug tasks should use /fix-and-ship or similar skill
        assert "Fix login" in msg

    def test_no_slash_commands_uses_inline(self):
        task = SwarmTask(title="Fix login", task_type=TaskType.BUG)
        msg = build_task_message(task, supports_slash_commands=False)
        # Should fall back to inline format without skill prefix
        assert "Fix login" in msg

    def test_attachments_included(self):
        task = SwarmTask(
            title="Fix bug",
            task_type=TaskType.CHORE,
            attachments=["/tmp/screenshot.png"],
        )
        msg = build_task_message(task)
        assert "/tmp/screenshot.png" in msg
        assert "Attachments" in msg

    def test_numbered_task_prefix(self):
        task = SwarmTask(title="Fix bug", task_type=TaskType.CHORE, number=5)
        msg = build_task_message(task, supports_slash_commands=False)
        assert "Task #5:" in msg
