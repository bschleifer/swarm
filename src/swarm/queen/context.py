"""Hive context — aggregated state for Queen coordination decisions."""

from __future__ import annotations

from __future__ import annotations

from typing import TYPE_CHECKING

from swarm.drones.log import DroneEntry, DroneLog
from swarm.worker.worker import Worker, WorkerState

if TYPE_CHECKING:
    from swarm.tasks.board import TaskBoard


def build_hive_context(
    workers: list[Worker],
    worker_outputs: dict[str, str] | None = None,
    drone_log: DroneLog | None = None,
    task_board: TaskBoard | None = None,
    max_output_lines: int = 20,
    max_log_entries: int = 15,
) -> str:
    """Build a compressed context string describing the entire hive.

    This gives the Queen awareness of all workers, not just the one
    being analyzed.  Used for task decomposition, conflict detection,
    and pipeline orchestration.
    """
    outputs = worker_outputs or {}
    sections: list[str] = []

    # -- Worker summary table --
    lines = ["## Hive Workers"]
    for w in workers:
        dur = f"{w.state_duration:.0f}s"
        revives = f" (revived {w.revive_count}x)" if w.revive_count else ""
        lines.append(f"- {w.name}: {w.state.display} for {dur}{revives}  path={w.path}")
    sections.append("\n".join(lines))

    # -- Recent output per worker (truncated) --
    if outputs:
        out_lines = ["## Recent Worker Output"]
        for name, content in outputs.items():
            trimmed = _tail(content, max_output_lines)
            out_lines.append(f"### {name}")
            out_lines.append(f"```\n{trimmed}\n```")
        sections.append("\n".join(out_lines))

    # -- Recent drone log --
    if drone_log and drone_log.entries:
        entries = drone_log.entries[-max_log_entries:]
        log_lines = ["## Recent Auto-Pilot Actions"]
        for e in entries:
            log_lines.append(f"- [{e.formatted_time}] {e.action.value} {e.worker_name}: {e.detail}")
        sections.append("\n".join(log_lines))

    # -- Task board --
    if task_board is not None:
        sections.append(_task_board_section(task_board))

    # -- Aggregate stats --
    stats = _hive_stats(workers)
    sections.append(stats)

    return "\n\n".join(sections)


def _tail(text: str, n: int) -> str:
    """Return the last N lines of text."""
    lines = text.strip().splitlines()
    if len(lines) <= n:
        return text.strip()
    return "\n".join(lines[-n:])


def _task_board_section(board: TaskBoard) -> str:
    """Render the task board for Queen context."""
    from swarm.tasks.task import TaskStatus

    lines = ["## Task Board"]
    lines.append(board.summary())

    available = board.available_tasks
    if available:
        lines.append("\n### Available (unassigned)")
        for t in available:
            lines.append(f"- [{t.id}] {t.title} (priority={t.priority.value})")
            if t.description:
                lines.append(f"  {t.description[:120]}")

    active = board.active_tasks
    if active:
        lines.append("\n### Active (assigned/in-progress)")
        for t in active:
            lines.append(f"- [{t.id}] {t.title} → {t.assigned_worker} ({t.status.value})")

    return "\n".join(lines)


def _hive_stats(workers: list[Worker]) -> str:
    """Quick aggregate stats."""
    total = len(workers)
    buzzing = sum(1 for w in workers if w.state == WorkerState.BUZZING)
    resting = sum(1 for w in workers if w.state == WorkerState.RESTING)
    stung = sum(1 for w in workers if w.state == WorkerState.STUNG)
    return (
        f"## Hive Stats\n"
        f"- Total workers: {total}\n"
        f"- Buzzing (working): {buzzing}\n"
        f"- Resting (idle): {resting}\n"
        f"- Stung (exited): {stung}"
    )
