"""Task routes — CRUD, assignment, attachments, history."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from aiohttp import web

from swarm.server.daemon import SwarmOperationError
from swarm.server.helpers import (
    get_daemon,
    handle_errors,
    json_error,
    parse_limit,
    read_file_field,
)
from swarm.tasks.task import (
    TaskPriority,
    TaskType,
    validate_priority,
    validate_task_type,
)


def register(app: web.Application) -> None:
    app.router.add_get("/api/tasks", handle_tasks)
    app.router.add_post("/api/tasks", handle_create_task)
    app.router.add_post("/api/tasks/from-email", handle_create_task_from_email)
    app.router.add_post("/api/tasks/{task_id}/assign", handle_assign_task)
    app.router.add_post("/api/tasks/{task_id}/complete", handle_complete_task)
    app.router.add_post("/api/tasks/{task_id}/fail", handle_fail_task)
    app.router.add_post("/api/tasks/{task_id}/unassign", handle_unassign_task)
    app.router.add_post("/api/tasks/{task_id}/reopen", handle_reopen_task)
    app.router.add_delete("/api/tasks/{task_id}", handle_remove_task)
    app.router.add_patch("/api/tasks/{task_id}", handle_edit_task)
    app.router.add_post("/api/tasks/{task_id}/attachments", handle_upload_attachment)
    app.router.add_post("/api/tasks/{task_id}/retry-draft", handle_retry_draft)
    app.router.add_get("/api/tasks/{task_id}/history", handle_task_history)


def _validate_priority(raw: str) -> TaskPriority:
    try:
        return validate_priority(raw)
    except ValueError as e:
        raise SwarmOperationError(str(e)) from e


def _validate_task_type(raw: str) -> TaskType:
    try:
        return validate_task_type(raw)
    except ValueError as e:
        raise SwarmOperationError(str(e)) from e


def _validate_edit_body(body: dict[str, Any]) -> web.Response | None:
    """Return an error Response if edit body fields are invalid, else None."""
    if "title" in body:
        raw_title = body["title"]
        if isinstance(raw_title, str) and len(raw_title) > 500:
            return json_error("Task title too long (max 500 characters)")
    if "description" in body:
        desc = body["description"]
        if isinstance(desc, str) and len(desc) > 10_000:
            return json_error("Task description too long (max 10000 characters)")
    if "attachments" in body:
        uploads_dir = (Path.home() / ".swarm" / "uploads").resolve()
        for att in body["attachments"]:
            att_path = Path(att).resolve()
            if not att_path.is_relative_to(uploads_dir):
                return json_error("attachment path outside uploads directory", 400)
    return None


async def handle_tasks(request: web.Request) -> web.Response:
    d = get_daemon(request)
    tasks = d.task_board.all_tasks
    return web.json_response(
        {
            "tasks": [
                {
                    "id": t.id,
                    "title": t.title,
                    "description": t.description,
                    "status": t.status.value,
                    "priority": t.priority.value,
                    "task_type": t.task_type.value,
                    "assigned_worker": t.assigned_worker,
                }
                for t in tasks
            ],
            "summary": d.task_board.summary(),
        }
    )


@handle_errors
async def handle_create_task(request: web.Request) -> web.Response:
    d = get_daemon(request)
    body = await request.json()
    title = body.get("title", "")
    if not isinstance(title, str):
        title = ""
    title = title.strip()
    if len(title) > 500:
        return json_error("Task title too long (max 500 characters)")
    description = body.get("description", "")
    if isinstance(description, str) and len(description) > 10_000:
        return json_error("Task description too long (max 10000 characters)")

    priority = _validate_priority(body.get("priority", "normal"))

    type_str = body.get("task_type", "")
    task_type = None
    if type_str:
        task_type = _validate_task_type(type_str)

    task = await d.create_task_smart(
        title=title,
        description=description,
        priority=priority,
        task_type=task_type,
    )
    return web.json_response({"id": task.id, "title": task.title}, status=201)


async def handle_create_task_from_email(request: web.Request) -> web.Response:
    """Parse a .eml file and return extracted data for the create-task modal."""
    d = get_daemon(request)
    try:
        filename, data = await read_file_field(request)
    except ValueError as e:
        return json_error(str(e))

    from swarm.tasks.task import parse_email, smart_title

    parsed = parse_email(data, filename=filename)
    subject = parsed.get("subject", "")
    body = parsed.get("body", "")

    title = subject.strip()
    if not title and body:
        title = await smart_title(body)

    attachment_paths: list[str] = []
    for att in parsed.get("attachments", []):
        path = d.save_attachment(att["filename"], att["data"])
        attachment_paths.append(path)

    return web.json_response(
        {
            "title": title or "",
            "description": body or "",
            "attachments": attachment_paths,
            "message_id": parsed.get("message_id", ""),
        }
    )


@handle_errors
async def handle_assign_task(request: web.Request) -> web.Response:
    d = get_daemon(request)
    task_id = request.match_info["task_id"]
    body = await request.json()
    worker_name = body.get("worker", "")
    if not worker_name:
        return json_error("worker required")

    await d.assign_task(task_id, worker_name)
    return web.json_response({"status": "assigned", "task_id": task_id, "worker": worker_name})


@handle_errors
async def handle_complete_task(request: web.Request) -> web.Response:
    d = get_daemon(request)
    task_id = request.match_info["task_id"]
    body = await request.json() if request.can_read_body else {}
    resolution = body.get("resolution", "") if body else ""
    d.complete_task(task_id, resolution=resolution)
    return web.json_response({"status": "completed", "task_id": task_id})


@handle_errors
async def handle_fail_task(request: web.Request) -> web.Response:
    d = get_daemon(request)
    task_id = request.match_info["task_id"]
    d.fail_task(task_id)
    return web.json_response({"status": "failed", "task_id": task_id})


@handle_errors
async def handle_unassign_task(request: web.Request) -> web.Response:
    d = get_daemon(request)
    task_id = request.match_info["task_id"]
    d.unassign_task(task_id)
    return web.json_response({"status": "unassigned", "task_id": task_id})


@handle_errors
async def handle_reopen_task(request: web.Request) -> web.Response:
    d = get_daemon(request)
    task_id = request.match_info["task_id"]
    d.reopen_task(task_id)
    return web.json_response({"status": "reopened", "task_id": task_id})


@handle_errors
async def handle_remove_task(request: web.Request) -> web.Response:
    d = get_daemon(request)
    task_id = request.match_info["task_id"]
    d.remove_task(task_id)
    return web.json_response({"status": "removed", "task_id": task_id})


@handle_errors
async def handle_edit_task(request: web.Request) -> web.Response:
    d = get_daemon(request)
    task_id = request.match_info["task_id"]
    body = await request.json()

    err = _validate_edit_body(body)
    if err is not None:
        return err

    kwargs: dict[str, Any] = {}
    if "title" in body:
        title = await d.tasks.resolve_title(body["title"], body.get("description", ""), task_id)
        if not title:
            return json_error("title or description required to generate title")
        kwargs["title"] = title
    if "description" in body:
        kwargs["description"] = body["description"]
    if "priority" in body:
        kwargs["priority"] = _validate_priority(body["priority"])
    if "task_type" in body:
        kwargs["task_type"] = _validate_task_type(body["task_type"])
    if "tags" in body:
        kwargs["tags"] = body["tags"]
    if "attachments" in body:
        kwargs["attachments"] = body["attachments"]

    d.edit_task(task_id, **kwargs)
    return web.json_response({"status": "updated", "task_id": task_id})


@handle_errors
async def handle_upload_attachment(request: web.Request) -> web.Response:
    d = get_daemon(request)
    task_id = request.match_info["task_id"]

    task = d.task_board.get(task_id)
    if not task:
        return json_error(f"Task '{task_id}' not found", 404)

    filename, data = await read_file_field(request)
    path = d.save_attachment(filename, data)

    new_attachments = [*task.attachments, path]
    d.task_board.update(task_id, attachments=new_attachments)

    return web.json_response({"status": "uploaded", "path": path}, status=201)


async def handle_task_history(request: web.Request) -> web.Response:
    d = get_daemon(request)
    task_id = request.match_info["task_id"]
    limit = parse_limit(request)
    events = d.task_history.get_events(task_id, limit=limit)
    return web.json_response(
        {
            "events": [e.to_dict() for e in events],
        }
    )


@handle_errors
async def handle_retry_draft(request: web.Request) -> web.Response:
    d = get_daemon(request)
    task_id = request.match_info["task_id"]
    await d.retry_draft_reply(task_id)
    return web.json_response({"status": "retrying", "task_id": task_id})
