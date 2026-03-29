"""System routes — health, session, tunnel, server, upload, resources."""

from __future__ import annotations

import asyncio
import os
import time

from aiohttp import web

from swarm.auth.password import verify_password
from swarm.server.helpers import get_daemon, handle_errors, json_error, read_file_field


def register(app: web.Application) -> None:
    app.router.add_get("/health", handle_health_check)
    app.router.add_get("/ready", handle_readiness)
    app.router.add_get("/api/health", handle_health)
    app.router.add_get("/api/resources", handle_resources)

    app.router.add_post("/api/session/kill", handle_session_kill)

    app.router.add_post("/api/tunnel/start", handle_tunnel_start)
    app.router.add_post("/api/tunnel/stop", handle_tunnel_stop)
    app.router.add_get("/api/tunnel/status", handle_tunnel_status)

    app.router.add_post("/api/server/stop", handle_server_stop)
    app.router.add_post("/api/server/restart", handle_server_restart)

    app.router.add_post("/api/uploads", handle_upload)


async def handle_readiness(request: web.Request) -> web.Response:
    """Readiness probe — unauthenticated, returns 200 when fully initialized."""
    d = get_daemon(request)
    checks: dict[str, bool] = {
        "config_loaded": d.config is not None,
        "workers_initialized": hasattr(d, "workers"),
    }
    if d.config and d.config.drones.enabled:
        checks["pilot_running"] = d.pilot is not None and d.pilot.enabled
    ready = all(checks.values())
    return web.json_response({"ready": ready, "checks": checks}, status=200 if ready else 503)


@handle_errors
async def handle_resources(request: web.Request) -> web.Response:
    """GET /api/resources — return current resource snapshot."""
    daemon = get_daemon(request)
    snapshot = daemon.get_resource_snapshot()
    if snapshot is None:
        return web.json_response({"error": "resource monitoring not active"}, status=503)
    return web.json_response(snapshot)


@handle_errors
async def handle_health_check(request: web.Request) -> web.Response:
    """Root-level health check — unauthenticated for tunnel probes."""
    from swarm.server.api import get_api_password
    from swarm.update import _get_installed_version, build_sha

    d = get_daemon(request)
    uptime = time.time() - d.start_time
    version = _get_installed_version()

    payload: dict[str, object] = {
        "status": "ok",
        "uptime": uptime,
        "version": version,
    }

    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        password = get_api_password(d)
        if verify_password(auth[7:], password):
            payload["workers"] = [
                {
                    "name": w.name,
                    "state": w.state.value,
                    "duration": w.state_duration,
                }
                for w in d.workers
            ]
            payload["queen"] = dict(d.queen_queue.status())
            payload["drones"] = {"enabled": d.pilot.enabled if d.pilot else False}
            payload["pilot"] = d.pilot.get_diagnostics() if d.pilot else {}
            payload["build_sha"] = build_sha()

    return web.json_response(payload)


@handle_errors
async def handle_health(request: web.Request) -> web.Response:
    from swarm.update import _get_installed_version, build_sha

    d = get_daemon(request)
    pilot_info: dict[str, object] = {}
    if d.pilot:
        pilot_info = d.pilot.get_diagnostics()
    return web.json_response(
        {
            "status": "ok",
            "workers": len(d.workers),
            "drones_enabled": d.pilot.enabled if d.pilot else False,
            "uptime": time.time() - d.start_time,
            "pilot": pilot_info,
            "version": _get_installed_version(),
            "build_sha": build_sha(),
        }
    )


@handle_errors
async def handle_session_kill(request: web.Request) -> web.Response:
    d = get_daemon(request)
    await d.kill_session()
    return web.json_response({"status": "killed"})


@handle_errors
async def handle_tunnel_start(request: web.Request) -> web.Response:
    d = get_daemon(request)
    if d.tunnel.is_running:
        return web.json_response(d.tunnel.to_dict())
    explicit_pw = os.environ.get("SWARM_API_PASSWORD") or d.config.api_password
    if not explicit_pw:
        return json_error(
            "Set SWARM_API_PASSWORD or api_password in swarm.yaml before starting a public tunnel",
            400,
        )
    try:
        await d.tunnel.start()
    except RuntimeError as e:
        return json_error(str(e), 500)
    return web.json_response(d.tunnel.to_dict())


@handle_errors
async def handle_tunnel_stop(request: web.Request) -> web.Response:
    d = get_daemon(request)
    await d.tunnel.stop()
    return web.json_response(d.tunnel.to_dict())


@handle_errors
async def handle_tunnel_status(request: web.Request) -> web.Response:
    d = get_daemon(request)
    return web.json_response(d.tunnel.to_dict())


@handle_errors
async def handle_server_stop(request: web.Request) -> web.Response:
    shutdown: asyncio.Event | None = request.app.get("shutdown_event")
    if shutdown:
        shutdown.set()
        return web.json_response({"status": "stopping"})
    return json_error("shutdown not available")


@handle_errors
async def handle_server_restart(request: web.Request) -> web.Response:
    from swarm.update import reinstall_from_local_source

    ok, output = await reinstall_from_local_source()
    if not ok:
        import logging

        logging.getLogger("swarm.api").warning(
            "Local reinstall failed (proceeding with restart): %s", output
        )

    restart_flag = request.app.get("restart_flag")
    if restart_flag is not None:
        restart_flag["requested"] = True
    shutdown: asyncio.Event | None = request.app.get("shutdown_event")
    if shutdown:
        shutdown.set()
        return web.json_response({"status": "restarting"})
    return json_error("shutdown not available")


@handle_errors
async def handle_upload(request: web.Request) -> web.Response:
    d = get_daemon(request)
    filename, data = await read_file_field(request)
    path = d.save_attachment(filename, data)
    return web.json_response({"status": "uploaded", "path": path}, status=201)
