"""Drone routes — log, status, toggle, tuning, rules, notifications."""

from __future__ import annotations

import time

from aiohttp import web

from swarm.server.helpers import get_daemon, json_error, parse_limit


def register(app: web.Application) -> None:
    app.router.add_get("/api/drones/log", handle_drone_log)
    app.router.add_get("/api/drones/status", handle_drone_status)
    app.router.add_post("/api/drones/toggle", handle_drone_toggle)
    app.router.add_post("/api/drones/poll", handle_drones_poll)
    app.router.add_get("/api/drones/tuning", handle_tuning_suggestions)
    app.router.add_get("/api/drones/rules/analytics", handle_rule_analytics)
    app.router.add_post("/api/drones/rules/suggest", handle_rule_suggest)
    app.router.add_get("/api/notifications", handle_notification_history)
    app.router.add_get("/api/queen/oversight", handle_oversight_status)
    app.router.add_get("/api/coordination/ownership", handle_ownership_status)
    app.router.add_get("/api/coordination/sync", handle_sync_status)


async def handle_drone_log(request: web.Request) -> web.Response:
    d = get_daemon(request)
    limit = parse_limit(request)

    worker = request.query.get("worker")
    action = request.query.get("action")
    category = request.query.get("category")
    since_str = request.query.get("since")
    overridden_str = request.query.get("overridden")

    use_store = any([worker, action, category, since_str, overridden_str])

    if use_store and d.drone_log.store is not None:
        since = float(since_str) if since_str else None
        overridden = None
        if overridden_str is not None:
            overridden = overridden_str.lower() in ("true", "1", "yes")
        rows = d.drone_log.query(
            worker_name=worker,
            action=action.upper() if action else None,
            category=category,
            since=since,
            overridden=overridden,
            limit=limit,
        )
        return web.json_response({"entries": rows})

    entries = d.drone_log.entries[-limit:]
    return web.json_response(
        {
            "entries": [
                {
                    "time": e.formatted_time,
                    "action": e.action.value.lower(),
                    "worker": e.worker_name,
                    "detail": e.detail,
                }
                for e in entries
            ]
        }
    )


async def handle_tuning_suggestions(request: web.Request) -> web.Response:
    """Return auto-tuning suggestions based on override patterns."""
    from swarm.drones.tuning import analyze_overrides

    d = get_daemon(request)
    store = d.drone_log.store
    if store is None:
        return web.json_response({"suggestions": []})
    try:
        days = int(request.query.get("days", "7"))
    except ValueError:
        return json_error("Invalid 'days' parameter — must be an integer", 400)
    suggestions = analyze_overrides(store, days=days)
    return web.json_response(
        {
            "suggestions": [
                {
                    "id": s.id,
                    "description": s.description,
                    "config_path": s.config_path,
                    "current_value": s.current_value,
                    "suggested_value": s.suggested_value,
                    "reason": s.reason,
                    "override_count": s.override_count,
                    "total_decisions": s.total_decisions,
                    "override_rate": round(s.override_rate, 2),
                }
                for s in suggestions
            ]
        }
    )


async def handle_rule_analytics(request: web.Request) -> web.Response:
    """Return per-rule firing statistics from the decision log."""
    d = get_daemon(request)
    store = d.drone_log.store
    if store is None:
        return web.json_response({"analytics": [], "config_rules": []})

    try:
        days = int(request.query.get("days", "7"))
    except ValueError:
        return json_error("Invalid 'days' parameter — must be an integer", 400)
    since = time.time() - days * 86400
    analytics = store.rule_analytics(since=since)

    config_rules = [
        {"pattern": r.pattern, "action": r.action} for r in d.config.drones.approval_rules
    ]

    return web.json_response({"analytics": analytics, "config_rules": config_rules})


async def handle_rule_suggest(request: web.Request) -> web.Response:
    """Suggest a drone approval rule pattern from log detail strings."""
    from swarm.drones.suggest import suggest_rule

    try:
        body = await request.json()
    except Exception:
        return json_error("Invalid JSON body", 400)

    details = body.get("details")
    if not details or not isinstance(details, list):
        return json_error("'details' is required and must be a non-empty list of strings", 400)
    if not all(isinstance(d, str) for d in details):
        return json_error("'details' must contain only strings", 400)

    action = body.get("action", "approve")
    if action not in ("approve", "escalate"):
        return json_error("'action' must be 'approve' or 'escalate'", 400)

    suggestion = suggest_rule(details, action=action)
    return web.json_response(
        {
            "suggestion": {
                "pattern": suggestion.pattern,
                "action": suggestion.action,
                "confidence": suggestion.confidence,
                "explanation": suggestion.explanation,
            }
        }
    )


async def handle_notification_history(request: web.Request) -> web.Response:
    """Return recent notification history."""
    d = get_daemon(request)
    limit = min(int(request.query.get("limit", "50")), 50)
    history = d._notification_history[-limit:]
    return web.json_response({"notifications": list(reversed(history))})


async def handle_oversight_status(request: web.Request) -> web.Response:
    """Return Queen oversight monitor status."""
    d = get_daemon(request)
    monitor = getattr(d, "_oversight_monitor", None)
    if monitor is None:
        return web.json_response({"enabled": False})
    return web.json_response(monitor.get_status())


async def handle_ownership_status(request: web.Request) -> web.Response:
    """Return file ownership map status."""
    d = get_daemon(request)
    ownership = getattr(d, "file_ownership", None)
    if ownership is None:
        return web.json_response({"mode": "off"})
    return web.json_response(ownership.to_dict())


async def handle_sync_status(request: web.Request) -> web.Response:
    """Return auto-pull sync status."""
    d = get_daemon(request)
    sync = getattr(d, "auto_pull", None)
    if sync is None:
        return web.json_response({"enabled": False})
    return web.json_response(sync.get_status())


async def handle_drone_status(request: web.Request) -> web.Response:
    d = get_daemon(request)
    return web.json_response(
        {
            "enabled": d.pilot.enabled if d.pilot else False,
        }
    )


async def handle_drone_toggle(request: web.Request) -> web.Response:
    d = get_daemon(request)
    if d.pilot:
        new_state = d.toggle_drones()
        return web.json_response({"enabled": new_state})
    return json_error("pilot not running")


async def handle_drones_poll(request: web.Request) -> web.Response:
    d = get_daemon(request)
    if not d.pilot:
        return json_error("pilot not running")
    had_action = await d.poll_once()
    return web.json_response({"status": "ok", "had_action": had_action})
