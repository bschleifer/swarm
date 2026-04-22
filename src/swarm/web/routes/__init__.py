"""Web dashboard route modules."""

from __future__ import annotations

from aiohttp import web


def register_all(app: web.Application) -> None:
    """Register all web route modules on the application."""
    from swarm.web.routes import (
        auth,
        login,
        pages,
        partials,
        proposals,
        pwa,
        system,
        tasks,
        workers,
    )

    login.register(app)
    pages.register(app)
    partials.register(app)
    workers.register(app)
    tasks.register(app)
    proposals.register(app)
    auth.register(app)
    system.register(app)
    pwa.register(app)
