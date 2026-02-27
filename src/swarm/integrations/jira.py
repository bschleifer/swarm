"""Jira integration — two-way sync between Jira and Swarm task board."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import aiohttp

from swarm.config import JiraConfig
from swarm.logging import get_logger
from swarm.tasks.task import SwarmTask, TaskPriority, TaskStatus, TaskType

_log = get_logger("integrations.jira")

# Jira issue type → Swarm TaskType
_JIRA_TYPE_MAP: dict[str, TaskType] = {
    "bug": TaskType.BUG,
    "story": TaskType.FEATURE,
    "task": TaskType.CHORE,
    "sub-task": TaskType.CHORE,
    "epic": TaskType.FEATURE,
}

# Jira priority → Swarm TaskPriority
_JIRA_PRIORITY_MAP: dict[str, TaskPriority] = {
    "highest": TaskPriority.URGENT,
    "high": TaskPriority.HIGH,
    "medium": TaskPriority.NORMAL,
    "low": TaskPriority.LOW,
    "lowest": TaskPriority.LOW,
}


@dataclass
class JiraSyncStats:
    """Track sync operation results."""

    last_sync: float = 0.0
    total_syncs: int = 0
    total_imported: int = 0
    total_exported: int = 0
    last_error: str = ""
    errors: int = 0


class JiraClient:
    """Async HTTP client for Jira REST API v3."""

    def __init__(self, config: JiraConfig) -> None:
        self._config = config
        self._base_url = config.url.rstrip("/")
        self._session: aiohttp.ClientSession | None = None

    @property
    def _auth(self) -> aiohttp.BasicAuth:
        return aiohttp.BasicAuth(self._config.email, self._config.resolved_token())

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                auth=self._auth,
                headers={"Content-Type": "application/json"},
                timeout=aiohttp.ClientTimeout(total=30),
            )
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    async def search_issues(self, jql: str, max_results: int = 50) -> list[dict[str, Any]]:
        """Search Jira issues using JQL.

        Returns a list of issue dicts with key, summary, description, etc.
        """
        session = await self._ensure_session()
        url = f"{self._base_url}/rest/api/3/search"
        params = {
            "jql": jql,
            "maxResults": max_results,
            "fields": "summary,description,status,issuetype,priority,labels",
        }
        async with session.get(url, params=params) as resp:
            resp.raise_for_status()
            data = await resp.json()
        return data.get("issues", [])

    async def get_transitions(self, issue_key: str) -> list[dict[str, Any]]:
        """Get available transitions for an issue."""
        session = await self._ensure_session()
        url = f"{self._base_url}/rest/api/3/issue/{issue_key}/transitions"
        async with session.get(url) as resp:
            resp.raise_for_status()
            data = await resp.json()
        return data.get("transitions", [])

    async def transition_issue(self, issue_key: str, transition_id: str) -> bool:
        """Transition an issue to a new status."""
        session = await self._ensure_session()
        url = f"{self._base_url}/rest/api/3/issue/{issue_key}/transitions"
        payload = {"transition": {"id": transition_id}}
        async with session.post(url, json=payload) as resp:
            if resp.status == 204:
                return True
            _log.warning(
                "transition %s to %s failed: %d",
                issue_key,
                transition_id,
                resp.status,
            )
            return False

    async def add_comment(self, issue_key: str, body: str) -> bool:
        """Add a comment to an issue using ADF (Atlassian Document Format)."""
        session = await self._ensure_session()
        url = f"{self._base_url}/rest/api/3/issue/{issue_key}/comment"
        payload = {
            "body": {
                "type": "doc",
                "version": 1,
                "content": [
                    {
                        "type": "paragraph",
                        "content": [{"type": "text", "text": body}],
                    }
                ],
            }
        }
        async with session.post(url, json=payload) as resp:
            if resp.status in (200, 201):
                return True
            _log.warning(
                "comment on %s failed: %d",
                issue_key,
                resp.status,
            )
            return False


class JiraSyncService:
    """Two-way sync between Jira and Swarm's task board."""

    def __init__(self, config: JiraConfig) -> None:
        self._config = config
        self.client = JiraClient(config)
        self.stats = JiraSyncStats()
        self._running = False

    @property
    def enabled(self) -> bool:
        return self._config.enabled and bool(self._config.url)

    async def close(self) -> None:
        self._running = False
        await self.client.close()

    # --- Import: Jira → Swarm ---

    async def import_issues(self, existing_tasks: dict[str, SwarmTask]) -> list[SwarmTask]:
        """Fetch issues from Jira and return new SwarmTasks to create.

        Deduplicates by checking ``jira_key`` against existing tasks.
        """
        if not self.enabled:
            return []

        jql = self._config.import_filter
        if not jql:
            jql = f"project = {self._config.project} AND status = 'To Do'"

        try:
            issues = await self.client.search_issues(jql)
        except (aiohttp.ClientError, TimeoutError) as e:
            self.stats.last_error = str(e)
            self.stats.errors += 1
            _log.warning("Jira import failed: %s", e)
            return []

        # Build set of existing jira_keys for dedup
        known_keys = {t.jira_key for t in existing_tasks.values() if t.jira_key}

        new_tasks: list[SwarmTask] = []
        for issue in issues:
            key = issue.get("key", "")
            if not key or key in known_keys:
                continue

            fields = issue.get("fields", {})
            task = _jira_issue_to_task(key, fields)
            new_tasks.append(task)
            known_keys.add(key)

        self.stats.total_imported += len(new_tasks)
        self.stats.last_sync = time.time()
        self.stats.total_syncs += 1

        if new_tasks:
            _log.info("imported %d new tasks from Jira", len(new_tasks))
        return new_tasks

    # --- Export: Swarm → Jira ---

    async def export_status(self, task: SwarmTask, new_status: TaskStatus) -> bool:
        """Update a Jira ticket's status to match the Swarm task status."""
        if not self.enabled or not task.jira_key:
            return False

        target_name = self._config.status_map.get(new_status.value, "")
        if not target_name:
            _log.debug(
                "no Jira status mapping for %s",
                new_status.value,
            )
            return False

        try:
            transitions = await self.client.get_transitions(task.jira_key)
        except (aiohttp.ClientError, TimeoutError) as e:
            self.stats.last_error = str(e)
            self.stats.errors += 1
            _log.warning(
                "failed to get transitions for %s: %s",
                task.jira_key,
                e,
            )
            return False

        # Find transition matching target status name
        transition_id = _find_transition(transitions, target_name)
        if not transition_id:
            _log.warning(
                "no transition to '%s' found for %s (available: %s)",
                target_name,
                task.jira_key,
                [t.get("name", "") for t in transitions],
            )
            return False

        try:
            ok = await self.client.transition_issue(
                task.jira_key,
                transition_id,
            )
        except (aiohttp.ClientError, TimeoutError) as e:
            self.stats.last_error = str(e)
            self.stats.errors += 1
            _log.warning(
                "failed to transition %s: %s",
                task.jira_key,
                e,
            )
            return False

        if ok:
            self.stats.total_exported += 1
            _log.info(
                "transitioned %s to '%s'",
                task.jira_key,
                target_name,
            )
        return ok

    async def post_completion_comment(self, task: SwarmTask, *, summary: str = "") -> bool:
        """Post a completion summary as a Jira comment."""
        if not self.enabled or not task.jira_key:
            return False

        parts = ["Task completed in Swarm."]
        if task.assigned_worker:
            parts.append(f"Worker: {task.assigned_worker}")
        if task.resolution:
            parts.append(f"Resolution: {task.resolution}")
        if summary:
            parts.append(f"Summary: {summary}")

        body = "\n".join(parts)

        try:
            return await self.client.add_comment(task.jira_key, body)
        except (aiohttp.ClientError, TimeoutError) as e:
            self.stats.last_error = str(e)
            self.stats.errors += 1
            _log.warning(
                "failed to comment on %s: %s",
                task.jira_key,
                e,
            )
            return False

    def get_status(self) -> dict[str, Any]:
        """Return sync status for API/WS."""
        return {
            "enabled": self.enabled,
            "url": self._config.url,
            "project": self._config.project,
            "last_sync": self.stats.last_sync,
            "total_syncs": self.stats.total_syncs,
            "total_imported": self.stats.total_imported,
            "total_exported": self.stats.total_exported,
            "errors": self.stats.errors,
            "last_error": self.stats.last_error,
        }


