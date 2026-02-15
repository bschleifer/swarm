"""The Queen — headless Claude conductor for complex decisions."""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from typing import Any

from swarm.config import QueenConfig
from swarm.logging import get_logger
from swarm.queen.session import clear_session, load_session, save_session

_log = get_logger("queen")

_DEFAULT_TIMEOUT = 120  # seconds for claude -p calls

# Matches a fenced JSON code block — the Queen often adds markdown text after
# the closing fence which broke the old starts/endswith parser.
_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*\n(.*?)\n\s*```", re.DOTALL)


def _extract_json(text: str) -> dict[str, Any] | None:
    """Extract and parse JSON from Queen output text.

    Handles three formats:
    1. Plain JSON string
    2. JSON inside markdown code fences (possibly with trailing text)
    3. JSON with leading/trailing whitespace
    """
    stripped = text.strip()

    # Try plain JSON first (no fences)
    try:
        parsed = json.loads(stripped)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass

    # Try extracting from markdown code fences
    m = _JSON_FENCE_RE.search(stripped)
    if m:
        try:
            parsed = json.loads(m.group(1))
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            _log.debug("JSON fence found but parse failed")

    _log.debug("Queen inner JSON parse failed")
    return None


# Environment variables to strip when spawning headless claude -p.
# These leak from the parent Claude Code session and can cause the
# child process to target an interactive session instead of running headlessly.
_STRIP_ENV_PREFIXES = ("CLAUDE",)


class Queen:
    def __init__(
        self,
        config: QueenConfig | None = None,
        session_name: str = "default",
    ) -> None:
        cfg = config or QueenConfig()
        self.session_name = session_name
        self.session_id: str | None = None
        self.enabled = cfg.enabled
        self.cooldown = cfg.cooldown
        self.system_prompt = cfg.system_prompt
        self.min_confidence = cfg.min_confidence
        self._last_call: float = 0.0
        self._last_coordination: float = 0.0
        self._lock = asyncio.Lock()
        # Load persisted session ID
        self.session_id = load_session(self.session_name)
        if self.session_id:
            _log.info("restored Queen session: %s", self.session_id)

    @property
    def can_call(self) -> bool:
        return self.enabled and time.time() - self._last_call >= self.cooldown

    @property
    def cooldown_remaining(self) -> float:
        """Seconds until the Queen can be called again."""
        remaining = self.cooldown - (time.time() - self._last_call)
        return max(0.0, remaining)

    @staticmethod
    def _clean_env() -> dict[str, str]:
        """Build a clean environment for headless claude -p subprocesses.

        Strips CLAUDE* variables that leak from the parent Claude Code
        session, preventing the child from targeting an interactive session.
        """
        return {
            k: v
            for k, v in os.environ.items()
            if not any(k.startswith(p) for p in _STRIP_ENV_PREFIXES)
        }

    async def _run_claude(self, args: list[str]) -> tuple[bytes, bytes, int]:
        """Run a claude subprocess and return (stdout, stderr, returncode)."""
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=self._clean_env(),
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=_DEFAULT_TIMEOUT)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            _log.warning("Queen call timed out after %ds", _DEFAULT_TIMEOUT)
            return b"", b"timeout", -1
        return stdout, stderr, proc.returncode or 0

    def _prepend_system_prompt(self, prompt: str) -> str:
        """Prepend operator system prompt if configured."""
        if self.system_prompt:
            return f"[Operator instructions]\n{self.system_prompt}\n\n{prompt}"
        return prompt

    async def ask(  # noqa: C901
        self,
        prompt: str,
        *,
        _coordination: bool = False,
        force: bool = False,
        stateless: bool = False,
    ) -> dict[str, Any]:
        """Ask the Queen a question using claude -p with JSON output.

        When *_coordination* is True (periodic background check), the call
        uses a separate cooldown timer so it doesn't block reactive calls
        like task-completion analysis or escalation handling.

        When *force* is True (user-initiated), the cooldown is bypassed.

        When *stateless* is True, ``--resume`` is NOT used, so the call
        has no memory of previous conversations.  This prevents stale state
        from prior hive-wide analyses bleeding into per-worker queries.
        """
        prompt = self._prepend_system_prompt(prompt)
        # Lock scope 1: rate-limit check + timestamp update (fast, <1ms)
        async with self._lock:
            if force:
                # User-initiated: bypass cooldown, still update timestamp
                self._last_call = time.time()
            elif _coordination:
                if not self.enabled or time.time() - self._last_coordination < self.cooldown:
                    rem = self.cooldown - (time.time() - self._last_coordination)
                    return {"error": f"Coordination rate limited ({max(0, rem):.0f}s)"}
                self._last_coordination = time.time()
            else:
                if not self.can_call:
                    wait = self.cooldown_remaining
                    return {"error": f"Rate limited — try again in {wait:.0f}s"}
                self._last_call = time.time()
            session_id = None if stateless else self.session_id

        # Build args outside lock
        args = [
            "claude",
            "-p",
            prompt,
            "--output-format",
            "json",
        ]
        if session_id:
            args.extend(["--resume", session_id])

        # Run subprocess outside lock so other callers aren't blocked
        stdout, stderr, returncode = await self._run_claude(args)
        if returncode == -1:
            return {"error": f"Queen call timed out after {_DEFAULT_TIMEOUT}s"}

        # Detect stale session and retry without --resume
        if (
            returncode != 0
            and session_id
            and "No conversation found" in stderr.decode(errors="replace")
        ):
            _log.warning("Stale Queen session %s — clearing and retrying", session_id)
            clear_session(self.session_name)
            async with self._lock:
                self.session_id = None
            args = [a for a in args if a not in ("--resume", session_id)]
            stdout, stderr, returncode = await self._run_claude(args)
            if returncode == -1:
                return {"error": f"Queen call timed out after {_DEFAULT_TIMEOUT}s"}

        if returncode != 0:
            _log.warning("Queen process exited with code %d: %s", returncode, stderr.decode()[:200])

        try:
            result = json.loads(stdout.decode())
            # Lock scope 2: save session ID (fast)
            if isinstance(result, dict) and "session_id" in result:
                async with self._lock:
                    self.session_id = result["session_id"]
                save_session(self.session_name, result["session_id"])
            # claude -p --output-format json wraps the response in an envelope:
            # {"type": "result", "result": "...actual text...", "session_id": "..."}
            # Try to extract and parse the inner JSON from the result text.
            inner = result.get("result", "") if isinstance(result, dict) else ""
            if isinstance(inner, str):
                parsed = _extract_json(inner)
                if isinstance(parsed, dict):
                    return parsed
            return result
        except json.JSONDecodeError:
            _log.warning("Queen returned non-JSON: %s", stdout.decode()[:200])
            return {"result": stdout.decode(), "raw": True}

    async def analyze_worker(
        self,
        worker_name: str,
        pane_content: str,
        hive_context: str = "",
        *,
        force: bool = False,
        task_info: str = "",
    ) -> dict[str, Any]:
        """Ask the Queen to analyze a worker and recommend action.

        Per-worker calls are **stateless** (no ``--resume``) so stale hive
        state from previous coordination calls doesn't bleed in.
        """
        hive_section = ""
        if hive_context:
            hive_section = f"""
## Full Hive State
{hive_context}
"""

        task_section = ""
        if task_info:
            task_section = f"""
## Assigned Task
{task_info}
"""

        prompt = f"""You are the Queen of a swarm of Claude Code agents.

Analyze ONLY worker '{worker_name}'. Do NOT reference or make claims about
other workers — you have no information about them in this call.

Note: Drones handle routine approvals automatically using configured rules.
Escalated choices (destructive operations) are sent to you for review.
Low-confidence assessments will be presented to the operator for confirmation.

IMPORTANT: If the worker is presenting a plan for approval (plan mode),
you MUST set confidence to 0.0 and action to "wait". Plans always require
human review — never auto-approve or auto-reject a plan.

Current pane output (recent):
```
{pane_content}
```
{hive_section}{task_section}
Analyze the situation and respond with ONLY a JSON object (no extra text):
{{
  "assessment": "brief description of what's happening with THIS worker",
  "action": "continue" | "send_message" | "complete_task" | "restart" | "wait",
  "message": "message to send if action is send_message",
  "reasoning": "why you chose this action",
  "confidence": 0.0 to 1.0 — calibrate carefully:
    0.95+: Absolutely certain (worker explicitly said "done", tests all green)
    0.8-0.9: High confidence with clear evidence
    0.6-0.7: Reasonable but some ambiguity
    0.4-0.5: Uncertain — could go either way
    Below 0.4: Low confidence — flag for human review
}}

Action guide:
- "continue": Press Enter to accept a prompt/choice (worker waiting for input)
- "send_message": Send a specific message to the worker
- "complete_task": The assigned task is DONE — worker shows evidence of completion
  (commits pushed, tests passing, deployment succeeded, explicit "done" message).
  Use this when the worker is idle at prompt and the task output shows success.
- "restart": Restart the worker (crashed/stuck)
- "wait": No action needed right now"""
        # Per-worker analysis is stateless to avoid stale hive-state memory.
        # Escalation calls (with hive_context) use the session for continuity.
        use_session = bool(hive_context)
        return await self.ask(prompt, force=force, stateless=not use_session)

    async def assign_tasks(
        self,
        idle_workers: list[str],
        available_tasks: list[dict[str, Any]],
        hive_context: str = "",
    ) -> list[dict[str, Any]]:
        """Ask the Queen to match idle workers to available tasks.

        Returns a list of assignments: [{"worker": str, "task_id": str, "message": str}]
        """
        if not idle_workers or not available_tasks:
            return []

        task_lines: list[str] = []
        for t in available_tasks:
            task_type = t.get("task_type", "chore")
            line = f"- [{t['id']}] {t['title']} (priority={t['priority']}, type={task_type})"
            desc = t.get("description", "")
            if desc:
                task_lines.append(line)
                task_lines.append(f"  Description: {desc}")
            else:
                task_lines.append(line)
            attachments = t.get("attachments", [])
            if attachments:
                fnames = [a.rsplit("/", 1)[-1] for a in attachments]
                task_lines.append(f"  Attachments: {', '.join(fnames)}")
            tags = t.get("tags", [])
            if tags:
                task_lines.append(f"  Tags: {', '.join(tags)}")
        tasks_desc = "\n".join(task_lines)
        workers_desc = ", ".join(idle_workers)

        ctx_section = f"\n## Hive Context\n{hive_context}" if hive_context else ""

        prompt = f"""You are the Queen of a swarm of Claude Code agents.

Idle workers needing tasks: {workers_desc}

Available tasks:
{tasks_desc}
{ctx_section}

Match idle workers to the most appropriate available tasks.
Use worker descriptions/paths and task content to find the best match.
Not every worker needs a task — only assign if there's a good match.
Drones have approval rules configured and will auto-handle routine choices.
Escalated choices will come back for your review.

Each task has a "type" field (bug, verify, feature, chore). Tailor your instructions accordingly:
- bug: TDD workflow — trace root cause, write failing test, minimal fix, validate, commit
- verify: Pull latest, run tests, verify specific behavior, report pass/fail (no code changes)
- feature: Read existing patterns, implement minimally, write tests, validate, commit
- chore: Complete the task, validate, commit

Your "message" field is the ONLY instruction the worker receives. Include:
- The full task description
- Attachment file paths (if any)
- Workflow instructions matching the task type
- Clear instructions on what to do

Respond with a JSON object:
{{
  "assignments": [
    {{
      "worker": "worker_name",
      "task_id": "task_id",
      "message": "full task instructions for the worker",
      "confidence": 0.0 to 1.0 — calibrate carefully:
        0.95+: Perfect match (worker skills align exactly, task is clear)
        0.8-0.9: Strong match with clear evidence
        0.6-0.7: Reasonable match but some ambiguity
        0.4-0.5: Uncertain — could assign to multiple workers
        Below 0.4: Poor match — flag for human review
    }}
  ],
  "reasoning": "brief explanation of matching logic"
}}"""
        result = await self.ask(prompt)
        if isinstance(result, dict):
            return result.get("assignments", [])
        return []

    async def draft_email_reply(self, task_title: str, task_type: str, resolution: str) -> str:
        """Draft a short, professional email reply for a completed task.

        Returns plain text suitable for the Graph API ``comment`` field.
        Falls back to a simple default if Claude fails.
        """
        prompt = (
            "Draft a brief, professional email reply (2-4 sentences) explaining "
            "what was done. Keep it non-technical and friendly. Do NOT include a "
            "subject line, greeting, or sign-off — just the reply body.\n\n"
            f"Task: {task_title}\n"
            f"Type: {task_type}\n"
            f"Resolution: {resolution}\n\n"
            "Return ONLY the reply text, nothing else."
        )
        args = [
            "claude",
            "-p",
            prompt,
            "--output-format",
            "text",
            "--max-turns",
            "1",
        ]
        stdout, stderr, returncode = await self._run_claude(args)
        if returncode == 0 and stdout.strip():
            return stdout.decode().strip()
        _log.warning("draft_email_reply failed (rc=%d), using fallback", returncode)
        return (
            f"This has been addressed. {resolution}" if resolution else "This has been addressed."
        )

    async def coordinate_hive(self, hive_context: str, *, force: bool = False) -> dict[str, Any]:
        """Ask the Queen to do a full hive analysis and return directives.

        Used for proactive coordination: task decomposition, conflict
        detection, pipeline orchestration.
        """
        prompt = f"""You are the Queen of a swarm of Claude Code agents.
Analyze the full hive state and provide coordination directives.

{hive_context}

Respond with a JSON object:
{{
  "assessment": "overall hive health and what's happening",
  "confidence": 0.0 to 1.0 — calibrate carefully:
    0.95+: Absolutely certain (clear evidence in worker output)
    0.8-0.9: High confidence with clear evidence
    0.6-0.7: Reasonable but some ambiguity
    0.4-0.5: Uncertain — could go either way
    Below 0.4: Low confidence — flag for human review,
  "directives": [
    {{
      "worker": "worker_name",
      "action": "continue" | "send_message" | "restart" | "wait" | "assign_task" | "complete_task",
      "message": "message to send (if action is send_message or assign_task)",
      "task_id": "task ID (REQUIRED for complete_task and assign_task)",
      "resolution": "summary of what was done (REQUIRED for complete_task)",
      "reason": "why"
    }}
  ],
  "conflicts": ["description of any detected conflicts between workers"],
  "suggestions": ["high-level suggestions for the human operator"]
}}

IMPORTANT — task lifecycle:
- Only use "complete_task" when you are CONFIDENT the task is genuinely finished. Evidence must
  include: worker is RESTING/idle, AND the output clearly shows the work was completed (e.g.
  successful commit, "all tests pass", explicit completion message). A worker being idle for a
  moment is NOT enough — they may be between steps.
- Do NOT complete tasks for BUZZING workers — they are still actively working.
- For every complete_task directive, you MUST include a "resolution" field summarizing what the
  worker did.  Be specific: mention files changed, tests added, bugs fixed.
- When in doubt, use "wait" — it is always safer to let the worker finish on its own than to
  prematurely mark a task as done. Premature completion is WORSE than a small delay.
- Use "wait" when the worker is busy, between steps, or when you're unsure if it's done."""
        return await self.ask(prompt, _coordination=not force, force=force)
