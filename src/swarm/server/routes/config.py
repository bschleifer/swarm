"""Config routes — CRUD for workers, groups, approval rules, projects."""

from __future__ import annotations

from pathlib import Path

from aiohttp import web

from swarm.logging import get_logger
from swarm.server.daemon import WorkerNotFoundError
from swarm.server.helpers import get_daemon, handle_errors, json_error, validate_worker_name

_log = get_logger("server.routes.config")


def register(app: web.Application) -> None:
    app.router.add_get("/api/config", handle_get_config)
    app.router.add_put("/api/config", handle_update_config)
    app.router.add_post("/api/config/workers", handle_add_config_worker)
    app.router.add_post("/api/config/workers/{name}/save", handle_save_worker_to_config)
    app.router.add_post("/api/config/workers/{name}/add-to-group", handle_add_worker_to_group)
    app.router.add_delete("/api/config/workers/{name}", handle_remove_config_worker)
    app.router.add_post("/api/config/groups", handle_add_config_group)
    app.router.add_put("/api/config/groups/{name}", handle_update_config_group)
    app.router.add_delete("/api/config/groups/{name}", handle_remove_config_group)
    app.router.add_get("/api/config/projects", handle_list_projects)
    app.router.add_post("/api/config/approval-rules/dry-run", handle_dry_run_rules)
    app.router.add_post("/api/config/approval-rules", handle_add_approval_rule)


@handle_errors
async def handle_get_config(request: web.Request) -> web.Response:
    d = get_daemon(request)
    from swarm.config import serialize_config

    cfg = serialize_config(d.config)
    cfg.pop("api_password", None)
    return web.json_response(cfg)


@handle_errors
async def handle_update_config(request: web.Request) -> web.Response:
    """Partial update of settings (drones, queen, notifications, top-level scalars)."""
    d = get_daemon(request)
    body = await request.json()
    await d.apply_config_update(body)

    from swarm.config import serialize_config

    cfg = serialize_config(d.config)
    cfg.pop("api_password", None)
    return web.json_response(cfg)


@handle_errors
async def handle_add_config_worker(request: web.Request) -> web.Response:
    d = get_daemon(request)
    body = await request.json()
    name = body.get("name", "").strip()
    path = body.get("path", "").strip()

    if not name:
        return json_error("name is required")
    if err := validate_worker_name(name):
        return json_error(err)
    if not path:
        return json_error("path is required")

    resolved = Path(path).expanduser().resolve()
    if not resolved.exists():
        return json_error(f"Path does not exist: {resolved}")

    if d.config.get_worker(name):
        return json_error(f"Worker '{name}' already exists", 409)

    from swarm.config import WorkerConfig

    description = body.get("description", "").strip()
    provider = body.get("provider", "").strip()

    from swarm.providers import get_valid_providers

    if provider and provider not in get_valid_providers():
        return json_error(f"Unknown provider '{provider}'")
    wc = WorkerConfig(name=name, path=str(resolved), description=description, provider=provider)
    d.config.workers.append(wc)

    try:
        await d.spawn_worker(wc)
    except Exception as e:
        d.config.workers.remove(wc)
        _log.exception("failed to spawn worker '%s'", name)
        return json_error(f"Failed to spawn worker: {e}", 500)

    d.save_config()
    return web.json_response(
        {"status": "added", "worker": name},
        status=201,
    )


@handle_errors
async def handle_save_worker_to_config(request: web.Request) -> web.Response:
    """Save a running (spawned) worker to swarm.yaml."""
    d = get_daemon(request)
    name = request.match_info["name"]

    worker = d.get_worker(name)
    if not worker:
        return json_error(f"Worker '{name}' not found", 404)

    if d.config.get_worker(name):
        return json_error(f"Worker '{name}' is already in config", 409)

    from swarm.config import WorkerConfig

    description = ""
    for wc_existing in d.config.workers:
        if wc_existing.path == worker.path:
            description = wc_existing.description
            break

    wc = WorkerConfig(
        name=name,
        path=worker.path,
        provider=worker.provider_name,
        description=description,
    )
    d.config.workers.append(wc)
    d.save_config()
    return web.json_response({"status": "saved", "worker": name}, status=201)