# --- Helpers ---


def _jira_issue_to_task(key: str, fields: dict[str, Any]) -> SwarmTask:
    """Convert a Jira issue's fields to a SwarmTask."""
    summary = fields.get("summary", key)

    # Extract plain-text description from ADF or string
    raw_desc = fields.get("description")
    description = _extract_text(raw_desc) if raw_desc else ""

    # Map issue type
    issue_type_name = ""
    issue_type = fields.get("issuetype")
    if isinstance(issue_type, dict):
        issue_type_name = issue_type.get("name", "").lower()
    task_type = _JIRA_TYPE_MAP.get(issue_type_name, TaskType.CHORE)

    # Map priority
    priority_name = ""
    priority = fields.get("priority")
    if isinstance(priority, dict):
        priority_name = priority.get("name", "").lower()
    task_priority = _JIRA_PRIORITY_MAP.get(
        priority_name,
        TaskPriority.NORMAL,
    )

    return SwarmTask(
        title=summary,
        description=description,
        jira_key=key,
        task_type=task_type,
        priority=task_priority,
    )


def _extract_text(adf: Any) -> str:
    """Extract plain text from an ADF document or plain string."""
    if isinstance(adf, str):
        return adf
    if not isinstance(adf, dict):
        return ""

    parts: list[str] = []

    def _walk(node: Any) -> None:
        if isinstance(node, dict):
            if node.get("type") == "text":
                parts.append(node.get("text", ""))
            for child in node.get("content", []):
                _walk(child)
        elif isinstance(node, list):
            for item in node:
                _walk(item)

    _walk(adf)
    return " ".join(parts).strip()


def _find_transition(transitions: list[dict[str, Any]], target_name: str) -> str | None:
    """Find a transition ID whose target status matches the name."""
    target_lower = target_name.lower()
    for t in transitions:
        name = t.get("name", "").lower()
        if name == target_lower:
            return t.get("id", "")
        # Also check the "to" status name
        to_status = t.get("to", {})
        if isinstance(to_status, dict):
            to_name = to_status.get("name", "").lower()
            if to_name == target_lower:
                return t.get("id", "")
    return None
