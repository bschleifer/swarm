"""Interactive Queen PTY runtime.

The Queen is the swarm's conversational coordinator.  Unlike a regular
worker, she is:

* A singleton per swarm instance.
* Always-on (her "idle" state is active conversation readiness,
  not a candidate for SLEEPING).
* Exempt from task assignment and operator "continue all" / "send all"
  broadcasts.
* Spawned automatically at daemon startup when
  ``config.queen.enabled`` is true.

This module owns the startup-time spawn and the reattach-after-reload
path.  It delegates the actual PTY management to the same pool the
regular workers use.

See `docs/specs/interactive-queen.md` §4.1 for the full architecture.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from swarm.config.models import WorkerConfig
from swarm.logging import get_logger
from swarm.worker.worker import QUEEN_WORKER_NAME, WORKER_KIND_QUEEN, Worker

if TYPE_CHECKING:
    from swarm.config.models import HiveConfig
    from swarm.pty.provider import WorkerProcessProvider


_log = get_logger("queen.runtime")

# Queen's working directory — dedicated so Claude's `--continue` resumes
# her session without clashing with any operator shell history.
QUEEN_WORK_DIR = Path.home() / ".swarm" / "queen" / "workdir"

# First-pass system prompt for the interactive Queen.
# Written to `workdir/CLAUDE.md` on startup when no prior copy exists,
# so the operator can edit it in place and the change picks up on
# the Queen's next session (or next daemon reload).
#
# See docs/specs/interactive-queen.md §6.
QUEEN_SYSTEM_PROMPT = """\
# You are the Queen

You are the swarm's conversational coordinator — the operator's
central point of contact for every worker under your command.

## Hierarchy

- **Operator (human)** is above you. Their instructions override yours.
- **Drones** sit below you; they handle routine fast-path decisions
  and you supervise them.
- **Workers** sit below you; they do the actual coding work.

You do not defy the operator. You direct everything below.

## Role: pure coordinator

You **never** edit files, run shell commands, or take hands-on code
work yourself. Your job is to understand, decide, and direct.
Workers are the hands; you are the brain.

When the operator asks you to do something that needs execution,
delegate it to the right worker via messaging or task assignment.

## Conversation shape

Every response lands in a **thread** — a partitioned view of our
shared conversation. Start new threads for new topics via
`queen_post_thread`. Mark threads resolved via `queen_update_thread`
when the outcome is reached.

Stay terse. The operator reads the diff, not the narration.

## Proactive, with restraint

Surface things the operator should know — stuck workers, proposals
ready to review, anomalies you've spotted. Do not flood.

Use the existing Queen confidence threshold to decide:
- High confidence → act, record, then inform.
- Low confidence → post a thread and wait for the operator.

## Tools you have

Introspection (read-only):
- `queen_view_worker_state` — state, task, PTY tail for any worker
- `queen_view_task_board` — open and recent tasks
- `queen_view_messages` — inter-worker message log
- `queen_view_buzz_log` — system activity feed
- `queen_view_drone_actions` — what the drones are deciding
- `queen_query_learnings` — operator corrections from past decisions

Always call `queen_query_learnings` **before** making a judgement
call similar to one you've seen before.

## Subscription-aware

You share a 5-hour Claude rate-limit window with workers. If usage
approaches the limit, pause rather than burning through — the
operator's coding session depends on it.

## Focus context

The dashboard tells you which worker the operator is currently
viewing. Bias toward that worker when they ask an ambiguous
question ('why is this stuck?'), but don't assume it's exclusive —
they may ask about anything.

## Voice

Terse. Swarm-aware. Operator-peer (not servile, not bossy).
Quote specific file paths, task numbers, worker names when you have
them. No emoji unless the operator uses them first.
"""


def _ensure_queen_claude_md(workdir: Path) -> None:
    """Write the first-pass system prompt to Queen's workdir if absent.

    Claude Code reads `CLAUDE.md` at session start, so landing it in
    the Queen's working directory makes the prompt load automatically
    without needing a CLI flag.  Operator edits to the file survive
    restarts since we only write when the file doesn't exist.
    """
    target = workdir / "CLAUDE.md"
    if target.exists():
        return
    workdir.mkdir(parents=True, exist_ok=True)
    target.write_text(QUEEN_SYSTEM_PROMPT)
    _log.info("wrote queen CLAUDE.md to %s", target)


def queen_worker_config(config: HiveConfig) -> WorkerConfig:
    """Build a WorkerConfig for the Queen.

    The config is synthetic (never stored in the ``workers`` DB table)
    — the Queen is a runtime singleton, not an operator-configurable
    worker.
    """
    return WorkerConfig(
        name=QUEEN_WORKER_NAME,
        path=str(QUEEN_WORK_DIR),
        description="Swarm coordinator — always-on conversational command.",
        provider=config.provider or "claude",
        identity="queen",
    )


def find_queen(workers: list[Worker]) -> Worker | None:
    """Return the queen Worker from the live list, or None."""
    for w in workers:
        if w.is_queen:
            return w
    return None


async def ensure_queen_running(
    pool: WorkerProcessProvider,
    workers: list[Worker],
    config: HiveConfig,
) -> Worker | None:
    """Spawn the Queen if she isn't already in the worker list.

    Call this once at daemon startup, after ``discover()`` has
    reattached to any persisted PTYs.  If discover() already found a
    running Queen PTY she'll be in ``workers`` with kind="queen";
    we're a no-op in that case.  Otherwise we spawn a fresh Queen
    process.

    Returns the Queen Worker (existing or newly spawned), or None
    when ``config.queen.enabled`` is false.
    """
    if not config.queen.enabled:
        _log.info("queen disabled in config — not spawning")
        return None

    existing = find_queen(workers)
    if existing is not None:
        _log.info("queen already running (pid=%s)", _pid_of(existing))
        return existing

    # Fresh spawn.  We want --continue so the Queen's prior Claude
    # session resumes; her dedicated workdir makes that unambiguous.
    QUEEN_WORK_DIR.mkdir(parents=True, exist_ok=True)
    _ensure_queen_claude_md(QUEEN_WORK_DIR)

    # Late import to break module-load cycle (manager imports worker).
    from swarm.worker.manager import add_worker_live

    wc = queen_worker_config(config)
    worker = await add_worker_live(
        pool,
        wc,
        workers,
        auto_start=True,
        default_provider=config.provider or "claude",
        kind=WORKER_KIND_QUEEN,
        resume=True,
    )
    _log.info("spawned queen (pid=%s)", _pid_of(worker))
    return worker


def _pid_of(worker: Worker) -> str:
    proc = worker.process
    return str(getattr(proc, "pid", "?")) if proc else "?"
