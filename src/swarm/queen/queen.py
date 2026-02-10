"""The Queen — headless Claude conductor for complex decisions."""

from __future__ import annotations

import asyncio
import json
import time

from swarm.config import QueenConfig
from swarm.logging import get_logger
from swarm.queen.session import clear_session, load_session, save_session

_log = get_logger("queen")

_DEFAULT_TIMEOUT = 120  # seconds for claude -p calls


class Queen:
    def __init__(
        self,
        config: QueenConfig | None = None,
        session_name: str = "default",
    ) -> None:
        cfg = config or QueenConfig()
        self.session_name = session_name
        self.session_id: str | None = None
        self.cooldown = cfg.cooldown
        self._last_call: float = 0.0
        self._lock = asyncio.Lock()
        # Load persisted session ID
        self.session_id = load_session(self.session_name)
        if self.session_id:
            _log.info("restored Queen session: %s", self.session_id)

    @property
    def can_call(self) -> bool:
        return time.time() - self._last_call >= self.cooldown

    @property
    def cooldown_remaining(self) -> float:
        """Seconds until the Queen can be called again."""
        remaining = self.cooldown - (time.time() - self._last_call)
        return max(0.0, remaining)

    async def _run_claude(self, args: list[str]) -> tuple[bytes, bytes, int]:
        """Run a claude subprocess and return (stdout, stderr, returncode)."""
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=_DEFAULT_TIMEOUT)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            _log.warning("Queen call timed out after %ds", _DEFAULT_TIMEOUT)
            return b"", b"timeout", -1
        return stdout, stderr, proc.returncode or 0

    async def ask(self, prompt: str) -> dict:  # noqa: C901
        """Ask the Queen a question using claude -p with JSON output."""
        # Lock scope 1: rate-limit check + timestamp update (fast, <1ms)
        async with self._lock:
            if not self.can_call:
                wait = self.cooldown_remaining
                return {"error": f"Rate limited — try again in {wait:.0f}s"}
            self._last_call = time.time()
            session_id = self.session_id

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
                # Strip markdown code fences if present
                cleaned = inner.strip()
                if cleaned.startswith("```"):
                    # Remove opening fence (```json or ```)
                    cleaned = cleaned.split("\n", 1)[-1] if "\n" in cleaned else cleaned[3:]
                if cleaned.endswith("```"):
                    cleaned = cleaned[:-3].rstrip()
                try:
                    parsed = json.loads(cleaned)
                    if isinstance(parsed, dict):
                        return parsed
                except json.JSONDecodeError:
                    _log.debug("Queen inner JSON parse failed, returning envelope")
            return result
        except json.JSONDecodeError:
            _log.warning("Queen returned non-JSON: %s", stdout.decode()[:200])
            return {"result": stdout.decode(), "raw": True}

    async def analyze_worker(
        self,
        worker_name: str,
        pane_content: str,
        hive_context: str = "",
    ) -> dict:
        """Ask the Queen to analyze a stuck worker and recommend action."""
        hive_section = ""
        if hive_context:
            hive_section = f"""
## Full Hive State
{hive_context}
"""

        prompt = f"""You are the Queen of a swarm of Claude Code agents.

Worker '{worker_name}' needs your attention.

Current pane output (recent):
```
{pane_content}
```
{hive_section}
Analyze the situation and respond with a JSON object:
{{
  "assessment": "brief description of what's happening",
  "action": "continue" | "send_message" | "restart" | "wait",
  "message": "message to send if action is send_message",
  "reasoning": "why you chose this action"
}}"""
        return await self.ask(prompt)

    async def assign_tasks(
        self,
        idle_workers: list[str],
        available_tasks: list[dict],
        hive_context: str = "",
    ) -> list[dict]:
        """Ask the Queen to match idle workers to available tasks.

        Returns a list of assignments: [{"worker": str, "task_id": str, "message": str}]
        """
        if not idle_workers or not available_tasks:
            return []

        tasks_desc = "\n".join(
            f"- [{t['id']}] {t['title']} (priority={t['priority']}): "
            f"{t.get('description', '')[:100]}"
            for t in available_tasks
        )
        workers_desc = ", ".join(idle_workers)

        ctx_section = f"\n## Hive Context\n{hive_context}" if hive_context else ""

        prompt = f"""You are the Queen of a swarm of Claude Code agents.

Idle workers needing tasks: {workers_desc}

Available tasks:
{tasks_desc}
{ctx_section}

Match idle workers to the most appropriate available tasks based on worker names,
task descriptions, and priorities. Not every worker needs a task — only assign
if there's a good match.

Respond with a JSON object:
{{
  "assignments": [
    {{
      "worker": "worker_name",
      "task_id": "task_id",
      "message": "instruction to send to the worker"
    }}
  ],
  "reasoning": "brief explanation of matching logic"
}}"""
        result = await self.ask(prompt)
        if isinstance(result, dict):
            return result.get("assignments", [])
        return []

    async def coordinate_hive(self, hive_context: str) -> dict:
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
  "directives": [
    {{
      "worker": "worker_name",
      "action": "continue" | "send_message" | "restart" | "wait" | "assign_task",
      "message": "message to send (if action is send_message or assign_task)",
      "reason": "why"
    }}
  ],
  "conflicts": ["description of any detected conflicts between workers"],
  "suggestions": ["high-level suggestions for the human operator"]
}}"""
        return await self.ask(prompt)
