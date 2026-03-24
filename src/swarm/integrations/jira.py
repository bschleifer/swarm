"""Jira integration — two-way sync between Jira and Swarm task board."""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import aiohttp

from swarm.config import JiraConfig
from swarm.logging import get_logger
from swarm.tasks.task import SwarmTask, TaskPriority, TaskStatus, TaskType

if TYPE_CHECKING:
    from swarm.auth.jira import JiraTokenManager

_log = get_logger("integrations.jira")

# Jira issue type → Swarm TaskType
_JIRA_TYPE_MAP: dict[str, TaskType] = {
    "bug": TaskType.BUG,
    "story": TaskType.FEATURE,
    "task": TaskType.CHORE,
    "sub-task": TaskType.CHORE,
    "epic": TaskType.FEATURE,
}

# Swarm TaskType → Jira issue type (reverse)
_SWARM_TYPE_TO_JIRA: dict[TaskType, str] = {
    TaskType.BUG: "Bug",
    TaskType.FEATURE: "Story",
    TaskType.CHORE: "Task",
    TaskType.VERIFY: "Task",
    TaskType.CONTENT: "Task",
    TaskType.REVIEW: "Task",
    TaskType.PUBLISH: "Task",
    TaskType.INGEST: "Task",
}

# Jira priority → Swarm TaskPriority
_JIRA_PRIORITY_MAP: dict[str, TaskPriority] = {
    "highest": TaskPriority.URGENT,
    "high": TaskPriority.HIGH,
    "medium": TaskPriority.NORMAL,
    "low": TaskPriority.LOW,
    "lowest": TaskPriority.LOW,
}

