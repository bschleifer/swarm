"""Worker routes — CRUD, I/O, lifecycle."""

from __future__ import annotations

import json

from aiohttp import web

from swarm.pty.process import ProcessError
from swarm.server.helpers import (
    get_daemon,
    handle_errors,
    json_error,
    require_message,
    validate_worker_name,
    worker_action,
)


def register(app: web.Application) -> None:
    app.router.add_get("/api/workers", handle_workers)

    # Literal worker routes BEFORE {name} to avoid ambiguity
    app.router.add_post("/api/workers/launch", handle_workers_launch)
    app.router.add_post("/api/workers/spawn", handle_workers_spawn)
    app.router.add_post("/api/workers/continue-all", handle_workers_continue_all)
    app.router.add_post("/api/workers/send-all", handle_workers_send_all)
    app.router.add_post("/api/workers/discover", handle_workers_discover)
    app.router.add_post("/api/workers/reorder", handle_workers_reorder)

    app.router.add_get("/api/workers/{name}", handle_worker_detail)
    app.router.add_patch("/api/workers/{name}", handle_worker_update)
    app.router.add_get("/api/workers/{name}/identity", handle_worker_identity)
    app.router.add_get("/api/workers/{name}/memory", handle_worker_memory)
    app.router.add_put("/api/workers/{name}/memory", handle_worker_memory_save)
    app.router.add_post("/api/workers/{name}/send", handle_worker_send)
    app.router.add_post("/api/workers/{name}/continue", handle_worker_continue)
    app.router.add_post("/api/workers/{name}/kill", handle_worker_kill)
    app.router.add_post("/api/workers/{name}/escape", handle_worker_escape)
    app.router.add_post("/api/workers/{name}/arrow-up", handle_worker_arrow_up)
    app.router.add_post("/api/workers/{name}/arrow-down", handle_worker_arrow_down)
    app.router.add_post("/api/workers/{name}/interrupt", handle_worker_interrupt)
    app.router.add_post("/api/workers/{name}/revive", handle_worker_revive)
    app.router.add_post("/api/workers/{name}/sleep", handle_worker_sleep)
    app.router.add_post("/api/workers/{name}/analyze", handle_worker_analyze)
    app.router.add_post("/api/workers/{name}/merge", handle_worker_merge)

    # Conflicts
    app.router.add_get("/api/conflicts", handle_conflicts)

    # Groups
    app.router.add_post("/api/groups/{name}/send", handle_group_send)

    # Usage
    app.router.add_get("/api/usage", handle_usage)


@handle_errors
async def handle_workers(request: web.Request) -> web.Response:
    d = get_daemon(request)
    workers = []
    for w in d.workers:
        wd = w.to_api_dict()
        wd["in_config"] = d.config.get_worker(w.name) is not None
        workers.append(wd)
    return web.json_response({"workers": workers})


@handle_errors
async def handle_worker_detail(request: web.Request) -> web.Response:
    d = get_daemon(request)
    name = request.match_info["name"]
    worker = d.get_worker(name)
    if not worker:
        return json_error(f"Worker '{name}' not found", 404)

    try:
        content = await d.capture_worker_output(name)
    except (ProcessError, OSError):
        content = "(output unavailable)"

    result = worker.to_api_dict()
    result["worker_output"] = content
    return web.json_response(result)


@handle_errors
async def handle_worker_update(request: web.Request) -> web.Response:
    d = get_daemon(request)
    name = request.match_info["name"]
    body = await request.json()
    new_name = body.get("name", "").strip() or None
    new_path = body.get("path", "").strip() or None

    if new_name:
        if err := validate_worker_name(new_name):
            return json_error(err)

    d.worker_svc.update_worker(name, name=new_name, path=new_path)
    result_name = new_name or name
    return web.json_response({"status": "updated", "worker": result_name})


@handle_errors
async def handle_worker_send(request: web.Request) -> web.Response:
    d = get_daemon(request)
    name = request.match_info["name"]

    body = await request.json()
    result = require_message(body)
    if isinstance(result, web.Response):
        return result
    message = result

    await d.send_to_worker(name, message)
    return web.json_response({"status": "sent", "worker": name})


async def handle_worker_continue(request: web.Request) -> web.Response:
    return await worker_action(request, lambda d, n: d.continue_worker(n), "continued")


async def handle_worker_kill(request: web.Request) -> web.Response:
    return await worker_action(request, lambda d, n: d.kill_worker(n), "killed")


async def handle_worker_escape(request: web.Request) -> web.Response:
    return await worker_action(request, lambda d, n: d.escape_worker(n), "escape_sent")


async def handle_worker_arrow_up(request: web.Request) -> web.Response:
    return await worker_action(request, lambda d, n: d.arrow_up_worker(n), "arrow_up_sent")


async def handle_worker_arrow_down(request: web.Request) -> web.Response:
    return await worker_action(request, lambda d, n: d.arrow_down_worker(n), "arrow_down_sent")


async def handle_worker_interrupt(request: web.Request) -> web.Response:
    return await worker_action(request, lambda d, n: d.interrupt_worker(n), "interrupted")


async def handle_worker_revive(request: web.Request) -> web.Response:
    return await worker_action(request, lambda d, n: d.revive_worker(n), "revived")


async def handle_worker_sleep(request: web.Request) -> web.Response:
    return await worker_action(request, lambda d, n: d.sleep_worker(n), "sleeping")


