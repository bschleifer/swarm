"""Jira integration routes — status, sync, preview, create."""

from __future__ import annotations

from aiohttp import web

from swarm.logging import get_logger
from swarm.server.helpers import get_daemon, json_error

_log = get_logger("server.routes.jira")


def register(app: web.Application) -> None:
    app.router.add_get("/api/jira/status", handle_jira_status)
    app.router.add_post("/api/jira/sync", handle_jira_sync)
    app.router.add_get("/api/jira/preview", handle_jira_preview)
    app.router.add_post("/api/tasks/{task_id}/jira", handle_jira_create)


async def handle_jira_status(request: web.Request) -> web.Response:
    """Return Jira sync service status."""
    d = get_daemon(request)
    jira = getattr(d, "jira", None)
    if jira is None:
        return web.json_response({"enabled": False})
    return web.json_response(jira.get_status())


async def handle_jira_sync(request: web.Request) -> web.Response:
    """Trigger a manual Jira import sync."""
    d = get_daemon(request)
    jira = getattr(d, "jira", None)
    if jira is None or not jira.enabled:
        return json_error("Jira integration not enabled", status=400)
    count = await d._run_jira_import()
    return web.json_response({"imported": count})


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
