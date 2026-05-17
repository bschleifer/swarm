"""Render ACTIVE playbooks into a worker's ``.claude/skills/`` dir.

Phase 3 propagation. Each in-scope ACTIVE playbook becomes a
``pb-<name>/SKILL.md`` Claude Code Skill so the worker's model can
discover it by description match — the same delivery mechanism the
bundled swarm-coordination skills use (``hooks.install``).

PROVIDER GATING is the caller's job: the daemon only invokes this for
Claude workers (``.claude/skills/`` is Claude-specific). Non-Claude
workers still reach playbooks via the provider-neutral
``swarm_get_playbooks`` MCP tool — no native install.

DISTINCT from the bundled static skills and the v5 ``skills`` registry
(see ``docs/specs/playbook-synthesis-loop.md``).
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import TYPE_CHECKING

from swarm.logging import get_logger
from swarm.playbooks.models import PlaybookStatus

if TYPE_CHECKING:
    from swarm.db.playbook_store import PlaybookStore

_log = get_logger("playbooks.installer")

# Generated playbook skills are namespaced so they never collide with the
# bundled static skills and can be wiped/regenerated wholesale each run.
_PB_PREFIX = "pb-"


def _in_scope(scope: str, *, worker_name: str, repo: str) -> bool:
    return scope in ("global", f"worker:{worker_name}", f"project:{repo}")


def _skill_md(name: str, *, title: str, trigger: str, body: str) -> str:
    # Description drives Claude Code skill discovery — lead with the
    # trigger so the model reaches for it in the right situations.
    desc = (trigger or title or name).replace("\n", " ").strip()[:300]
    return (
        f"---\nname: {_PB_PREFIX}{name}\n"
        f"description: {desc} (swarm playbook — vetted from past successful work)\n"
        f"---\n\n# {title or name}\n\n{body}\n"
    )


def install_worker_playbooks(
    worker_path: Path, store: PlaybookStore, *, worker_name: str
) -> int:
    """(Re)render ACTIVE in-scope playbooks for one worker. Idempotent.

    Wipes any prior ``pb-*`` skill dirs first so retired/demoted
    playbooks disappear, then writes the current active set. Logs (never
    raises) on per-playbook failure so one bad row can't block a worker.
    Returns the number of playbooks written.
    """
    skills_dir = worker_path / ".claude" / "skills"
    try:
        skills_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        _log.warning("cannot create %s — skipping playbook install", skills_dir)
        return 0

    # Wipe stale generated playbook skills (deletions/demotions propagate).
    for old in skills_dir.glob(f"{_PB_PREFIX}*"):
        if old.is_dir():
            shutil.rmtree(old, ignore_errors=True)

    repo = worker_path.name
    written = 0
    for pb in store.list(status=PlaybookStatus.ACTIVE, limit=500):
        if not _in_scope(pb.scope, worker_name=worker_name, repo=repo):
            continue
        try:
            dst = skills_dir / f"{_PB_PREFIX}{pb.name}"
            dst.mkdir(parents=True, exist_ok=True)
            (dst / "SKILL.md").write_text(
                _skill_md(pb.name, title=pb.title, trigger=pb.trigger, body=pb.body),
                encoding="utf-8",
            )
            written += 1
        except OSError:
            _log.warning("failed to write playbook skill %s", pb.name, exc_info=True)
    return written