# Swarm TaskPriority → Jira priority (reverse)
_SWARM_PRIORITY_TO_JIRA: dict[TaskPriority, str] = {
    TaskPriority.URGENT: "Highest",
    TaskPriority.HIGH: "High",
    TaskPriority.NORMAL: "Medium",
    TaskPriority.LOW: "Low",
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
    """Async HTTP client for Jira REST API v3 (OAuth 2.0 only)."""

    def __init__(self, config: JiraConfig, token_manager: JiraTokenManager | None = None) -> None:
        self._config = config
        self._token_manager = token_manager
        self._base_url = self._resolve_base_url()
        self._session: aiohttp.ClientSession | None = None
        self._current_token: str | None = None  # track OAuth token for session reuse

    def _resolve_base_url(self) -> str:
        if self._token_manager and self._token_manager.api_base_url:
            return self._token_manager.api_base_url
        return ""

    def update_base_url(self) -> None:
        """Refresh base URL (call after cloud_id discovery)."""
        self._base_url = self._resolve_base_url()

    async def _ensure_session(self) -> aiohttp.ClientSession:
        """Create or reuse an OAuth session with Bearer token."""
        if self._token_manager is None:
            raise RuntimeError("No Jira OAuth token manager configured")
        token = await self._token_manager.get_token()
        if not token:
            raise RuntimeError("No valid Jira OAuth token — reconnect via Config page")
        # Recreate session when token changes
        if self._session and not self._session.closed and self._current_token == token:
            return self._session
        if self._session and not self._session.closed:
            await self._session.close()
        self._current_token = token
        self._base_url = self._resolve_base_url()
        _log.debug("Jira session base_url: %s", self._base_url)
        self._session = aiohttp.ClientSession(
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
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
        url = f"{self._base_url}/rest/api/3/search/jql"
        params = {
            "jql": jql,
            "maxResults": max_results,
            "fields": "summary,description,status,issuetype,priority,labels",
        }
        async with session.get(url, params=params) as resp:
            if resp.status != 200:
                body = await resp.text()
                _log.warning(
                    "Jira search failed: %d %s — %s (url=%s, jql=%s)",
                    resp.status,
                    resp.reason,
                    body[:500],
                    url,
                    jql,
                )
                if resp.status == 410 and self._token_manager:
                    _log.warning("410 Gone — cloud_id may be stale, re-discovering")
                    await self._token_manager._discover_cloud_id()
                    self.update_base_url()
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

    async def get_myself(self) -> dict[str, Any]:
        """Fetch the authenticated user's profile (accountId, displayName, etc.)."""
        session = await self._ensure_session()
        url = f"{self._base_url}/rest/api/3/myself"
        async with session.get(url) as resp:
            resp.raise_for_status()
            return await resp.json()

    async def assign_issue(self, issue_key: str, account_id: str) -> bool:
        """Assign a Jira issue to a user by accountId."""
        session = await self._ensure_session()
        url = f"{self._base_url}/rest/api/3/issue/{issue_key}/assignee"
        async with session.put(url, json={"accountId": account_id}) as resp:
            if resp.status == 204:
                return True
            _log.warning(
                "assign %s to %s failed: %d",
                issue_key,
                account_id,
                resp.status,
            )
            return False

    async def create_issue(
        self,
        project: str,
        summary: str,
        description: str,
        issue_type: str = "Task",
        priority: str = "Medium",
    ) -> dict[str, Any]:
        """Create a Jira issue. Returns the created issue dict with 'key' and 'id'."""
        session = await self._ensure_session()
        url = f"{self._base_url}/rest/api/3/issue"
        payload: dict[str, Any] = {
            "fields": {
                "project": {"key": project},
                "summary": summary,
                "issuetype": {"name": issue_type},
                "priority": {"name": priority},
            }
        }
        if description:
            payload["fields"]["description"] = {
                "type": "doc",
                "version": 1,
                "content": [
                    {
                        "type": "paragraph",
                        "content": [{"type": "text", "text": description}],
                    }
                ],
            }
        async with session.post(url, json=payload) as resp:
            resp.raise_for_status()
            return await resp.json()


class JiraSyncService:
    """Two-way sync between Jira and Swarm's task board."""

    def __init__(
        self,
        config: JiraConfig,
        token_manager: JiraTokenManager | None = None,
    ) -> None:
        self._config = config
        self._token_manager = token_manager
        self.client = JiraClient(config, token_manager)
        self.stats = JiraSyncStats()
        self._running = False

    @property
    def enabled(self) -> bool:
        return (
            self._config.enabled
            and self._token_manager is not None
            and self._token_manager.is_connected()
        )

    async def close(self) -> None:
        self._running = False
        await self.client.close()

    # --- Import: Jira → Swarm ---

    def build_jql(self) -> str:
        """Build the JQL query string for importing issues."""
        jql = self._config.import_filter
        if not jql and self._config.project:
            jql = f"project = {self._config.project}"
        # Apply lookback window when there's no explicit filter
        lookback = self._config.lookback_days
        if not jql and not self._config.import_label:
            if lookback > 0:
                jql = f"created >= -{lookback}d"
        # Strip any ORDER BY from the filter so we can safely append
        # clauses and re-add it at the very end.
        order_by = ""
        if jql:
            m = re.search(r"\s+ORDER\s+BY\s+.+$", jql, re.IGNORECASE)
            if m:
                order_by = m.group(0)
                jql = jql[: m.start()]
        # Include label in JQL for server-side filtering; client-side
        # filter remains as a case-insensitive safety net.
        if self._config.import_label and "labels" not in (jql or "").lower():
            label_clause = f'labels = "{self._config.import_label}"'
            jql = f"{label_clause} AND {jql}" if jql else label_clause
        # Always exclude completed issues unless the user's custom filter
        # already handles statusCategory.
        if "statuscategory" not in (jql or "").lower():
            done_clause = "statusCategory != Done"
            jql = f"{jql} AND {done_clause}" if jql else done_clause
        if not order_by:
            order_by = " ORDER BY created DESC"
        jql += order_by
        return jql

    async def import_issues(self, existing_tasks: dict[str, SwarmTask]) -> list[SwarmTask]:
        """Fetch issues from Jira and return new SwarmTasks to create.

        Deduplicates by checking ``jira_key`` against existing tasks.
        """
        if not self.enabled:
            return []

        jql = self.build_jql()

        try:
            issues = await self.client.search_issues(jql)
        except (aiohttp.ClientError, TimeoutError) as e:
            self.stats.last_error = str(e)
            self.stats.errors += 1
            _log.warning("Jira import failed: %s", e)
            return []

        # Build set of existing jira_keys for dedup
        known_keys = {t.jira_key for t in existing_tasks.values() if t.jira_key}

        # Optional case-insensitive label filter (client-side)
        label_filter = self._config.import_label.lower() if self._config.import_label else ""

        new_tasks: list[SwarmTask] = []
        for issue in issues:
            key = issue.get("key", "")
            if not key or key in known_keys:
                continue

            # Apply label filter case-insensitively
            if label_filter:
                issue_labels = [lbl.lower() for lbl in issue.get("fields", {}).get("labels", [])]
                if label_filter not in issue_labels:
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

    async def post_completion_comment(self, task: SwarmTask) -> bool:
        """Post a completion summary as a Jira comment.

        The comment includes a non-technical summary (task title) for end
        users and the full technical resolution for developers.
        """
        if not self.enabled or not task.jira_key:
            return False

        parts = ["*Task completed in Swarm.*"]
        if task.title:
            parts.append(f"*Summary:* {task.title} — done.")
        if task.assigned_worker:
            parts.append(f"*Worker:* {task.assigned_worker}")
        if task.resolution:
            parts.append(f"\n----\n*Technical Resolution:*\n{task.resolution}")

        body = "\n".join(parts)

        try:
            ok = await self.client.add_comment(task.jira_key, body)
            if ok:
                _log.info("posted completion comment on %s", task.jira_key)
            return ok
        except (aiohttp.ClientError, TimeoutError) as e:
            self.stats.last_error = str(e)
            self.stats.errors += 1
            _log.warning(
                "failed to comment on %s: %s",
                task.jira_key,
                e,
            )
            return False

    async def assign_to_me(self, task: SwarmTask) -> bool:
        """Assign a Jira issue to the authenticated user."""
        if not self.enabled or not task.jira_key:
            return False

        account_id = self._token_manager.account_id if self._token_manager else ""
        if not account_id:
            _log.warning("cannot assign %s — no account_id available", task.jira_key)
            return False

        try:
            ok = await self.client.assign_issue(task.jira_key, account_id)
            if ok:
                _log.info("assigned %s to current user", task.jira_key)
            return ok
        except (aiohttp.ClientError, TimeoutError) as e:
            self.stats.last_error = str(e)
            self.stats.errors += 1
            _log.warning("failed to assign %s: %s", task.jira_key, e)
            return False

    async def create_jira_issue(self, task: SwarmTask) -> str:
        """Create a Jira issue from a Swarm task. Returns the Jira key.

        Raises RuntimeError if Jira is not enabled.
        """
        if not self.enabled:
            raise RuntimeError("Jira integration is not enabled")

        issue_type = _SWARM_TYPE_TO_JIRA.get(task.task_type, "Task")
        priority = _SWARM_PRIORITY_TO_JIRA.get(task.priority, "Medium")

        result = await self.client.create_issue(
            project=self._config.project,
            summary=task.title,
            description=task.description,
            issue_type=issue_type,
            priority=priority,
        )
        key = result.get("key", "")
        if key:
            self.stats.total_exported += 1
            _log.info("created Jira issue %s from task %s", key, task.id[:8])
        return key

    def get_status(self) -> dict[str, Any]:
        """Return sync status for API/WS."""
        return {
            "enabled": self.enabled,
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


def _extract_text(adf: str | dict[str, object]) -> str:
    """Extract plain text from an ADF document or plain string."""
    if isinstance(adf, str):
        return adf
    if not isinstance(adf, dict):
        return ""

    parts: list[str] = []

    def _walk(node: dict[str, object] | list[object] | object) -> None:
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
