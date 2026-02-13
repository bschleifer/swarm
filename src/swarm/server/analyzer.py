"""QueenAnalyzer — handles Queen escalation/completion analysis and hive coordination."""

from __future__ import annotations

import asyncio
import re
from typing import TYPE_CHECKING

from swarm.drones.log import DroneAction, LogCategory, SystemAction
from swarm.logging import get_logger
from swarm.tasks.proposal import AssignmentProposal, build_worker_task_info
from swarm.worker.worker import Worker, WorkerState

if TYPE_CHECKING:
    from swarm.queen.queen import Queen
    from swarm.server.daemon import SwarmDaemon
    from swarm.tasks.task import SwarmTask

_log = get_logger("server.analyzer")


class QueenAnalyzer:
    """Manages Queen analysis: escalations, completions, and hive coordination."""

    def __init__(self, queen: Queen, daemon: SwarmDaemon) -> None:
        self.queen = queen
        self._daemon = daemon
        self._inflight_escalations: set[str] = set()
        self._inflight_completions: set[str] = set()  # keyed by "worker:task_id"

    async def analyze_escalation(self, worker: Worker, reason: str) -> None:
        """Ask Queen to analyze an escalated worker and act or propose.

        High-confidence actions are executed immediately. Low-confidence
        actions (and plans) are surfaced to the user as proposals.
        """
        d = self._daemon
        try:
            from swarm.tmux.cell import capture_pane

            content = await capture_pane(worker.pane_id)
            hive_ctx = await self.gather_context()
            result = await self.queen.analyze_worker(worker.name, content, hive_context=hive_ctx)
        except (OSError, asyncio.TimeoutError):
            _log.warning("Queen escalation analysis failed for %s", worker.name, exc_info=True)
            self._inflight_escalations.discard(worker.name)
            return
        finally:
            self._inflight_escalations.discard(worker.name)

        if not isinstance(result, dict):
            return

        action = result.get("action", "wait")
        confidence = float(result.get("confidence", 0.8))
        reason_lower = reason.lower()
        # User questions and plans always require user approval — the Queen
        # must never auto-act on these.  Approval-rule escalations ("choice
        # requires approval") are handled by the Queen's confidence threshold:
        # if the Queen is confident enough, it auto-acts; otherwise it creates
        # a proposal for the user.
        requires_user = "plan" in reason_lower or "user question" in reason_lower

        assessment = result.get("assessment", "")
        reasoning = result.get("reasoning", "")
        message = result.get("message", "")

        # Reject proposals with no actionable content — useless to the user
        if not assessment and not reasoning and not message:
            _log.info(
                "Queen returned empty escalation analysis for %s — dropping",
                worker.name,
            )
            return

        proposal = AssignmentProposal.escalation(
            worker_name=worker.name,
            action=action,
            assessment=assessment or reasoning or f"Escalation: {reason}",
            message=message,
            reasoning=reasoning or assessment,
            confidence=confidence,
        )

        # Race guard: another escalation may have created a proposal while Queen was thinking
        if d.proposal_store.has_pending_escalation(worker.name):
            _log.debug("dropping duplicate Queen proposal for %s", worker.name)
            return

        # Only auto-execute for routine escalations (unrecognized state, etc.)
        # where the Queen is confident. User-facing decisions always go to the user.
        # Never auto-execute send_message — injecting arbitrary text into a worker
        # pane is too dangerous without human review.
        safe_auto_actions = ("continue", "restart")
        if (
            not requires_user
            and confidence >= self.queen.min_confidence
            and action in safe_auto_actions
        ):
            _log.info(
                "Queen auto-acting on %s: %s (confidence=%.0f%%)",
                worker.name,
                action,
                confidence * 100,
            )
            await self.execute_escalation(proposal)
            d.drone_log.add(
                DroneAction.CONTINUED,
                worker.name,
                f"Queen auto-acted: {action} ({confidence * 100:.0f}%)",
            )
            d.drone_log.add(
                SystemAction.QUEEN_AUTO_ACTED,
                worker.name,
                f"{action} ({confidence * 100:.0f}%)",
                category=LogCategory.QUEEN,
                is_notification=True,
            )
            d._broadcast_ws(
                {
                    "type": "queen_auto_acted",
                    "worker": worker.name,
                    "action": action,
                    "confidence": confidence,
                    "assessment": result.get("assessment", ""),
                }
            )
        else:
            d._on_proposal(proposal)

    async def execute_escalation(self, proposal: AssignmentProposal) -> bool:
        """Execute an approved escalation proposal's recommended action."""
        from swarm.tmux.cell import send_enter, send_keys
        from swarm.worker.manager import revive_worker

        d = self._daemon
        worker = d.get_worker(proposal.worker_name)
        if not worker:
            return False

        action = proposal.queen_action
        if action == "send_message" and proposal.message:
            await send_keys(worker.pane_id, proposal.message)
        elif action == "continue":
            await send_enter(worker.pane_id)
        elif action == "restart":
            await revive_worker(worker, session_name=d.config.session_name)
            worker.record_revive()
        # "wait" is a no-op
        return True

    async def analyze_completion(self, worker: Worker, task: SwarmTask) -> None:
        """Ask Queen to assess whether a task is complete and draft resolution."""
        d = self._daemon
        key = f"{worker.name}:{task.id}"
        # Re-check: worker may have resumed working since the event was queued
        if worker.state == WorkerState.BUZZING:
            _log.info(
                "Aborting completion analysis for '%s': worker %s resumed (BUZZING)",
                task.title,
                worker.name,
            )
            self._inflight_completions.discard(key)
            return

        try:
            from swarm.tmux.cell import capture_pane

            content = await capture_pane(worker.pane_id, lines=100)
            result = await self.queen.ask(
                f"Worker '{worker.name}' was assigned task:\n"
                f"  Title: {task.title}\n"
                f"  Description: {task.description or 'N/A'}\n"
                f"  Type: {getattr(task.task_type, 'value', task.task_type)}\n\n"
                f"The worker has been idle for {worker.state_duration:.0f}s.\n\n"
                f"Recent worker output (last 100 lines):\n{content}\n\n"
                "Analyze the output carefully. Look for concrete evidence:\n"
                "- Commits, pushes, or PRs created\n"
                "- Tests passing or failing\n"
                "- Error messages or unresolved issues\n"
                "- The worker explicitly stating it finished or got stuck\n\n"
                "Do NOT restate the task title as the resolution. Instead describe "
                "what the worker ACTUALLY DID based on the output evidence.\n\n"
                "Return JSON:\n"
                '{"done": true/false, "resolution": "what the worker actually accomplished '
                'or what remains unfinished — cite specific evidence from the output", '
                '"confidence": 0.0 to 1.0}\n\n'
                "Set done=false unless you see clear evidence of completion "
                "(commit, tests passing, worker saying done). When in doubt, say not done."
            )
        except (OSError, asyncio.TimeoutError):
            _log.warning("Queen completion analysis failed for %s", worker.name, exc_info=True)
            self._inflight_completions.discard(key)
            return
        finally:
            self._inflight_completions.discard(key)

        done = result.get("done", False) if isinstance(result, dict) else False
        resolution = (
            result.get("resolution", f"Worker idle for {worker.state_duration:.0f}s")
            if isinstance(result, dict)
            else f"Worker idle for {worker.state_duration:.0f}s"
        )
        confidence = float(result.get("confidence", 0.3)) if isinstance(result, dict) else 0.3

        # Reject idle-fallback resolutions — Queen didn't provide real analysis
        if re.match(r"^worker\s+\S*\s*(?:idle|has been idle)\s+for\s+\d+", resolution, re.I):
            _log.info(
                "Queen returned idle-fallback resolution for task '%s' — not proposing",
                task.title,
            )
            return

        # Sanity check: if the resolution text contradicts "done", override
        _NOT_DONE_PHRASES = (
            "could not be verified",
            "not verified",
            "could not confirm",
            "unable to confirm",
            "unable to verify",
            "not complete",
            "not done",
            "not finished",
            "needs more work",
            "went idle without",
            "did not complete",
            "hasn't been completed",
            "recommend re-running",
        )
        res_lower = resolution.lower()
        if done and any(phrase in res_lower for phrase in _NOT_DONE_PHRASES):
            _log.info(
                "Queen said done but resolution contradicts — overriding to not done: %s",
                resolution[:120],
            )
            done = False

        if not done:
            _log.info("Queen says task '%s' is NOT done for %s", task.title, worker.name)
            return

        if confidence < 0.6:
            _log.info(
                "Queen confidence too low (%.2f) for task '%s' — skipping proposal",
                confidence,
                task.title,
            )
            return

        # Race guard: worker may have resumed while Queen was thinking
        if worker.state == WorkerState.BUZZING:
            _log.info(
                "Worker %s resumed (BUZZING) — dropping completion proposal for '%s'",
                worker.name,
                task.title,
            )
            return

        # Race guard: duplicate proposal check
        if d.proposal_store.has_pending_completion(worker.name, task.id):
            return

        proposal = AssignmentProposal.completion(
            worker_name=worker.name,
            task_id=task.id,
            task_title=task.title,
            assessment=resolution,
            reasoning=f"Worker idle for {worker.state_duration:.0f}s",
            confidence=confidence,
        )
        d._on_proposal(proposal)

    async def gather_context(self) -> str:
        """Capture all worker panes and build hive context string for the Queen."""
        from swarm.queen.context import build_hive_context
        from swarm.tmux.cell import capture_pane

        d = self._daemon
        worker_outputs: dict[str, str] = {}
        for w in list(d.workers):
            try:
                worker_outputs[w.name] = await capture_pane(w.pane_id, lines=60)
            except (OSError, asyncio.TimeoutError):
                _log.debug("failed to capture pane for %s in queen flow", w.name)
        return build_hive_context(
            list(d.workers),
            worker_outputs=worker_outputs,
            drone_log=d.drone_log,
            task_board=d.task_board,
            worker_descriptions=d._worker_descriptions(),
            approval_rules=d.config.drones.approval_rules or None,
        )

    async def analyze_worker(self, worker_name: str, *, force: bool = False) -> dict:
        """Run Queen analysis on a specific worker. Returns Queen's analysis dict.

        Does NOT include full hive context — per-worker analysis should focus
        on just that worker's pane output.  Includes assigned task info so the
        Queen can recommend ``complete_task`` when appropriate.
        Use ``coordinate()`` for hive-wide analysis.
        """
        from swarm.tmux.cell import capture_pane

        d = self._daemon
        worker = d._require_worker(worker_name)
        content = await capture_pane(worker.pane_id)

        task_info = build_worker_task_info(d.task_board, worker.name)

        return await self.queen.analyze_worker(
            worker.name, content, force=force, task_info=task_info
        )

    async def coordinate(self, *, force: bool = False) -> dict:
        """Run Queen coordination across the entire hive. Returns coordination dict."""
        hive_ctx = await self.gather_context()
        return await self.queen.coordinate_hive(hive_ctx, force=force)
