"""Unified event stream — the Command Center running log.

Aggregates operator-meaningful events from existing tables at read time
(no new event table). Sources:

* ``buzz_log`` — drone actions, queen decisions, tool approvals, worker
  state transitions. The original existing log; here it's chunked into
  category-specific event types.
* ``task_history`` — task lifecycle (created, assigned, completed,
  reassigned, reopened, failed).
* ``messages`` — peer-to-peer worker chatter (messages to ``queen`` are
  served by the Attention queue endpoint; we surface non-queen messages
  here under the ``peer_message`` category).
* ``queen_threads`` — Attention items as they land (kind filter).

Each event has a uniform shape so the dashboard can render them in one
list. Filter chips in the UI map to the ``category`` field.
"""

from __future__ import annotations

from typing import Any

from aiohttp import web

from swarm.logging import get_logger
from swarm.server.helpers import get_daemon, handle_errors

_log = get_logger("server.routes.events")

DEFAULT_CATEGORIES = ("attention", "task", "worker_state", "queen", "ship")
ALL_CATEGORIES = (
    "attention",
    "task",
    "worker_state",
    "queen",
    "peer_message",
    "drone",
    "tool_approval",
    "ship",
)

# buzz_log.category → our event category. The native "task" / "queen" /
# "drone" categories map almost 1:1; "worker" → "worker_state".
_BUZZ_CATEGORY_MAP = {
    "drone": "drone",
    "task": "task",
    "queen": "queen",
    "worker": "worker_state",
    "operator": "drone",
    "message": "peer_message",
}

_ATTENTION_KINDS = (
    "worker-message",
    "queen-escalation",
    "escalation",
    "oversight",
    "proposal",
    "anomaly",
)


def register(app: web.Application) -> None:
    app.router.add_get("/api/events", handle_list_events)


@handle_errors
async def handle_list_events(request: web.Request) -> web.Response:
    d = get_daemon(request)
    categories = _parse_categories(request.query.get("categories"))
    try:
        limit = min(int(request.query.get("limit", "100")), 500)
    except ValueError:
        limit = 100
    try:
        before = float(request.query["before"]) if request.query.get("before") else None
    except ValueError:
        before = None

    db = getattr(d, "swarm_db", None)
    if db is None:
        return web.json_response({"events": []})

    events: list[dict[str, Any]] = []
    if "attention" in categories:
        events.extend(_attention_events(d, limit, before))
    if any(c in categories for c in ("task", "ship")):
        events.extend(_task_events(db, limit, before, include_ship="ship" in categories))
    if any(
        c in categories for c in ("queen", "drone", "worker_state", "tool_approval", "peer_message")
    ):
        events.extend(_buzz_events(db, categories, limit, before))
    if "peer_message" in categories:
        events.extend(_message_events(db, limit, before))

    events.sort(key=lambda e: e["ts"], reverse=True)
    return web.json_response({"events": events[:limit]})


def _parse_categories(raw: str | None) -> set[str]:
    if not raw:
        return set(DEFAULT_CATEGORIES)
    parts = {p.strip() for p in raw.split(",") if p.strip()}
    return parts & set(ALL_CATEGORIES) or set(DEFAULT_CATEGORIES)


def _attention_events(d: Any, limit: int, before: float | None) -> list[dict[str, Any]]:
    chat = getattr(d, "queen_chat", None)
    if chat is None:
        return []
    try:
        threads = chat.list_threads(status="active", limit=limit)
    except Exception:
        return []
    out: list[dict[str, Any]] = []
    for t in threads:
        if t.kind not in _ATTENTION_KINDS:
            continue
        if before is not None and t.updated_at >= before:
            continue
        out.append(
            {
                "id": f"thread:{t.id}",
                "ts": t.updated_at,
                "category": "attention",
                "worker": t.worker_name,
                "title": t.title or f"{t.kind} from {t.worker_name or 'queen'}",
                "detail": "",
                "ref": {"thread_id": t.id, "kind": t.kind, "task_id": t.task_id},
            }
        )
    return out


