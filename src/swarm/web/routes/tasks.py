"""Task action routes: create, assign, complete, remove, fail, reopen, etc."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

import aiohttp
from aiohttp import web

from swarm.server.daemon import console_log
from swarm.server.helpers import get_daemon, json_error
from swarm.tasks.task import (
    PRIORITY_MAP,
    TYPE_MAP,
    TaskPriority,
    smart_title,
)
from swarm.web.app import handle_swarm_errors

if TYPE_CHECKING:
    from types import ModuleType

    from swarm.server.daemon import SwarmDaemon


@handle_swarm_errors
async def handle_action_create_task(request: web.Request) -> web.Response:
    d = get_daemon(request)
    data = await request.post()

    priority = PRIORITY_MAP.get(data.get("priority", "normal"), TaskPriority.NORMAL)
    type_str = data.get("task_type", "").strip()
    task_type = TYPE_MAP.get(type_str) if type_str else None

    deps_raw = data.get("depends_on", "").strip()
    depends_on = [x.strip() for x in deps_raw.split(",") if x.strip()] if deps_raw else None

    att_raw = data.get("attachments", "").strip()
    attachments = [a.strip() for a in att_raw.split(",") if a.strip()] if att_raw else None

    task = await d.create_task_smart(
        title=data.get("title", "").strip(),
        description=data.get("description", ""),
        priority=priority,
        task_type=task_type,
        depends_on=depends_on,
        attachments=attachments,
        source_email_id=data.get("source_email_id", "").strip(),
    )
    console_log(f'Task created: "{task.title}" ({task.priority.value}, {task.task_type.value})')
    return web.json_response({"id": task.id, "title": task.title}, status=201)


@handle_swarm_errors
async def handle_action_assign_task(request: web.Request) -> web.Response:
    d = get_daemon(request)
    data = await request.post()
    task_id = data.get("task_id", "")
    worker_name = data.get("worker", "")
    if not task_id or not worker_name:
        return json_error("task_id and worker required")

    await d.assign_task(task_id, worker_name)
    console_log(f'Task assigned to "{worker_name}"')
    return web.json_response({"status": "assigned", "task_id": task_id, "worker": worker_name})


@handle_swarm_errors
async def handle_action_complete_task(request: web.Request) -> web.Response:
    d = get_daemon(request)
    data = await request.post()
    task_id = data.get("task_id", "")
    resolution = data.get("resolution", "").strip()
    if not task_id:
        return json_error("task_id required")

    d.complete_task(task_id, resolution=resolution)
    console_log(f"Task completed: {task_id[:8]}")
    return web.json_response({"status": "completed", "task_id": task_id})


@handle_swarm_errors
async def handle_action_remove_task(request: web.Request) -> web.Response:
    d = get_daemon(request)
    data = await request.post()
    task_id = data.get("task_id", "")
    if not task_id:
        return json_error("task_id required")

    d.remove_task(task_id)
    console_log(f"Task removed: {task_id[:8]}")
    return web.json_response({"status": "removed", "task_id": task_id})


@handle_swarm_errors
async def handle_action_fail_task(request: web.Request) -> web.Response:
    d = get_daemon(request)
    data = await request.post()
    task_id = data.get("task_id", "")
    if not task_id:
        return json_error("task_id required")

    d.fail_task(task_id)
    console_log(f"Task failed: {task_id[:8]}", level="warn")
    return web.json_response({"status": "failed", "task_id": task_id})


@handle_swarm_errors
async def handle_action_reopen_task(request: web.Request) -> web.Response:
    d = get_daemon(request)
    data = await request.post()
    task_id = data.get("task_id", "")
    if not task_id:
        return json_error("task_id required")

    d.reopen_task(task_id)
    console_log(f"Task reopened: {task_id[:8]}")
    return web.json_response({"status": "reopened", "task_id": task_id})


@handle_swarm_errors
async def handle_action_unassign_task(request: web.Request) -> web.Response:
    d = get_daemon(request)
    data = await request.post()
    task_id = data.get("task_id", "")
    if not task_id:
        return json_error("task_id required")

    d.unassign_task(task_id)
    console_log(f"Task unassigned: {task_id[:8]}")
    return web.json_response({"status": "unassigned", "task_id": task_id})


def _parse_cross_task_fields(
    data: aiohttp.MultiDict,
    kwargs: dict[str, Any],
) -> None:
    """Extract cross-project fields from form data into *kwargs*."""
    if "source_worker" in data:
        kwargs["source_worker"] = data["source_worker"].strip()
    if "target_worker" in data:
        kwargs["target_worker"] = data["target_worker"].strip()
    if "dependency_type" in data:
        dep_type = data["dependency_type"].strip()
        if dep_type in ("blocks", "enhances", "enables"):
            kwargs["dependency_type"] = dep_type
    if "acceptance_criteria" in data:
        raw = data["acceptance_criteria"].strip()
        kwargs["acceptance_criteria"] = (
            [line.strip() for line in raw.splitlines() if line.strip()] if raw else []
        )
    if "context_refs" in data:
        raw = data["context_refs"].strip()
        kwargs["context_refs"] = [r.strip() for r in raw.split(",") if r.strip()] if raw else []


@handle_swarm_errors
async def handle_action_edit_task(request: web.Request) -> web.Response:
    d = get_daemon(request)
    data = await request.post()
    task_id = data.get("task_id", "")
    if not task_id:
        return json_error("task_id required")

    kwargs: dict[str, Any] = {}
    title = data.get("title", "").strip()
    desc = data.get("description")
    if title:
        kwargs["title"] = title
    elif "title" in data and desc:
        # Title cleared -- regenerate from description
        kwargs["title"] = await smart_title(desc)
    if desc is not None:
        kwargs["description"] = desc
    if data.get("priority") and data["priority"] in PRIORITY_MAP:
        kwargs["priority"] = PRIORITY_MAP[data["priority"]]
    if data.get("task_type") and data["task_type"] in TYPE_MAP:
        kwargs["task_type"] = TYPE_MAP[data["task_type"]]
    tags_raw = data.get("tags", "").strip()
    if tags_raw:
        kwargs["tags"] = [t.strip() for t in tags_raw.split(",") if t.strip()]
    deps_raw = data.get("depends_on", "")
    if deps_raw:
        kwargs["depends_on"] = [x.strip() for x in deps_raw.strip().split(",") if x.strip()]
    _parse_cross_task_fields(data, kwargs)

    d.edit_task(task_id, **kwargs)
    console_log(f"Task edited: {task_id[:8]}")
    return web.json_response({"status": "updated", "task_id": task_id})


async def handle_action_upload_attachment(request: web.Request) -> web.Response:
    d = get_daemon(request)
    reader = await request.multipart()

    task_id = None
    file_data = None
    file_name = "upload"

    while True:
        field = await reader.next()
        if field is None:
            break
        if field.name == "task_id":
            task_id = (await field.text()).strip()
        elif field.name == "file":
            file_name = field.filename or "upload"
            file_data = await field.read(decode=False)

    if not task_id or file_data is None:
        return json_error("task_id and file required")

    task = d.task_board.get(task_id)
    if not task:
        return json_error(f"Task '{task_id}' not found", 404)

    path = d.save_attachment(file_name, file_data)
    new_attachments = [*task.attachments, path]
    d.task_board.update(task_id, attachments=new_attachments)

    console_log(f"Attachment uploaded: {file_name}")
    return web.json_response({"status": "uploaded", "path": path}, status=201)


async def handle_action_upload(request: web.Request) -> web.Response:
    """Upload a file and return its absolute server path."""
    d = get_daemon(request)
    reader = await request.multipart()

    file_data = None
    file_name = "upload"

    while True:
        field = await reader.next()
        if field is None:
            break
        if field.name == "file":
            file_name = field.filename or "upload"
            file_data = await field.read(decode=False)

    if file_data is None:
        return json_error("file required")

    path = d.save_attachment(file_name, file_data)
    console_log(f"File uploaded: {file_name} → {path}")
    return web.json_response({"path": path}, status=201)


async def handle_action_fetch_outlook_email(request: web.Request) -> web.Response:
    """Fetch an email from Microsoft Graph API using a message ID."""
    d = get_daemon(request)
    data = await request.post()
    message_id = data.get("message_id", "").strip()
    if not message_id:
        return json_error("message_id required")

    # Check if Graph API is configured and connected
    if not d.graph_mgr:
        return json_error("Microsoft Graph not configured")

    graph_token = await d.graph_mgr.get_token()
    if not graph_token:
        return json_error("Microsoft Graph not connected — authenticate first")

    console_log(f"Fetching email via Graph: {message_id[:30]}...")
    return await _fetch_graph_email(d, message_id, graph_token)


async def _translate_exchange_id(
    sess: aiohttp.ClientSession, headers: dict[str, str], ews_id: str
) -> str | None:
    """Translate an EWS-format message ID to REST format via Graph API."""
    url = "https://graph.microsoft.com/v1.0/me/translateExchangeIds"
    payload = {
        "inputIds": [ews_id],
        "sourceIdType": "ewsId",
        "targetIdType": "restId",
    }
    try:
        async with sess.post(url, headers=headers, json=payload) as resp:
            if resp.status != 200:
                err = await resp.text()
                console_log(f"translateExchangeIds failed ({resp.status}): {err[:200]}")
                return None
            data = await resp.json()
            values = data.get("value", [])
            if values:
                return values[0].get("targetId")
    except Exception as exc:
        console_log(f"translateExchangeIds error: {exc}")
    return None


async def _fetch_attachment_bytes(
    sess: aiohttp.ClientSession,
    headers: dict[str, str],
    msg_id: str,
    att_id: str,
    quote: Callable[..., str],
    yarl: ModuleType,
) -> str:
    """Fetch a single attachment's contentBytes from Graph API."""
    import aiohttp as _aiohttp

    encoded_msg = quote(msg_id, safe="")
    encoded_att = quote(att_id, safe="")
    url = yarl.URL(
        f"https://graph.microsoft.com/v1.0/me/messages/{encoded_msg}/attachments/{encoded_att}",
        encoded=True,
    )
    try:
        async with sess.get(url, headers=headers, timeout=_aiohttp.ClientTimeout(total=30)) as resp:
            if resp.status != 200:
                console_log(f"Attachment fetch failed ({resp.status})")
                return ""
            data = await resp.json()
            return data.get("contentBytes", "")
    except Exception as exc:
        console_log(f"Attachment fetch error: {exc}")
        return ""


