"""Workflow templates — type-specific instructions embedded in task assignments.

Task types with a matching Claude Code skill (global ~/.claude/commands/) use
a skill invocation instead of inline workflow steps.  The skill handles the
full pipeline (tooling detection, planning gates, TDD loops, commit offers).

The ``workflows:`` section in ``swarm.yaml`` can override or extend these
defaults.  For example::

    workflows:
      bug: /fix-and-ship
      feature: /feature
      verify: /verify
      chore: /my-custom-chore-skill
"""

from __future__ import annotations

from swarm.tasks.task import TaskType

# Built-in defaults — overridable via config ``workflows:`` section.
_DEFAULT_SKILL_COMMANDS: dict[TaskType, str] = {
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

# Resolved map — starts as defaults, merged with config at init time.
SKILL_COMMANDS: dict[TaskType, str] = dict(_DEFAULT_SKILL_COMMANDS)


def apply_config_overrides(overrides: dict[str, str]) -> None:
    """Merge ``workflows:`` config section into the skill commands map.

    Called once at daemon startup.  Keys are TaskType value strings
    (``bug``, ``feature``, ``verify``, ``chore``).  An empty/null value
    removes the skill for that type (falls back to inline template).
    """
    type_lookup = {t.value: t for t in TaskType}
    for key, cmd in overrides.items():
        task_type = type_lookup.get(key.lower())
        if task_type is None:
            continue
        if cmd:
            SKILL_COMMANDS[task_type] = cmd
        else:
            SKILL_COMMANDS.pop(task_type, None)


def get_skill_command(task_type: TaskType) -> str | None:
    """Return the slash-command for *task_type*, or ``None`` if it has no skill."""
    return SKILL_COMMANDS.get(task_type)


def get_workflow_instructions(task_type: TaskType) -> str:
    """Return inline workflow instructions for the given task type.

    Only returns text for types that do NOT have a dedicated skill.
    """
    return WORKFLOW_TEMPLATES.get(task_type, WORKFLOW_TEMPLATES[TaskType.CHORE])
