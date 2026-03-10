"""Web dashboard route modules."""

from __future__ import annotations

from aiohttp import web


def register_all(app: web.Application) -> None:
    """Register all web route modules on the application."""
    from swarm.web.routes import (
        auth,
        pages,
        partials,
        proposals,
        pwa,
        queen,
        system,
        tasks,
        workers,
    )

    pages.register(app)
    partials.register(app)
    workers.register(app)
    tasks.register(app)
    proposals.register(app)
    queen.register(app)
    auth.register(app)
    system.register(app)
    pwa.register(app)