async def _fetch_graph_email(d: SwarmDaemon, message_id: str, token: str) -> web.Response:
    """Fetch email + attachments from Microsoft Graph API."""
    from urllib.parse import quote

    import aiohttp as _aiohttp
    import yarl

    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    base = "https://graph.microsoft.com/v1.0/me/messages"

    async def _graph_get(sess: _aiohttp.ClientSession, mid: str) -> tuple[int, str]:
        encoded = quote(mid, safe="")
        url = yarl.URL(f"{base}/{encoded}?$expand=attachments", encoded=True)
        console_log(f"Graph GET: {str(url)[:120]}...")
        async with sess.get(url, headers=headers, timeout=_aiohttp.ClientTimeout(total=15)) as resp:
            return resp.status, await resp.text()

    import json as _json

    try:
        async with _aiohttp.ClientSession() as sess:
            effective_id = message_id
            status, body = await _graph_get(sess, message_id)

            # If 400/404, the ID may be in EWS format -- translate to REST format
            if status in (400, 404) and ("/" in message_id or "+" in message_id):
                console_log("Direct fetch failed; translating EWS ID to REST format...")
                rest_id = await _translate_exchange_id(sess, headers, message_id)
                if rest_id and rest_id != message_id:
                    effective_id = rest_id
                    status, body = await _graph_get(sess, rest_id)

            if status != 200:
                console_log(f"Graph API error {status}: {body[:200]}", level="error")
                return json_error(f"Graph API {status}: {body[:200]}")

            msg = _json.loads(body)

            # Individually fetch attachment bytes if missing (Graph sometimes omits them)
            attachments = msg.get("attachments", [])
            for att in attachments:
                if (
                    att.get("@odata.type") == "#microsoft.graph.fileAttachment"
                    and not att.get("contentBytes")
                    and att.get("id")
                ):
                    att["contentBytes"] = await _fetch_attachment_bytes(
                        sess, headers, effective_id, att["id"], quote, yarl
                    )
    except Exception as exc:
        return json_error(str(exc)[:200])

    # Delegate business logic (HTML->text, title gen, type classification) to daemon
    result = await d.process_email_data(
        subject=msg.get("subject", ""),
        body_content=msg.get("body", {}).get("content", ""),
        body_type=msg.get("body", {}).get("contentType", "text"),
        attachment_dicts=attachments,
        effective_id=effective_id,
    )
    return web.json_response(result)


