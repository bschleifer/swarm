"""Queen routes — coordination and queue status."""

from __future__ import annotations

from aiohttp import web

from swarm.server.helpers import get_daemon, handle_errors


def register(app: web.Application) -> None:
    app.router.add_post("/api/queen/coordinate", handle_queen_coordinate)
    app.router.add_get("/api/queen/queue", handle_queen_queue)


@handle_errors
async def handle_queen_coordinate(request: web.Request) -> web.Response:
    d = get_daemon(request)
    result = await d.coordinate_hive(force=True)
    return web.json_response(result)


async def handle_queen_queue(request: web.Request) -> web.Response:
    d = get_daemon(request)
    return web.json_response(d.queen_queue.status())
