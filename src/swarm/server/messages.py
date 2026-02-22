"""Task message builder — pure functions for formatting task messages to workers."""

from __future__ import annotations

from swarm.tasks.task import SwarmTask


def task_detail_parts(task: SwarmTask) -> list[str]:
    """Collect title, description, and tags into a parts list (no attachments)."""
    parts: list[str] = [f"#{task.number}: {task.title}" if task.number else task.title]
    if task.description:
        parts.append(task.description)
    if task.tags:
        parts.append(f"Tags: {', '.join(task.tags)}")
    return parts


def attachment_lines(task: SwarmTask) -> str:
    """Format attachment paths as separate lines for the worker."""
    if not task.attachments:
        return ""
    lines = ["\nAttachments (read these files for context):"]
    for a in task.attachments:
        lines.append(f"  - {a}")
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

    skill = get_skill_command(task.task_type) if supports_slash_commands else None
    if skill:
        desc = " ".join(task_detail_parts(task))
        msg = f'{skill} "{desc}"'
        atts = attachment_lines(task)
        if atts:
            msg = f"{msg}{atts}"
        return msg

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
    workflow = get_workflow_instructions(task.task_type)
    if workflow:
        parts.append(f"\n{workflow}")
    return "\n".join(parts)
