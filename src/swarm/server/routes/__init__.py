"""Domain-specific route modules for the swarm API."""

from __future__ import annotations

from aiohttp import web


def register_all(app: web.Application) -> None:
    """Register all route modules on the application."""
    from swarm.server.routes import (
        config,
        drones,
        hooks,
        jira,
        pipelines,
        proposals,
        queen,
        system,
        tasks,
        websocket,
        workers,
    )

    workers.register(app)
    drones.register(app)
    hooks.register(app)
    jira.register(app)
    queen.register(app)
    tasks.register(app)
    pipelines.register(app)
    proposals.register(app)
    system.register(app)
    config.register(app)
    websocket.register(app)