@handle_errors
async def handle_add_worker_to_group(request: web.Request) -> web.Response:
    """Add a worker to a config group (optionally creating the group)."""
    d = get_daemon(request)
    name = request.match_info["name"]

    body = await request.json()
    group_name = body.get("group", "").strip()
    create = body.get("create", False)

    if not group_name:
        return json_error("group is required")

    if not d.get_worker(name) and not d.config.get_worker(name):
        return json_error(f"Worker '{name}' not found", 404)

    group = next((g for g in d.config.groups if g.name.lower() == group_name.lower()), None)
    if group:
        if name.lower() in [w.lower() for w in group.workers]:
            return json_error(f"Worker '{name}' is already in group '{group_name}'", 409)
        group.workers.append(name)
    elif create:
        from swarm.config import GroupConfig

        d.config.groups.append(GroupConfig(name=group_name, workers=[name]))
    else:
        return json_error(f"Group '{group_name}' not found", 404)

    d.save_config()
    return web.json_response({"status": "added", "worker": name, "group": group_name})


@handle_errors
async def handle_remove_config_worker(request: web.Request) -> web.Response:
    d = get_daemon(request)
    name = request.match_info["name"]

    wc = d.config.get_worker(name)
    if not wc:
        return json_error(f"Worker '{name}' not found in config", 404)

    try:
        await d.kill_worker(name)
    except WorkerNotFoundError:
        pass

    d.config.workers = [w for w in d.config.workers if w.name.lower() != name.lower()]
    d.save_config()
    return web.json_response({"status": "removed", "worker": name})


@handle_errors
async def handle_add_config_group(request: web.Request) -> web.Response:
    d = get_daemon(request)
    body = await request.json()
    name = body.get("name", "").strip()
    workers = body.get("workers", [])

    if not name:
        return json_error("name is required")
    if not isinstance(workers, list):
        return json_error("workers must be a list")

    existing = [g for g in d.config.groups if g.name.lower() == name.lower()]
    if existing:
        return json_error(f"Group '{name}' already exists", 409)

    from swarm.config import GroupConfig

    d.config.groups.append(GroupConfig(name=name, workers=workers))
    d.save_config()
    return web.json_response({"status": "added", "group": name}, status=201)


@handle_errors
async def handle_update_config_group(request: web.Request) -> web.Response:
    d = get_daemon(request)
    name = request.match_info["name"]
    body = await request.json()
    workers = body.get("workers", [])

    if not isinstance(workers, list):
        return json_error("workers must be a list")

    group = next((g for g in d.config.groups if g.name.lower() == name.lower()), None)
    if not group:
        return json_error(f"Group '{name}' not found", 404)

    new_name = body.get("name", "").strip()
    if new_name and new_name.lower() != group.name.lower():
        existing = [g for g in d.config.groups if g.name.lower() == new_name.lower()]
        if existing:
            return json_error(f"Group '{new_name}' already exists", 409)
        old_name = group.name
        group.name = new_name
        if d.config.default_group and d.config.default_group.lower() == old_name.lower():
            d.config.default_group = new_name

    group.workers = workers
    d.save_config()
    return web.json_response({"status": "updated", "group": group.name, "workers": workers})


@handle_errors
async def handle_remove_config_group(request: web.Request) -> web.Response:
    d = get_daemon(request)
    name = request.match_info["name"]

    before = len(d.config.groups)
    d.config.groups = [g for g in d.config.groups if g.name.lower() != name.lower()]
    if len(d.config.groups) == before:
        return json_error(f"Group '{name}' not found", 404)

    d.save_config()
    return web.json_response({"status": "removed", "group": name})


