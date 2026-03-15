"""REST + WebSocket API server for the swarm daemon.

This module contains the ``create_app`` factory, middleware, and shared
auth/rate-limit helpers.  Route handlers live in ``swarm.server.routes.*``.
"""

from __future__ import annotations

import os
import secrets
import time
from collections import defaultdict, deque
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from aiohttp import web

from swarm.auth.password import verify_password
from swarm.logging import get_logger
from swarm.server.helpers import get_daemon, json_error

if TYPE_CHECKING:
    from swarm.server.daemon import SwarmDaemon

_log = get_logger("server.api")

_RATE_LIMIT_REQUESTS = 60  # per minute
_RATE_LIMIT_WINDOW = 60  # seconds
_RATE_LIMIT_CLEANUP_INTERVAL = 300  # evict stale IPs every 5 minutes
_RATE_LIMIT_MAX_IPS = 10_000  # absolute cap on tracked IPs
_rate_limit_last_cleanup: float = 0.0

# Paths that require authentication for mutating methods
_CONFIG_AUTH_PREFIX = "/api/config"

# Auto-generated token for sessions where no api_password is configured.
# Generated once per process; logged on startup so the operator can see it.
_auto_token: str = secrets.token_urlsafe(32)


# ---------------------------------------------------------------------------
# Shared auth / origin helpers (used by routes)
# ---------------------------------------------------------------------------


def get_client_ip(request: web.Request) -> str:
    """Get client IP, respecting X-Forwarded-For only when trust_proxy is enabled."""
    daemon = get_daemon(request)
    if daemon.config.trust_proxy:
        forwarded = request.headers.get("X-Forwarded-For", "")
        if forwarded:
            parts = [p.strip() for p in forwarded.split(",") if p.strip()]
            if len(parts) >= 2:
                return parts[-2]
            if parts:
                return parts[0]
    return request.remote or "unknown"


def get_api_password(daemon: SwarmDaemon) -> str:
    """Get API password from config, environment, or auto-generated token."""
    return os.environ.get("SWARM_API_PASSWORD") or daemon.config.api_password or _auto_token


def is_same_origin(request: web.Request, origin: str) -> bool:
    """Check if the Origin header matches the request host or the tunnel URL."""
    if not origin:
        return True
    req_host = request.host.split(":")[0] if request.host else ""
    parsed = urlparse(origin)
    origin_host = parsed.hostname or ""
    if origin_host in ("localhost", "127.0.0.1") or origin_host == req_host:
        return True
    daemon = get_daemon(request)
    tunnel_url = daemon.tunnel.url
    if tunnel_url:
        tunnel_parsed = urlparse(tunnel_url)
        if origin_host == (tunnel_parsed.hostname or ""):
            return True
    return False


# ---------------------------------------------------------------------------
# Middleware
# ---------------------------------------------------------------------------


@web.middleware
async def _config_auth_middleware(
    request: web.Request, handler: Callable[[web.Request], Awaitable[web.StreamResponse]]
) -> web.StreamResponse:
    """Require Bearer token for mutating config endpoints."""
    if request.path.startswith(_CONFIG_AUTH_PREFIX) and request.method in ("PUT", "POST", "DELETE"):
        daemon = get_daemon(request)
        password = get_api_password(daemon)
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer ") or not verify_password(auth[7:], password):
            return json_error("Unauthorized", 401)
    return await handler(request)


@web.middleware
async def _csrf_middleware(
    request: web.Request, handler: Callable[[web.Request], Awaitable[web.StreamResponse]]
) -> web.StreamResponse:
    """Reject cross-origin mutating requests."""
    if request.method in ("POST", "PUT", "DELETE"):
        origin = request.headers.get("Origin", "")
        if origin and not is_same_origin(request, origin):
            return web.Response(status=403, text="CSRF rejected")
        if (
            request.path.startswith("/api/") or request.path.startswith("/action/")
        ) and not request.headers.get("X-Requested-With"):
            return web.Response(status=403, text="Missing X-Requested-With header")
    return await handler(request)


