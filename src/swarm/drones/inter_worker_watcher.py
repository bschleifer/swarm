"""Inter-worker message watcher drone — nudge idle recipients of unread messages.

Phase 3 of task #235. Phase 1 of the same ticket made messages to the
Queen auto-relay into her PTY; Phase 2 gave her a message-stream view
for triage. This watcher closes the loop for messages between workers:
when worker A sends to worker B and B is RESTING/SLEEPING, A's message
would otherwise sit in B's inbox until B happens to take a turn. That's
the failure mode the operator saw when cross-project coordination
stalled.

Deliberate boundary: workers MUST NOT be able to auto-interrupt each
other (otherwise one worker going pushy would derail the whole swarm).
The auto-interruption here is a drone/server-side concern — it only
fires when the recipient is demonstrably idle AND the message is still
unread, and every nudge is debounced per recipient so a flurry of
messages still results in at most one nudge per debounce window.

Scope mirrors :class:`swarm.drones.idle_watcher.IdleWatcher`: same
config keys (reused), same rate-limit escape hatch, same per-(worker)
debounce, same fault isolation.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from swarm.drones.log import DroneAction, LogCategory
from swarm.logging import get_logger
from swarm.worker.worker import QUEEN_WORKER_NAME, WorkerState

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from swarm.drones.log import DroneLog
    from swarm.messages.store import MessageStore
    from swarm.worker.worker import Worker


_log = get_logger("drones.inter_worker_watcher")


# States where a worker is "idle" and a nudge is appropriate. BUZZING =
# already working, WAITING = approval prompt (different code path), STUNG
# = process exited (revive is a separate concern).
_IDLE_STATES: frozenset[WorkerState] = frozenset({WorkerState.RESTING, WorkerState.SLEEPING})

# Message types that require action from the recipient. Nudging on
# action-required messages is the whole point of the watcher; nudging on
# informational traffic (FYI broadcasts, routine progress updates,
# side-channel notes) risks derailing a worker who has self-resolved
# the underlying concern already — see task #271 for the wifi-portal
# repro.  Operator messages never reach this path: the operator has
# direct PTY access and doesn't need a drone nudge.
_ACTION_REQUIRED_MSG_TYPES: frozenset[str] = frozenset({"dependency", "warning"})


def _nudge_message(sender: str, unread_count: int) -> str:
    """Build the PTY message sent to an idle recipient.

    Kept short and tool-centric — like the IdleWatcher's nudge, this
    points the worker at its own ``swarm_check_messages`` tool rather
    than treating the nudge as a fresh conversational prompt.
    """
    if unread_count == 1:
        return f"New message from `{sender}`. Run `swarm_check_messages` to read and process."
    return (
        f"{unread_count} new messages (latest from `{sender}`). "
        "Run `swarm_check_messages` to read and process."
    )


class InterWorkerMessageWatcher:
    """Periodic sweep: idle workers with unread messages get a nudge.

    Parameters
    ----------
    drone_config:
        Reuses ``idle_nudge_interval_seconds`` /
        ``idle_nudge_debounce_seconds`` from :class:`DroneConfig` so
        operators don't have to tune a separate knob. ``interval <= 0``
        disables.
    message_store:
        Source of truth for "does this worker have unread messages".
    drone_log:
        Every nudge is appended as ``AUTO_NUDGE_MESSAGE`` under
        ``LogCategory.DRONE``.
    send_to_worker:
        Async callable
        ``(worker_name, message, *, _log_operator=False) -> None``.
        Mirrors :meth:`SwarmDaemon.send_to_worker`.
    rate_limit_check:
        Optional ``(worker_name) -> bool``. Returning True skips the
        nudge — the worker hit the Claude 5hr quota and piling up work
        behind a dead quota is pointless.
    """

    def __init__(
        self,
        *,
        drone_config,
        message_store: MessageStore | None,
        drone_log: DroneLog,
        send_to_worker: Callable[..., Awaitable[None]],
        rate_limit_check: Callable[[str], bool] | None = None,
    ) -> None:
        self._config = drone_config
        self._message_store = message_store
        self._drone_log = drone_log
        self._send_to_worker = send_to_worker
        self._rate_limit_check = rate_limit_check
        # worker_name → last-nudge monotonic timestamp
        self._last_nudge: dict[str, float] = {}
        # worker_name → last AUTO_NUDGE_MESSAGE_SKIPPED entry timestamp.
        # Separate from ``_last_nudge`` so an informational-only inbox
        # doesn't block later real nudges — debounce applies to the
        # SKIPPED entry only, and uses the same window so the buzz log
        # doesn't spam operator with repeat "informational only"
        # entries on every sweep (task #271).
        self._last_skip_log: dict[str, float] = {}
        self._last_sweep: float = 0.0

    @property
    def interval_seconds(self) -> float:
        return float(self._config.idle_nudge_interval_seconds or 0.0)

    @property
    def debounce_seconds(self) -> float:
        return float(self._config.idle_nudge_debounce_seconds or 0.0)

    @property
    def enabled(self) -> bool:
        return self.interval_seconds > 0 and self._message_store is not None

    async def sweep(self, workers: list[Worker], *, now: float | None = None) -> int:
        """Run one sweep. Returns the number of nudges actually sent.

        Safe to call more often than ``interval_seconds``; no-ops until
        the window has elapsed. Caller can force a sweep by passing a
        ``now`` that pushes past the threshold.
        """
        if not self.enabled:
            return 0
        now = now if now is not None else time.monotonic()
        if (now - self._last_sweep) < self.interval_seconds:
            return 0
        self._last_sweep = now

        sent = 0
        for worker in workers:
            if not self._should_nudge(worker, now=now):
                continue
            try:
                # get_unread is read-only (does NOT mark-read); safe to
                # call from the watcher without disturbing the worker's
                # actual swarm_check_messages flow.
                unread = self._message_store.get_unread(worker.name)
            except Exception:
                _log.debug(
                    "inter_worker_watcher: get_unread raised for %s",
                    worker.name,
                    exc_info=True,
                )
                continue
            # Filter out queen-sourced messages — the Queen's own relay
            # path (task #235 Phase 1) already injects those directly
            # into the recipient's PTY via ``queen_prompt_worker``;
            # double-nudging would just spam.
            inter_worker = [m for m in unread if m.sender and m.sender != QUEEN_WORKER_NAME]
            if not inter_worker:
                continue
            # Task #271: narrow the nudge trigger to action-required
            # message types.  Informational messages (finding / status /
            # note) should not pull a worker off its current task —
            # they'll get picked up on the next ``swarm_check_messages``
            # the worker makes naturally.  When there ARE action-
            # required messages, they drive the nudge; the informational
            # backlog rides along and gets surfaced too.
            action_required = [m for m in inter_worker if m.msg_type in _ACTION_REQUIRED_MSG_TYPES]
            if not action_required:
                # Informational-only: skip + log so the operator has
                # visibility on why the inbox sits unread (prior
                # behaviour would have nudged and potentially derailed
                # the worker).  Debounce the skip entry per worker
                # using the same timestamp the nudge would have used,
                # so we don't spam AUTO_NUDGE_MESSAGE_SKIPPED on every
                # sweep for the same inbox state.
                if not self._is_skip_logged(worker.name, now=now):
                    latest_info = max(inter_worker, key=lambda m: m.created_at)
                    type_summary = ", ".join(sorted({m.msg_type for m in inter_worker}))
                    self._drone_log.add(
                        DroneAction.AUTO_NUDGE_MESSAGE_SKIPPED,
                        worker.name,
                        (
                            f"informational only from {latest_info.sender} "
                            f"({len(inter_worker)} unread: {type_summary}) — "
                            "not nudging"
                        ),
                        category=LogCategory.DRONE,
                    )
                    self._last_skip_log[worker.name] = now
                continue
            latest = max(action_required, key=lambda m: m.created_at)
            message = _nudge_message(latest.sender, len(inter_worker))
            try:
                await self._send_to_worker(worker.name, message, _log_operator=False)
            except Exception:
                _log.warning(
                    "inter_worker_watcher: send_to_worker failed for %s",
                    worker.name,
                    exc_info=True,
                )
                continue
            self._last_nudge[worker.name] = now
            self._drone_log.add(
                DroneAction.AUTO_NUDGE_MESSAGE,
                worker.name,
                (
                    f"unread from {latest.sender} "
                    f"({len(inter_worker)} total, "
                    f"{len(action_required)} action-required: {latest.msg_type})"
                ),
                category=LogCategory.DRONE,
            )
            sent += 1
        return sent

    def _should_nudge(self, worker: Worker, *, now: float) -> bool:
        """Cheap filters applied BEFORE we query the message store."""
        if worker.name == QUEEN_WORKER_NAME:
            # The Queen gets her own inbox relay via the Phase 1 path;
            # no need to double-nudge her.
            return False
        if worker.display_state not in _IDLE_STATES:
            return False
        if self._is_debounced(worker.name, now=now):
            return False
        if self._rate_limit_check is not None:
            try:
                if self._rate_limit_check(worker.name):
                    return False
            except Exception:
                _log.debug(
                    "inter_worker_watcher: rate_limit_check raised for %s",
                    worker.name,
                    exc_info=True,
                )
                return False
        return True

    def _is_debounced(self, name: str, *, now: float) -> bool:
        """True when this worker was nudged within the debounce window."""
        if self.debounce_seconds <= 0:
            return False
        last = self._last_nudge.get(name)
        if last is None:
            return False
        return (now - last) < self.debounce_seconds

    def _is_skip_logged(self, name: str, *, now: float) -> bool:
        """True when we've already logged an informational-only skip
        recently and shouldn't re-log on every sweep."""
        if self.debounce_seconds <= 0:
            return False
        last = self._last_skip_log.get(name)
        if last is None:
            return False
        return (now - last) < self.debounce_seconds