def _task_events(
    db: Any, limit: int, before: float | None, include_ship: bool
) -> list[dict[str, Any]]:
    where = ""
    params: list[Any] = []
    if before is not None:
        where = "WHERE h.created_at < ?"
        params.append(before)
    params.append(limit * 2)
    sql = f"""
        SELECT h.id AS h_id, h.task_id, h.action, h.actor, h.detail, h.created_at,
               t.number, t.title
          FROM task_history h
          LEFT JOIN tasks t ON t.id = h.task_id
          {where}
         ORDER BY h.created_at DESC
         LIMIT ?
    """
    try:
        rows = db.fetchall(sql, tuple(params))
    except Exception:
        return []
    out: list[dict[str, Any]] = []
    for r in rows:
        action = (r["action"] or "").upper()
        if action == "COMPLETED":
            category = "ship" if include_ship else "task"
        else:
            category = "task"
        if category not in ("task", "ship"):
            continue
        title = r["title"] or f"task {r['task_id']}"
        n = r["number"]
        prefix = f"#{n}" if n is not None else "task"
        out.append(
            {
                "id": f"task_history:{r['h_id']}",
                "ts": r["created_at"],
                "category": category,
                "worker": r["actor"],
                "title": f"{prefix} {action.lower()}: {title}",
                "detail": r["detail"] or "",
                "ref": {"task_id": r["task_id"], "task_number": n},
            }
        )
    return out


def _buzz_events(
    db: Any, categories: set[str], limit: int, before: float | None
) -> list[dict[str, Any]]:
    where = ""
    params: list[Any] = []
    if before is not None:
        where = "WHERE timestamp < ?"
        params.append(before)
    params.append(limit * 3)
    sql = f"""
        SELECT id, timestamp, action, worker_name, detail, category
          FROM buzz_log
          {where}
         ORDER BY timestamp DESC
         LIMIT ?
    """
    try:
        rows = db.fetchall(sql, tuple(params))
    except Exception:
        return []
    out: list[dict[str, Any]] = []
    for r in rows:
        buzz_cat = r["category"] or "drone"
        event_cat = _BUZZ_CATEGORY_MAP.get(buzz_cat, "drone")
        # Tool approvals live in `drone` category in buzz_log but action
        # names start with TOOL_ or PRE_TOOL_ — split them out so the
        # filter chip works.
        action = (r["action"] or "").upper()
        if action.startswith("TOOL_") or action.startswith("PRE_TOOL"):
            event_cat = "tool_approval"
        if event_cat not in categories:
            continue
        out.append(
            {
                "id": f"buzz:{r['id']}",
                "ts": r["timestamp"],
                "category": event_cat,
                "worker": r["worker_name"],
                "title": f"{action.replace('_', ' ').lower()}: {(r['detail'] or '')[:120]}",
                "detail": r["detail"] or "",
                "ref": {"action": action, "buzz_id": r["id"]},
            }
        )
    return out


def _message_events(db: Any, limit: int, before: float | None) -> list[dict[str, Any]]:
    where = "WHERE recipient != 'queen' AND sender != 'operator'"
    params: list[Any] = []
    if before is not None:
        where += " AND created_at < ?"
        params.append(before)
    params.append(limit)
    sql = f"""
        SELECT id, sender, recipient, msg_type, content, created_at
          FROM messages
          {where}
         ORDER BY created_at DESC
         LIMIT ?
    """
    try:
        rows = db.fetchall(sql, tuple(params))
    except Exception:
        return []
    out: list[dict[str, Any]] = []
    for r in rows:
        preview = (r["content"] or "")[:120].replace("\n", " ")
        out.append(
            {
                "id": f"msg:{r['id']}",
                "ts": r["created_at"],
                "category": "peer_message",
                "worker": r["sender"],
                "title": f"{r['sender']} → {r['recipient']} [{r['msg_type']}]: {preview}",
                "detail": r["content"] or "",
                "ref": {
                    "msg_id": r["id"],
                    "sender": r["sender"],
                    "recipient": r["recipient"],
                },
            }
        )
    return out
