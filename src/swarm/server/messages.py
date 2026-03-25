"""Task message builder — pure functions for formatting task messages to workers."""

from __future__ import annotations

from pathlib import Path

from swarm.tasks.task import SwarmTask

_IMAGE_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp"})

_COMPLETE_DIR = "~/.swarm/complete-task"

_COMPLETION_INSTRUCTIONS = """\

When done, mark the task complete by writing:
  ~/.swarm/complete-task/{task_id}.json
with content: {{"task_id": "{task_id}", "resolution": "<brief summary of what you did>"}}"""


def task_detail_parts(task: SwarmTask) -> list[str]:
    """Collect title, description, tags, and source metadata into a parts list."""
    parts: list[str] = [f"#{task.number}: {task.title}" if task.number else task.title]
    if task.description:
        parts.append(task.description)
    if task.tags:
        parts.append(f"Tags: {', '.join(task.tags)}")
    # Source metadata — lets the worker know the task's external origin
    source_parts: list[str] = []
    if task.jira_key:
        source_parts.append(f"Jira: {task.jira_key}")
    if task.source_email_id:
        source_parts.append(f"Email: {task.source_email_id}")
    if source_parts:
        parts.append(f"Source: {', '.join(source_parts)}")
    return parts


def attachment_lines(task: SwarmTask) -> str:
    """Format attachment paths as separate lines for the worker.

    Image files get explicit Read-tool instructions so Claude's multimodal
    support is actually invoked (plain paths are not auto-read as images).
    """
    if not task.attachments:
        return ""
    lines = ["\nAttachments:"]
    for a in task.attachments:
        ext = Path(a).suffix.lower()
        if ext in _IMAGE_EXTENSIONS:
            lines.append(f"  - IMAGE: {a} — Use the Read tool to view this image file")
        else:
            lines.append(f"  - {a} — Read this file for context")
    return "\n".join(lines)


def build_task_message(task: SwarmTask, *, supports_slash_commands: bool = True) -> str:
    """Build a message string describing a task for a worker.

    If the task type has a dedicated Claude Code skill (e.g. ``/feature``),
    the message is formatted as a skill invocation so the worker's Claude
    session handles the full pipeline.  Otherwise, inline workflow steps
    are appended as before.

    When *supports_slash_commands* is False (non-Claude providers), skill
    invocations are skipped and inline workflow instructions are used instead.

    Attachments are always listed on separate lines (never squished into
    the skill command's quoted argument) so the worker can see and read them.
    """
    from swarm.tasks.workflows import get_skill_command, get_workflow_instructions

    completion = _COMPLETION_INSTRUCTIONS.format(task_id=task.id)

    skill = get_skill_command(task.task_type) if supports_slash_commands else None
    if skill:
        desc = " ".join(task_detail_parts(task))
        msg = f'{skill} "{desc}"'
        atts = attachment_lines(task)
        if atts:
            msg = f"{msg}{atts}"
        return msg + completion

    # Fallback: inline workflow instructions (CHORE, unknown types).
    prefix = f"Task #{task.number}: " if task.number else "Task: "
    parts = [f"{prefix}{task.title}"]
    if task.description:
        parts.append(f"\n{task.description}")
    atts = attachment_lines(task)
    if atts:
        parts.append(atts)
    if task.tags:
        parts.append(f"\nTags: {', '.join(task.tags)}")
    # Source metadata (inline path)
    source_parts: list[str] = []
    if task.jira_key:
        source_parts.append(f"Jira: {task.jira_key}")
    if task.source_email_id:
        source_parts.append(f"Email: {task.source_email_id}")
    if source_parts:
        parts.append(f"\nSource: {', '.join(source_parts)}")
    workflow = get_workflow_instructions(task.task_type)
    if workflow:
        parts.append(f"\n{workflow}")
    parts.append(completion)
    return "\n".join(parts)
