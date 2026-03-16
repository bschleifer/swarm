"""Tests for session auth middleware and login routes."""

from __future__ import annotations

from unittest.mock import MagicMock

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from swarm.auth.session import _COOKIE_NAME, create_session_cookie


def _make_app(api_password: str = "test-password") -> web.Application:
    """Build a minimal app with session auth middleware for testing."""
    from swarm.server.api import _session_auth_middleware

    async def index(request: web.Request) -> web.Response:
        return web.Response(text="dashboard")

    async def api_health(request: web.Request) -> web.Response:
        return web.json_response({"status": "ok"})

    async def login_page(request: web.Request) -> web.Response:
        return web.Response(text="login page")

    async def static_file(request: web.Request) -> web.Response:
        return web.Response(text="static")

    app = web.Application(middlewares=[_session_auth_middleware])

    # Mock daemon
    daemon = MagicMock()
    daemon.config.domain = ""
    daemon.config.trust_proxy = False
    daemon.config.api_password = api_password
    app["daemon"] = daemon

    app.router.add_get("/", index)
    app.router.add_get("/api/health", api_health)
    app.router.add_get("/login", login_page)
    app.router.add_get("/static/foo.js", static_file)

    return app


async def test_login_page_exempt() -> None:
    async with TestClient(TestServer(_make_app())) as c:
        resp = await c.get("/login")
        assert resp.status == 200
        text = await resp.text()
        assert text == "login page"


async def test_static_exempt() -> None:
    async with TestClient(TestServer(_make_app())) as c:
        resp = await c.get("/static/foo.js")
        assert resp.status == 200


async def test_unauthenticated_browser_redirects() -> None:
    async with TestClient(TestServer(_make_app())) as c:
        resp = await c.get("/", headers={"Accept": "text/html"}, allow_redirects=False)
        assert resp.status == 302
        assert "/login" in resp.headers["Location"]


async def test_unauthenticated_api_returns_401() -> None:
    async with TestClient(TestServer(_make_app())) as c:
        resp = await c.get("/api/health", headers={"Accept": "application/json"})
        assert resp.status == 401


async def test_valid_session_cookie() -> None:
    async with TestClient(TestServer(_make_app())) as c:
        cookie_val, _ = create_session_cookie("test-password")
        resp = await c.get("/", cookies={_COOKIE_NAME: cookie_val})
        assert resp.status == 200
        text = await resp.text()
        assert text == "dashboard"


async def test_expired_cookie_redirects() -> None:
    async with TestClient(TestServer(_make_app())) as c:
        cookie_val, _ = create_session_cookie("test-password")
        # Tamper with expiry
        parts = cookie_val.split(".")
        cookie_val = "1000000000." + parts[1]  # expired timestamp
        resp = await c.get(
            "/",
            headers={"Accept": "text/html"},
            cookies={_COOKIE_NAME: cookie_val},
            allow_redirects=False,
        )
        assert resp.status == 302


async def test_bearer_token_auth() -> None:
    async with TestClient(TestServer(_make_app())) as c:
        resp = await c.get(
            "/api/health",
            headers={"Authorization": "Bearer test-password"},
        )
        assert resp.status == 200


async def test_wrong_bearer_token() -> None:
    async with TestClient(TestServer(_make_app())) as c:
        resp = await c.get(
            "/api/health",
            headers={
                "Authorization": "Bearer wrong",
                "Accept": "application/json",
            },
        )
        assert resp.status == 401


async def test_no_password_bypasses_auth() -> None:
    """When no api_password is configured, all routes are open (backward compat)."""
    async with TestClient(TestServer(_make_app(api_password=""))) as c:
        resp = await c.get("/")
        assert resp.status == 200
        text = await resp.text()
        assert text == "dashboard"


async def test_no_password_api_open() -> None:
    """API routes are open when no password is set."""
    async with TestClient(TestServer(_make_app(api_password=""))) as c:
        resp = await c.get("/api/health")
        assert resp.status == 200