@handle_errors
async def handle_worker_identity(request: web.Request) -> web.Response:
    d = get_daemon(request)
    name = request.match_info["name"]
    wc = d.config.get_worker(name)
    if not wc:
        return json_error(f"Worker '{name}' not found in config", 404)
    content = wc.load_identity()
    return web.json_response({"worker": name, "identity": content, "path": wc.identity})


@handle_errors
async def handle_worker_memory(request: web.Request) -> web.Response:
    name = request.match_info["name"]
    from swarm.worker.memory import list_memory_files, load_memory

    content = load_memory(name)
    files = list_memory_files(name)
    return web.json_response({"worker": name, "memory": content, "files": files})


@handle_errors
async def handle_worker_memory_save(request: web.Request) -> web.Response:
    name = request.match_info["name"]
    body = await request.json()
    content = body.get("content", "")
    if not isinstance(content, str):
        return json_error("'content' must be a string")
    from swarm.worker.memory import save_memory

    save_memory(name, content)
    return web.json_response({"status": "saved", "worker": name})


@handle_errors
async def handle_worker_analyze(request: web.Request) -> web.Response:
    d = get_daemon(request)
    name = request.match_info["name"]
    result = await d.analyze_worker(name, force=True)
    return web.json_response(result)


@handle_errors
async def handle_worker_merge(request: web.Request) -> web.Response:
    d = get_daemon(request)
    name = request.match_info["name"]
    result = await d.worker_svc.merge_worker(name)
    status = 200 if result.get("success") else 409
    return web.json_response(result, status=status)


@handle_errors
async def handle_conflicts(request: web.Request) -> web.Response:
    d = get_daemon(request)
    conflicts = getattr(d, "_conflicts", [])
    return web.json_response({"conflicts": conflicts})


@handle_errors
async def handle_workers_reorder(request: web.Request) -> web.Response:
    d = get_daemon(request)
    body = await request.json()
    order = body.get("order")
    if not isinstance(order, list) or not all(isinstance(n, str) for n in order):
        return json_error("'order' must be a list of worker name strings")
    d.worker_svc.reorder_workers(order)
    return web.json_response({"status": "ok"})


@handle_errors
async def handle_workers_launch(request: web.Request) -> web.Response:
    d = get_daemon(request)
    body = await request.json() if request.can_read_body else {}
    requested = body.get("workers", [])

    # Determine which configs to launch
    running_names = {w.name.lower() for w in d.workers}
    if requested:
        config_by_name = {wc.name.lower(): wc for wc in d.config.workers}
        seen: set[str] = set()
        configs = []
        for name in requested:
            key = name.lower()
            if key not in seen and key not in running_names and key in config_by_name:
                configs.append(config_by_name[key])
                seen.add(key)
    else:
        configs = [wc for wc in d.config.workers if wc.name.lower() not in running_names]

    if not configs:
        return web.json_response({"status": "no_new_workers", "launched": []})

    launched = await d.launch_workers(configs)

    return web.json_response(
        {"status": "launched", "launched": [w.name for w in launched]},
        status=201,
    )


@handle_errors
async def handle_workers_spawn(request: web.Request) -> web.Response:
    d = get_daemon(request)
    body = await request.json()
    name = body.get("name", "").strip()
    path = body.get("path", "").strip()
    provider = body.get("provider", "").strip()

    if not name:
        return json_error("name is required")
    if err := validate_worker_name(name):
        return json_error(err)
    if not path:
        return json_error("path is required")
    from swarm.providers import get_valid_providers

    if provider and provider not in get_valid_providers():
        return json_error(f"Unknown provider '{provider}'")

    from swarm.config import WorkerConfig

    worker = await d.spawn_worker(WorkerConfig(name=name, path=path, provider=provider))

    return web.json_response(
        {"status": "spawned", "worker": worker.name},
        status=201,
    )


@handle_errors
async def handle_workers_continue_all(request: web.Request) -> web.Response:
    d = get_daemon(request)
    count = await d.continue_all()
    return web.json_response({"status": "ok", "count": count})


@handle_errors
async def handle_workers_send_all(request: web.Request) -> web.Response:
    d = get_daemon(request)
    body = await request.json()
    result = require_message(body)
    if isinstance(result, web.Response):
        return result
    count = await d.send_all(result)
    return web.json_response({"status": "sent", "count": count})


@handle_errors
async def handle_workers_discover(request: web.Request) -> web.Response:
    d = get_daemon(request)
    workers = await d.discover()
    return web.json_response({"status": "ok", "workers": [{"name": w.name} for w in workers]})


@handle_errors
async def handle_group_send(request: web.Request) -> web.Response:
    d = get_daemon(request)
    group_name = request.match_info["name"]
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return json_error("Invalid JSON in request body")
    result = require_message(body)
    if isinstance(result, web.Response):
        return result
    try:
        count = await d.send_group(group_name, result)
    except (ValueError, KeyError) as e:
        return json_error(str(e), 404)
    return web.json_response({"status": "sent", "group": group_name, "count": count})


@handle_errors
async def handle_usage(request: web.Request) -> web.Response:
    """Return per-worker, queen, and total token usage."""
    d = get_daemon(request)
    from swarm.worker.worker import TokenUsage

    workers_usage: dict[str, dict[str, object]] = {}
    total = TokenUsage()
    for w in d.workers:
        workers_usage[w.name] = w.usage.to_dict()
        total.add(w.usage)

    queen_usage = d.queen.usage.to_dict()
    queen_tu = d.queen.usage
    total.add(queen_tu)

    return web.json_response(
        {
            "workers": workers_usage,
            "queen": queen_usage,
            "total": total.to_dict(),
        }
    )
