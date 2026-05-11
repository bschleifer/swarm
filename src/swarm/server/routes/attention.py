"""Attention queue routes — the operator's must-act surface.

The Attention queue is a filtered view over ``queen_threads`` covering the
kinds the operator needs to act on:

* ``worker-message`` — a worker sent a message to ``to="queen"`` (mailbox)
* ``queen-escalation`` — headless Queen explicitly flagged operator review
* ``escalation`` / ``oversight`` / ``proposal`` / ``anomaly`` — drone-driven
  escalations from the existing thread producers

``operator`` and ``operator-question`` threads (Ask Queen) are **not** in
the Attention queue — they're operator-initiated conversations, not items
that need attention.

The ``reply`` verb is what makes Attention distinct from the existing
``queen.thread`` routes: it (a) appends an operator message to the thread,
(b) sends a real ``queen → worker`` message via ``message_store`` (so the
worker's PTY actually gets a reply), and (c) resolves the thread. Closes
the loop in one call without leaving the panel.
"""

from __future__ import annotations

from aiohttp import web

from swarm.logging import get_logger
from swarm.server.helpers import get_daemon, handle_errors, json_error
from swarm.server.routes.queen import _broadcast_message, _broadcast_thread

_log = get_logger("server.routes.attention")

ATTENTION_KINDS = (
    "worker-message",
    "queen-escalation",
    "escalation",
    "oversight",
    "proposal",
    "anomaly",
)

_MAX_BODY_LEN = 8000


def register(app: web.Application) -> None:
    app.router.add_get("/api/attention", handle_list_attention)
    app.router.add_post("/api/attention/{thread_id}/reply", handle_reply)
    app.router.add_post("/api/attention/{thread_id}/resolve", handle_resolve)


@handle_errors
async def handle_list_attention(request: web.Request) -> web.Response:
    d = get_daemon(request)
    try:
        limit = min(int(request.query.get("limit", "100")), 500)
    except ValueError:
        limit = 100
    chat = getattr(d, "queen_chat", None)
    if chat is None:
        return web.json_response({"threads": []})
    # No multi-kind filter on the store API — fetch active and filter here.
    threads = chat.list_threads(status="active", limit=limit)
    payload = [t.to_dict() for t in threads if t.kind in ATTENTION_KINDS]
    return web.json_response({"threads": payload})


@handle_errors
async def handle_reply(request: web.Request) -> web.Response:
    """Operator replies to a worker's Attention card → message + resolve."""
    d = get_daemon(request)
    thread_id = request.match_info["thread_id"]
    chat = getattr(d, "queen_chat", None)
    if chat is None:
        return json_error("queen_chat unavailable", 503)
    thread = chat.get_thread(thread_id)
    if thread is None:
        return json_error("thread not found", 404)
    if thread.status == "resolved":
        return json_error("thread already resolved", 409)
    try:
        data = await request.json()
    except Exception:
        return json_error("invalid JSON body", 400)
    body = str(data.get("body") or "").strip()
    if not body:
        return json_error("'body' is required", 400)
    if len(body) > _MAX_BODY_LEN:
        return json_error("body exceeds max length", 413)

    msg = chat.add_message(thread_id, role="operator", content=body, widgets=[])
    _broadcast_message(d, thread_id, msg.to_dict())

    sent_id, nudged = await _deliver_reply_to_worker(d, thread.worker_name, body)

    ok = chat.resolve_thread(thread_id, resolved_by="operator", reason="operator replied")
    if ok:
        _broadcast_thread(d, thread_id, "resolved")
    return web.json_response(
        {
            "message": msg.to_dict(),
            "delivered_to": thread.worker_name,
            "delivered_id": sent_id,
            "nudged": nudged,
        }
    )


async def _deliver_reply_to_worker(
    d: object, worker: str | None, body: str
) -> tuple[int | None, bool]:
    """Deliver an operator reply to a worker two ways: persist a row and
    inject a short prompt into the worker's PTY.

    Returns ``(sent_id, nudged)``.
    """
    if not worker:
        return None, False

    from swarm.worker.worker import QUEEN_WORKER_NAME

    sent_id: int | None = None
    store = getattr(d, "message_store", None)
    if store is not None:
        try:
            sent_id = store.send(QUEEN_WORKER_NAME, worker, "status", body)
        except Exception:
            _log.warning("attention reply: store.send failed", exc_info=True)

    worker_svc = getattr(d, "worker_svc", None)
    if worker_svc is None:
        return sent_id, False

    nudge = (
        f"[operator reply via Queen Dashboard] {body[:400]}\n"
        "Full thread: `swarm_check_messages`. Resume the blocked work."
    )
    try:
        await worker_svc.send_to_worker(worker, nudge, _log_operator=False)
        return sent_id, True
    except Exception:
        _log.warning("attention reply: send_to_worker(%s) failed", worker, exc_info=True)
        return sent_id, False


@handle_errors
async def handle_resolve(request: web.Request) -> web.Response:
    """Dismiss an Attention card without sending a reply."""
    d = get_daemon(request)
    thread_id = request.match_info["thread_id"]
    chat = getattr(d, "queen_chat", None)
    if chat is None:
        return json_error("queen_chat unavailable", 503)
    try:
        data = await request.json() if request.body_exists else {}
    except Exception:
        data = {}
    reason = str((data or {}).get("reason") or "").strip() or "operator dismissed"
    ok = chat.resolve_thread(thread_id, resolved_by="operator", reason=reason)
    if not ok:
        return json_error("thread not found or already resolved", 404)
    _broadcast_thread(d, thread_id, "resolved")
    return web.json_response({"resolved": True})
