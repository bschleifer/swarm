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

At runtime the (optional) ``SkillsStore`` supersedes both the defaults and
the config overrides — giving operators a DB-backed registry to inspect
and tweak without editing config. Usage is recorded per skill so stale
entries are easy to spot.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from swarm.tasks.task import TaskType

if TYPE_CHECKING:
    from swarm.db.skills_store import SkillsStore

# Built-in defaults — overridable via config ``workflows:`` section.
_DEFAULT_SKILL_COMMANDS: dict[TaskType, str] = {
    TaskType.BUG: "/fix-and-ship",
    TaskType.FEATURE: "/feature",
    TaskType.VERIFY: "/verify",
}

# Description metadata for the default skills — used to seed the SkillsStore
# so the registry has something useful to show on first boot.
_DEFAULT_SKILL_DESCRIPTIONS: dict[str, tuple[str, list[str]]] = {
    "/fix-and-ship": ("Autonomous bug-fix pipeline: diagnose → TDD → validate → commit.", ["bug"]),
    "/feature": ("Implement a new feature with TDD and /check validation.", ["feature"]),
    "/verify": ("Read-only verification and QA pass.", ["verify"]),
}

# Fallback inline templates for types without a skill.
WORKFLOW_TEMPLATES: dict[TaskType, str] = {
    TaskType.CHORE: """\
## Workflow: General Task
1. Complete the task as described
2. Validate your changes (run tests if applicable)
3. Commit when done""",
    TaskType.CONTENT: """\
## Workflow: Content Task
1. Research and gather source material
2. Draft the content (script, article, plan)
3. Review and refine
4. Mark complete when ready for next step""",
    TaskType.REVIEW: """\
## Workflow: Review Task
1. Review the deliverable against acceptance criteria
2. Provide feedback or approve
3. Mark complete when satisfied""",
    TaskType.PUBLISH: """\
## Workflow: Publish Task
1. Prepare the content for the target platform
2. Publish or schedule publication
3. Verify the published content""",
    TaskType.INGEST: """\
## Workflow: Ingest Task
1. Connect to the data source
2. Extract and transform the data
3. Store results for downstream processing""",
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


# DB-backed registry — populated at daemon startup via ``attach_skills_store``.
# Kept at module scope so ``get_skill_command`` doesn't need to be threaded
# through every caller.
_SKILLS_STORE: SkillsStore | None = None


def attach_skills_store(store: SkillsStore) -> None:
    """Wire a ``SkillsStore`` into skill resolution.

    Seeds the store with built-in defaults (idempotent — existing rows
    are untouched) so a fresh DB starts with the canonical mapping.
    """
    global _SKILLS_STORE
    _SKILLS_STORE = store
    store.seed_defaults(_DEFAULT_SKILL_DESCRIPTIONS)


def detach_skills_store() -> None:
    """Clear the attached store (primarily for tests)."""
    global _SKILLS_STORE
    _SKILLS_STORE = None


def get_skills_store() -> SkillsStore | None:
    return _SKILLS_STORE


def _lookup_from_store(task_type: TaskType) -> str | None:
    """Return the first registered skill whose ``task_types`` includes
    *task_type*. Returns ``None`` when no store is attached or when no
    skill claims this type — callers fall back to the in-memory map.
    """
    if _SKILLS_STORE is None:
        return None
    try:
        for skill in _SKILLS_STORE.list_all():
            if task_type.value in skill.task_types:
                return skill.name
    except Exception:
        return None
    return None


def get_skill_command(task_type: TaskType) -> str | None:
    """Return the slash-command for *task_type*, or ``None`` if it has no skill.

    Resolution order: DB-backed registry → in-memory ``SKILL_COMMANDS``
    map (defaults + ``workflows:`` config overrides). On a cache hit
    from either source the call is logged as usage in the registry so
    stale/unused skills become visible.
    """
    from_store = _lookup_from_store(task_type)
    chosen = from_store or SKILL_COMMANDS.get(task_type)
    if chosen and _SKILLS_STORE is not None:
        try:
            _SKILLS_STORE.record_usage(chosen)
        except Exception:
            pass
    return chosen


def get_workflow_instructions(task_type: TaskType) -> str:
    """Return inline workflow instructions for the given task type.

    Only returns text for types that do NOT have a dedicated skill.
    """
    return WORKFLOW_TEMPLATES.get(task_type, WORKFLOW_TEMPLATES[TaskType.CHORE])