@handle_errors
async def handle_list_projects(request: web.Request) -> web.Response:
    d = get_daemon(request)
    from swarm.config import discover_projects

    projects_dir = Path(d.config.projects_dir).expanduser().resolve()
    projects = discover_projects(projects_dir)
    return web.json_response(
        {
            "projects": [{"name": name, "path": path} for name, path in projects],
        }
    )


@handle_errors
async def handle_dry_run_rules(request: web.Request) -> web.Response:
    """Test approval rules against sample content without deploying."""
    import re as _re

    from swarm.config import DroneApprovalRule
    from swarm.drones.rules import dry_run_rules

    try:
        body = await request.json()
    except Exception:
        return json_error("Invalid JSON body", 400)

    content = body.get("content", "")
    if not content or not isinstance(content, str):
        return json_error("'content' is required and must be a non-empty string", 400)

    raw_rules = body.get("rules")
    if raw_rules is not None:
        if not isinstance(raw_rules, list):
            return json_error("'rules' must be an array", 400)
        approval_rules: list[DroneApprovalRule] = []
        for i, r in enumerate(raw_rules):
            if not isinstance(r, dict):
                return json_error(f"rules[{i}]: must be an object", 400)
            pattern = r.get("pattern", "")
            action = r.get("action", "approve")
            if action not in ("approve", "escalate"):
                return json_error(
                    f"rules[{i}]: action must be 'approve' or 'escalate', got '{action}'",
                    400,
                )
            try:
                _re.compile(pattern)
            except _re.error as exc:
                return json_error(f"rules[{i}]: invalid regex '{pattern}': {exc}", 400)
            approval_rules.append(DroneApprovalRule(pattern=pattern, action=action))
    else:
        d = get_daemon(request)
        approval_rules = list(d.config.drones.approval_rules)

    allowed_read_paths = body.get("allowed_read_paths")
    if allowed_read_paths is None:
        d = get_daemon(request)
        allowed_read_paths = list(d.config.drones.allowed_read_paths)

    results = dry_run_rules(
        content=content,
        approval_rules=approval_rules,
        allowed_read_paths=allowed_read_paths,
    )
    return web.json_response(
        {
            "results": [
                {
                    "matched": r.matched,
                    "decision": r.decision,
                    "rule_index": r.rule_index,
                    "rule_pattern": r.rule_pattern,
                    "source": r.source,
                }
                for r in results
            ]
        }
    )


@handle_errors
async def handle_add_approval_rule(request: web.Request) -> web.Response:
    """Add a new drone approval rule to the config."""
    import re as _re

    from swarm.config import DroneApprovalRule

    try:
        body = await request.json()
    except Exception:
        return json_error("Invalid JSON body", 400)

    pattern = body.get("pattern", "")
    if not pattern or not isinstance(pattern, str):
        return json_error("'pattern' is required and must be a non-empty string", 400)

    action = body.get("action", "approve")
    if action not in ("approve", "escalate"):
        return json_error("'action' must be 'approve' or 'escalate'", 400)

    try:
        _re.compile(pattern)
    except _re.error as exc:
        return json_error(f"Invalid regex pattern: {exc}", 400)

    d = get_daemon(request)
    rules = list(d.config.drones.approval_rules)
    new_rule = DroneApprovalRule(pattern=pattern, action=action)

    position = body.get("position")
    if position is not None:
        try:
            position = int(position)
        except (ValueError, TypeError):
            return json_error("Invalid 'position' — must be an integer", 400)
        position = max(0, min(position, len(rules)))
        rules.insert(position, new_rule)
    else:
        rules.append(new_rule)

    d.config.drones.approval_rules = rules
    # sync_rules=True: this route's sole purpose is to add an approval
    # rule, so the in-memory list is authoritative for this save.
    d.config_mgr.save(sync_rules=True)
    d.config_mgr.hot_apply()

    return web.json_response(
        {
            "status": "ok",
            "rules": [{"pattern": r.pattern, "action": r.action} for r in rules],
        }
    )
