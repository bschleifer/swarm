"""Jira integration routes — status, sync, preview, create."""

from __future__ import annotations

from aiohttp import web

from swarm.logging import get_logger
from swarm.server.helpers import get_daemon, handle_errors, json_error

_log = get_logger("server.routes.jira")


def register(app: web.Application) -> None:
    app.router.add_get("/api/jira/status", handle_jira_status)
    app.router.add_post("/api/jira/sync", handle_jira_sync)
    app.router.add_post("/api/jira/import-by-key", handle_jira_import_by_key)
    app.router.add_get("/api/jira/preview", handle_jira_preview)
    app.router.add_post("/api/tasks/{task_id}/jira", handle_jira_create)
    app.router.add_post("/api/tasks/{task_id}/jira/refresh", handle_jira_refresh)


@handle_errors
async def handle_jira_status(request: web.Request) -> web.Response:
    """Return Jira sync service status."""
    d = get_daemon(request)
    jira = getattr(d, "jira", None)
    if jira is None:
        return web.json_response({"enabled": False})
    return web.json_response(jira.get_status())


@handle_errors
async def handle_jira_sync(request: web.Request) -> web.Response:
    """Trigger a manual Jira import sync."""
    d = get_daemon(request)
    jira = getattr(d, "jira", None)
    if jira is None or not jira.enabled:
        return json_error("Jira integration not enabled", status=400)
    count = await d.jira_svc.run_import()
    return web.json_response({"imported": count})


@handle_errors
async def handle_jira_import_by_key(request: web.Request) -> web.Response:
    """Import a single Jira issue by key. Used by drag-drop in the dashboard."""
    import re as _re

    d = get_daemon(request)
    jira = getattr(d, "jira", None)
    if jira is None or not jira.enabled:
        return json_error("Jira integration not enabled", status=400)

    data = await request.post()
    raw = (data.get("key") or "").strip()
    if not raw:
        return json_error("key required (e.g. PROJ-123 or full URL)")

    # Accept full URLs (https://foo.atlassian.net/browse/PROJ-123) or bare keys.
    match = _re.search(r"([A-Z][A-Z0-9_]+-\d+)", raw.upper())
    if not match:
        return json_error(f"could not parse Jira issue key from '{raw}'")
    issue_key = match.group(1)

    result = await d.jira_svc.import_one(issue_key)
    if not result:
        return json_error(f"Failed to import {issue_key}", status=502)
    return web.json_response(result)


@handle_errors
async def handle_jira_preview(request: web.Request) -> web.Response:
    """Preview what a Jira sync would import (dry run — no tasks created)."""
    d = get_daemon(request)
    jira = getattr(d, "jira", None)
    if jira is None:
        return json_error("Jira integration not configured", status=400)
    if not jira.enabled:
        connected = d.jira_mgr.is_connected() if d.jira_mgr else False
        return json_error(
            f"Jira not enabled (enabled={d.config.jira.enabled}, oauth_connected={connected})",
            status=400,
        )
    jql = jira.build_jql()
    existing = {t.id: t for t in d.task_board.all_tasks}
    prev_errors = jira.stats.errors
    new_tasks = await jira.import_issues(existing)
    preview = [
        {
            "jira_key": t.jira_key,
            "title": t.title,
            "type": t.task_type.value,
            "priority": t.priority.value,
        }
        for t in new_tasks
    ]
    result: dict[str, object] = {"count": len(preview), "tasks": preview, "jql": jql}
    if jira.stats.errors > prev_errors:
        result["error"] = jira.stats.last_error
        _log.warning("Jira preview error: %s (jql=%s)", jira.stats.last_error, jql)
    return web.json_response(result)


@handle_errors
async def handle_jira_refresh(request: web.Request) -> web.Response:
    """Pull the latest description, comments, and attachments from Jira."""
    d = get_daemon(request)
    task_id = request.match_info["task_id"]
    jira = getattr(d, "jira", None)
    if jira is None or not jira.enabled:
        return json_error("Jira integration not enabled", status=400)
    task = d.task_board.get(task_id)
    if not task:
        return json_error("Task not found", status=404)
    if not task.jira_key:
        return json_error("Task is not linked to a Jira issue", status=400)
    ok = await d.jira_svc.refresh_task(task_id)
    if not ok:
        return json_error("Failed to refresh task from Jira", status=502)
    refreshed = d.task_board.get(task_id)
    return web.json_response(
        {
            "task_id": task_id,
            "jira_key": task.jira_key,
            "attachments": list(refreshed.attachments) if refreshed else [],
            "description_length": len(refreshed.description) if refreshed else 0,
        }
    )


@handle_errors
async def handle_jira_create(request: web.Request) -> web.Response:
    """Create a Jira issue from an existing Swarm task."""
    d = get_daemon(request)
    task_id = request.match_info["task_id"]
    jira = getattr(d, "jira", None)
    if jira is None or not jira.enabled:
        return json_error("Jira integration not enabled", status=400)
    task = d.task_board.get(task_id)
    if not task:
        return json_error("Task not found", status=404)
    if task.jira_key:
        return json_error(f"Task already linked to {task.jira_key}", status=409)
    try:
        jira_key = await jira.create_jira_issue(task)
    except Exception as exc:
        return json_error(f"Failed to create Jira issue: {exc}", status=502)
    if jira_key:
        d.task_board.set_jira_key(task_id, jira_key)
        from swarm.tasks.history import TaskAction

        detail = f"linked to {jira_key}"
        d.task_history.append(task_id, TaskAction.EDITED, actor="user", detail=detail)
    return web.json_response({"jira_key": jira_key, "task_id": task_id})
