"""Workflow templates — type-specific instructions embedded in task assignments.

Task types with a matching Claude Code skill (global ~/.claude/commands/) use
a skill invocation instead of inline workflow steps.  The skill handles the
full pipeline (tooling detection, planning gates, TDD loops, commit offers).
"""

from __future__ import annotations

from swarm.tasks.task import TaskType

# Maps task types that have a dedicated Claude Code skill.
# Value is the slash-command name (e.g. "/fix-and-ship").
SKILL_COMMANDS: dict[TaskType, str] = {
    TaskType.BUG: "/fix-and-ship",
    TaskType.FEATURE: "/feature",
    TaskType.VERIFY: "/verify",
}

# Fallback inline templates for types without a skill.
WORKFLOW_TEMPLATES: dict[TaskType, str] = {
    TaskType.CHORE: """\
## Workflow: General Task
1. Complete the task as described
2. Validate your changes (run tests if applicable)
3. Commit when done""",
}


def get_skill_command(task_type: TaskType) -> str | None:
    """Return the slash-command for *task_type*, or ``None`` if it has no skill."""
    return SKILL_COMMANDS.get(task_type)


def get_workflow_instructions(task_type: TaskType) -> str:
    """Return inline workflow instructions for the given task type.

    Only returns text for types that do NOT have a dedicated skill.
    """
    return WORKFLOW_TEMPLATES.get(task_type, WORKFLOW_TEMPLATES[TaskType.CHORE])