@web.middleware
async def _security_headers_middleware(
    request: web.Request, handler: Callable[[web.Request], Awaitable[web.StreamResponse]]
) -> web.StreamResponse:
    """Add security headers to all responses."""
    response = await handler(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    nonce = request.get("csp_nonce")
    if nonce:
        script_src = f"'self' 'nonce-{nonce}' https://unpkg.com https://cdn.jsdelivr.net"
    else:
        script_src = "'self' https://unpkg.com https://cdn.jsdelivr.net"
    response.headers.setdefault(
        "Content-Security-Policy",
        f"default-src 'self'; "
        f"script-src {script_src}; "
        f"style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
        f"img-src 'self' data: blob:; "
        f"font-src 'self' data:; "
        f"connect-src 'self' ws: wss: https://cdn.jsdelivr.net https://unpkg.com; "
        f"frame-ancestors 'self'",
    )
    if request.secure:
        response.headers.setdefault(
            "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
        )
    return response


@web.middleware
async def _rate_limit_middleware(
    request: web.Request, handler: Callable[[web.Request], Awaitable[web.StreamResponse]]
) -> web.StreamResponse:
    """Simple in-memory rate limiter: N requests/minute per client IP."""
    if request.method == "GET" or request.path in ("/ws", "/ws/terminal"):
        return await handler(request)

    ip = get_client_ip(request)
    now = time.time()
    rate_limits: dict[str, deque[float]] = request.app["rate_limits"]
    timestamps = rate_limits[ip]
    cutoff = now - _RATE_LIMIT_WINDOW
    while timestamps and timestamps[0] <= cutoff:
        timestamps.popleft()

    if len(timestamps) >= _RATE_LIMIT_REQUESTS:
        return json_error("Rate limit exceeded. Try again later.", 429)

    timestamps.append(now)

    global _rate_limit_last_cleanup
    if now - _rate_limit_last_cleanup > _RATE_LIMIT_CLEANUP_INTERVAL:
        _rate_limit_last_cleanup = now
        stale = [k for k, v in rate_limits.items() if not v or v[-1] < cutoff]
        for k in stale:
            del rate_limits[k]
        if len(rate_limits) > _RATE_LIMIT_MAX_IPS:
            import heapq

            excess = len(rate_limits) - _RATE_LIMIT_MAX_IPS
            oldest = heapq.nsmallest(
                excess,
                rate_limits,
                key=lambda k: rate_limits[k][-1] if rate_limits[k] else 0,
            )
            for k in oldest:
                rate_limits.pop(k, None)

    return await handler(request)


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------


def create_app(daemon: SwarmDaemon, enable_web: bool = True) -> web.Application:
    """Create the aiohttp application with all routes."""
    app = web.Application(
        client_max_size=20 * 1024 * 1024,  # 20 MB for file uploads
        middlewares=[
            _security_headers_middleware,
            _csrf_middleware,
            _rate_limit_middleware,
            _config_auth_middleware,
        ],
    )
    app["daemon"] = daemon
    app["rate_limits"] = defaultdict(deque)  # ip -> deque of timestamps

    # Web dashboard routes (before API to allow / to serve dashboard)
    if enable_web:
        from swarm.web.app import setup_web_routes

        setup_web_routes(app)

    # Register all API + WebSocket routes from domain modules
    from swarm.server.routes import register_all

    register_all(app)

    return app


# ---------------------------------------------------------------------------
# Re-exports for backward compatibility (tests, bridge.py)
# ---------------------------------------------------------------------------
from swarm.server.routes.websocket import (  # noqa: E402, F401
    _WS_AUTH_LOCKOUT_SECONDS,
    _WS_AUTH_MAX_FAILURES,
    _handle_ws_command,
    _is_ws_auth_locked,
    _ws_auth_failures,
    record_ws_auth_failure,
    ws_authenticate,
)
