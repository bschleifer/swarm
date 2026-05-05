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
    app.router.add_get("/api/skills", handle_list_skills)


@handle_errors
async def handle_get_config(request: web.Request) -> web.Response:
    d = get_daemon(request)
    from swarm.config import serialize_config

    cfg = serialize_config(d.config)
    cfg.pop("api_password", None)
    return web.json_response(cfg)


@handle_errors
async def handle_update_config(request: web.Request) -> web.Response:
    """Partial update of settings (drones, queen, notifications, top-level scalars).

    Response shape: serialized HiveConfig at the top level (backward
    compatible with existing tests + dashboard code) plus an
    ``_apply_result`` key carrying the structured outcome — which
    fields landed, which were unknown, broken down by section.
    Phase 7 of #328 — drives the dashboard's enhanced save toast.
    """
    d = get_daemon(request)
    body = await request.json()
    apply_result = await d.apply_config_update(body)

    from swarm.config import serialize_config

    cfg = serialize_config(d.config)
    cfg.pop("api_password", None)
    cfg["_apply_result"] = apply_result
    return web.json_response(cfg)


# Worker create-body fields handled by bespoke validators in
# ``handle_add_config_worker`` (path resolution, duplicate check,
# provider whitelist).  Generic dataclass dispatch covers the rest
# so new ``WorkerConfig`` fields auto-flow into the create endpoint.
# ``approval_rules`` / ``allowed_tools`` are skipped: rules have
# their own endpoint with regex compile + DB sync semantics, and
# ``allowed_tools`` doesn't have a DB column yet (audit gap, deferred).
_WORKER_CREATE_CUSTOM_KEYS: frozenset[str] = frozenset(
    {"name", "path", "approval_rules", "allowed_tools"}
)


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
    from swarm.server.config_manager import _apply_dataclass_dict

    # Construct with the required identity fields, then let generic
    # dataclass dispatch populate every other writable field from
    # the body (description, provider, isolation, identity, …).
    # Phase 6 of #328 — closes the audit's worker-create silent-drop
    # gaps for ``isolation`` and ``identity``, and emits the
    # standard unknown-sub-key WARNING for any future schema drift.
    wc = WorkerConfig(name=name, path=str(resolved))
    outcome = _apply_dataclass_dict(body, wc, "worker", skip_keys=_WORKER_CREATE_CUSTOM_KEYS)

    from swarm.providers import get_valid_providers

    if wc.provider and wc.provider not in get_valid_providers():
        return json_error(f"Unknown provider '{wc.provider}'")

    d.config.workers.append(wc)

    try:
        await d.spawn_worker(wc)
    except Exception as e:
        d.config.workers.remove(wc)
        _log.exception("failed to spawn worker '%s'", name)
        return json_error(f"Failed to spawn worker: {e}", 500)

    d.save_config()
    # Phase 7 of #328: include the per-field outcome in the response
    # so the dashboard's addWorker handler can surface a warning toast
    # if the body carried fields the server didn't recognize.
    return web.json_response(
        {"status": "added", "worker": name, "_apply_result": outcome.to_dict()},
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
    from swarm.server.config_manager import FieldOutcome

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
    # Phase 8 of #328: structured response shape for client parity.
    # This endpoint takes no body fields — the worker's identity is
    # extracted from the running ``Worker`` object — so consumed and
    # unknown are both empty.  The dashboard's ``_toastApplyResult``
    # helper no-ops on empty lists, so there's no spurious toast.
    return web.json_response(
        {"status": "saved", "worker": name, "_apply_result": FieldOutcome().to_dict()},
        status=201,
    )


_ADD_TO_GROUP_BODY_KEYS: frozenset[str] = frozenset({"group", "create"})


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
    # Phase 8: validate body keys against the fixed schema and return
    # the structured outcome.  ``group`` + ``create`` are the only
    # supported keys; anything else lands in ``unknown`` with a
    # WARNING-level log.
    from swarm.server.config_manager import validate_body_keys

    outcome = validate_body_keys(body, set(_ADD_TO_GROUP_BODY_KEYS), "worker.add-to-group")
    return web.json_response(
        {
            "status": "added",
            "worker": name,
            "group": group_name,
            "_apply_result": outcome.to_dict(),
        }
    )


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


# Phase 7: group create/update endpoints now use full
# ``_apply_dataclass_dict`` dispatch directly — the helper below was
# the Phase 6 warn-only intermediate, replaced by structured
# ApplyResult on the response.


@handle_errors
async def handle_add_config_group(request: web.Request) -> web.Response:
    d = get_daemon(request)
    body = await request.json()
    name = body.get("name", "").strip()

    if not name:
        return json_error("name is required")

    existing = [g for g in d.config.groups if g.name.lower() == name.lower()]
    if existing:
        return json_error(f"Group '{name}' already exists", 409)

    from swarm.config import GroupConfig
    from swarm.server.config_manager import _apply_dataclass_dict

    # Phase 7 of #328: full generic dispatch on the group create body
    # — type-validates ``workers`` (list[str]) and surfaces unknown
    # sub-keys as a section-prefixed WARNING.  ``name`` was extracted
    # above for the duplicate check; everything else flows through
    # introspection.
    gc = GroupConfig(name=name, workers=[])
    outcome = _apply_dataclass_dict(body, gc, "group", skip_keys={"name"})
    d.config.groups.append(gc)
    d.save_config()
    return web.json_response(
        {"status": "added", "group": name, "_apply_result": outcome.to_dict()},
        status=201,
    )


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
    # When the default group's member order changes, sync dashboard worker order
    effective_name = new_name if (new_name and new_name.lower() != name.lower()) else group.name
    dg_name = d.config.default_group or "default"
    if dg_name.lower() == effective_name.lower():
        d.worker_svc.reorder_workers(workers)
        # Keep config.workers in sync so save_config_to_db writes correct sort_order
        by_name = {wc.name: wc for wc in d.config.workers}
        reordered_cfg: list = []
        for wn in workers:
            if wn in by_name:
                reordered_cfg.append(by_name.pop(wn))
        reordered_cfg.extend(by_name.values())
        d.config.workers = reordered_cfg
    # Phase 7 of #328: build a structured outcome for the response.
    # GroupConfig has just ``name`` + ``workers``; both are custom-
    # handled above (rename + reorder cascade), so the dispatch sweep
    # is purely a drift detector — it'll only populate ``unknown`` if
    # the dashboard sends a key GroupConfig doesn't declare.
    from swarm.server.config_manager import FieldOutcome, _apply_dataclass_dict

    consumed: list[str] = []
    if "workers" in body:
        consumed.append("workers")
    if new_name:
        consumed.append("name")
    drift = _apply_dataclass_dict(body, group, "group", skip_keys={"name", "workers"})
    outcome = FieldOutcome(consumed=consumed, unknown=list(drift.unknown))
    d.save_config()
    return web.json_response(
        {
            "status": "updated",
            "group": group.name,
            "workers": workers,
            "_apply_result": outcome.to_dict(),
        }
    )


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

    # Phase 8 of #328: validate the body's keys and return a
    # structured ApplyResult for client-side parity.  Approval rules
    # take ``pattern`` + ``action`` + optional ``position``.
    from swarm.server.config_manager import validate_body_keys

    outcome = validate_body_keys(body, {"pattern", "action", "position"}, "approval-rules")
    return web.json_response(
        {
            "status": "ok",
            "rules": [{"pattern": r.pattern, "action": r.action} for r in rules],
            "_apply_result": outcome.to_dict(),
        }
    )


@handle_errors
async def handle_list_skills(request: web.Request) -> web.Response:
    """List registered skills with their usage counts.

    When the DB-backed registry is attached, returns persisted skills.
    Otherwise returns the in-memory defaults as a read-only snapshot so
    fresh installs still expose something useful.
    """
    from swarm.tasks.workflows import SKILL_COMMANDS, get_skills_store

    store = get_skills_store()
    if store is not None:
        skills = [s.to_api() for s in store.list_all()]
    else:
        skills = [
            {
                "name": cmd,
                "description": "",
                "task_types": [task_type.value],
                "usage_count": 0,
                "last_used_at": None,
                "created_at": 0.0,
            }
            for task_type, cmd in SKILL_COMMANDS.items()
        ]
    return web.json_response({"skills": skills})