@handle_swarm_errors
async def handle_action_fetch_image(request: web.Request) -> web.Response:
    """Fetch an external image URL and save it as an attachment."""
    d = get_daemon(request)
    data = await request.post()
    url = data.get("url", "").strip()
    if not url:
        return json_error("url required")

    path = await d.fetch_and_save_image(url)
    return web.json_response({"path": path}, status=201)


@handle_swarm_errors
async def handle_action_retry_draft(request: web.Request) -> web.Response:
    d = get_daemon(request)
    data = await request.post()
    task_id = data.get("task_id", "")
    if not task_id:
        return json_error("task_id required")

    await d.retry_draft_reply(task_id)
    console_log(f"Retrying draft reply for task {task_id[:8]}")
    return web.json_response({"status": "retrying", "task_id": task_id})


def register(app: web.Application) -> None:
    """Register task action routes."""
    app.router.add_post("/action/task/create", handle_action_create_task)
    app.router.add_post("/action/task/assign", handle_action_assign_task)
    app.router.add_post("/action/task/complete", handle_action_complete_task)
    app.router.add_post("/action/task/remove", handle_action_remove_task)
    app.router.add_post("/action/task/fail", handle_action_fail_task)
    app.router.add_post("/action/task/unassign", handle_action_unassign_task)
    app.router.add_post("/action/task/reopen", handle_action_reopen_task)
    app.router.add_post("/action/task/edit", handle_action_edit_task)
    app.router.add_post("/action/task/upload", handle_action_upload_attachment)
    app.router.add_post("/action/upload", handle_action_upload)
    app.router.add_post("/action/fetch-image", handle_action_fetch_image)
    app.router.add_post("/action/fetch-outlook-email", handle_action_fetch_outlook_email)
    app.router.add_post("/action/task/retry-draft", handle_action_retry_draft)
