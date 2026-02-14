"""Hive context — aggregated state for Queen coordination decisions."""

from __future__ import annotations

from typing import TYPE_CHECKING

from swarm.drones.log import DroneLog
from swarm.worker.worker import Worker, format_duration, worker_state_counts

if TYPE_CHECKING:
    from swarm.config import DroneApprovalRule
    from swarm.tasks.board import TaskBoard


def build_hive_context(
    workers: list[Worker],
    worker_outputs: dict[str, str] | None = None,
    drone_log: DroneLog | None = None,
    task_board: TaskBoard | None = None,
    worker_descriptions: dict[str, str] | None = None,
    approval_rules: list[DroneApprovalRule] | None = None,
    max_output_lines: int = 20,
    max_log_entries: int = 15,
) -> str:
    """Build a compressed context string describing the entire hive.

    This gives the Queen awareness of all workers, not just the one
    being analyzed.  Used for task decomposition, conflict detection,
    and pipeline orchestration.
    """
    outputs = worker_outputs or {}
    descriptions = worker_descriptions or {}
    sections: list[str] = []

    # -- Worker summary table --
    lines = ["## Hive Workers"]
    for w in workers:
        dur = format_duration(w.state_duration)
        revives = f" (revived {w.revive_count}x)" if w.revive_count else ""
        desc = descriptions.get(w.name, "")
        desc_suffix = f" — {desc}" if desc else ""
        lines.append(
            f"- {w.name}: {w.display_state.display} for {dur}{revives}  path={w.path}{desc_suffix}"
        )
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

    # -- Drone approval rules --
    if approval_rules:
        rule_lines = ["## Drone Approval Rules"]
        rule_lines.append(
            "Drones auto-handle choice menus using these rules (first match wins)."
            " Escalated choices are sent back for Queen/operator review."
        )
        for r in approval_rules:
            rule_lines.append(f"- pattern: `{r.pattern}` → {r.action}")
        sections.append("\n".join(rule_lines))

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


def _completed_tasks_section(board: TaskBoard) -> list[str]:
    """Render recently completed tasks (capped to 20) so Queen doesn't re-assign them."""
    from swarm.tasks.task import TaskStatus

    completed = [t for t in board.all_tasks if t.status == TaskStatus.COMPLETED]
    if not completed:
        return []
    capped = completed[-20:]
    header = "### Completed (do NOT re-assign these)"
    if len(completed) > 20:
        header += f" — showing last 20 of {len(completed)}"
    lines = [f"\n{header}"]
    for t in capped:
        res = f" — {t.resolution}" if t.resolution else ""
        lines.append(f"- [{t.id}] {t.title}{res}")
    return lines


def _task_board_section(board: TaskBoard) -> str:
    """Render the task board for Queen context."""

    lines = ["## Task Board"]
    lines.append(board.summary())

    available = board.available_tasks
    if available:
        lines.append("\n### Available (unassigned)")
        for t in available:
            lines.append(
                f"- [{t.id}] {t.title} (priority={t.priority.value}, type={t.task_type.value})"
            )
            if t.description:
                lines.append(f"  {t.description}")
            if t.attachments:
                fnames = [a.rsplit("/", 1)[-1] for a in t.attachments]
                lines.append(f"  Attachments: {', '.join(fnames)}")
            if t.tags:
                lines.append(f"  Tags: {', '.join(t.tags)}")

    active = board.active_tasks
    if active:
        lines.append("\n### Active (assigned/in-progress)")
        for t in active:
            lines.append(
                f"- [{t.id}] {t.title} → {t.assigned_worker}"
                f" ({t.status.value}, type={t.task_type.value})"
            )

    lines.extend(_completed_tasks_section(board))
    return "\n".join(lines)


def _hive_stats(workers: list[Worker]) -> str:
    """Quick aggregate stats."""
    counts = worker_state_counts(workers)
    return (
        f"## Hive Stats\n"
        f"- Total workers: {counts['total']}\n"
        f"- Buzzing (working): {counts['buzzing']}\n"
        f"- Resting (idle): {counts['resting']}\n"
        f"- Sleeping (idle > 5m): {counts['sleeping']}\n"
        f"- Stung (exited): {counts['stung']}"
    )
