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

    def test_image_attachment_has_read_tool_hint(self):
        task = SwarmTask(title="Fix bug", attachments=["/tmp/screenshot.png"])
        result = attachment_lines(task)
        assert "IMAGE:" in result
        assert "Read tool to view" in result

    def test_text_attachment_has_read_hint(self):
        task = SwarmTask(title="Fix bug", attachments=["/tmp/notes.txt"])
        result = attachment_lines(task)
        assert "TEXT:" in result
        assert "Read tool" in result
        assert "IMAGE:" not in result

    def test_mixed_attachments(self):
        task = SwarmTask(
            title="Fix bug",
            attachments=["/tmp/screenshot.png", "/tmp/spec.md", "/tmp/photo.jpg"],
        )
        result = attachment_lines(task)
        lines = result.strip().splitlines()
        # header + 3 attachment lines
        assert len(lines) == 4
        assert "IMAGE:" in lines[1]  # .png
        assert "TEXT:" in lines[2]  # .md
        assert "IMAGE:" in lines[3]  # .jpg

    def test_docx_attachment_gets_pandoc_hint(self):
        """.docx files need conversion — Read returns binary garbage."""
        task = SwarmTask(title="Review change request", attachments=["/tmp/spec.docx"])
        result = attachment_lines(task)
        assert "WORD DOC:" in result
        # Worker should be pointed at a concrete extraction command.
        assert "pandoc" in result or "docx2txt" in result

    def test_pdf_attachment_gets_pdftotext_hint(self):
        task = SwarmTask(title="Review pdf", attachments=["/tmp/contract.pdf"])
        result = attachment_lines(task)
        assert "PDF:" in result
        assert "pdftotext" in result or "pypdf" in result

    def test_xlsx_attachment_gets_openpyxl_hint(self):
        task = SwarmTask(title="Review sheet", attachments=["/tmp/data.xlsx"])
        result = attachment_lines(task)
        assert "SPREADSHEET:" in result
        assert "openpyxl" in result


class TestSourceMetadata:
    """Tests for jira_key and source metadata in worker prompts."""

    def test_jira_key_in_detail_parts(self):
        task = SwarmTask(title="Fix bug", jira_key="PROJ-123", number=5)
        parts = task_detail_parts(task)
        assert any("PROJ-123" in p for p in parts), "jira_key should appear in detail parts"

    def test_jira_key_in_skill_message(self):
        task = SwarmTask(
            title="Fix login",
            task_type=TaskType.BUG,
            jira_key="HUB-42",
            number=10,
        )
        msg = build_task_message(task, supports_slash_commands=True)
        assert "HUB-42" in msg, "jira_key should appear in skill-based message"

    def test_jira_key_in_inline_message(self):
        task = SwarmTask(
            title="Clean up logs",
            task_type=TaskType.CHORE,
            jira_key="OPS-99",
            number=3,
        )
        msg = build_task_message(task, supports_slash_commands=True)
        assert "OPS-99" in msg, "jira_key should appear in inline workflow message"

    def test_source_email_id_in_detail_parts(self):
        task = SwarmTask(title="Reply to user", source_email_id="msg-abc-123")
        parts = task_detail_parts(task)
        assert any("msg-abc-123" in p for p in parts), (
            "source_email_id should appear in detail parts"
        )

    def test_no_source_metadata_when_empty(self):
        task = SwarmTask(title="Fix bug")
        parts = task_detail_parts(task)
        assert not any("Jira" in p or "Source" in p for p in parts), (
            "no source metadata line when fields are empty"
        )


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
