"""PWA routes: bee icon, service worker, offline page, manifest."""

from __future__ import annotations

from aiohttp import web

from swarm.web.app import STATIC_DIR


async def handle_bee_icon(request: web.Request) -> web.Response:
    """Serve the bee icon SVG with caching."""
    static = request.app.get("static_dir", STATIC_DIR)
    return web.FileResponse(
        static / "bee-icon.svg",
        headers={"Cache-Control": "public, max-age=86400"},
    )


async def handle_service_worker(request: web.Request) -> web.Response:
    """Serve sw.js from root path (service workers need root scope)."""
    static = request.app.get("static_dir", STATIC_DIR)
    return web.FileResponse(
        static / "sw.js",
        headers={"Content-Type": "application/javascript", "Cache-Control": "no-cache"},
    )


async def handle_offline_page(request: web.Request) -> web.Response:
    """Serve the PWA offline fallback page."""
    static = request.app.get("static_dir", STATIC_DIR)
    return web.FileResponse(static / "offline.html")


async def handle_manifest(request: web.Request) -> web.Response:
    """PWA manifest for add-to-homescreen support."""
    manifest = {
        "name": "Swarm",
        "short_name": "Swarm",
        "start_url": "/",
        "display": "standalone",
        "background_color": "#2A1B0E",
        "theme_color": "#D8A03D",
        "icons": [
            {
                "src": "/static/icon-192.png",
                "sizes": "192x192",
                "type": "image/png",
                "purpose": "any maskable",
            },
            {
                "src": "/static/icon-512.png",
                "sizes": "512x512",
                "type": "image/png",
                "purpose": "any maskable",
            },
        ],
    }
    return web.json_response(manifest)


def register(app: web.Application) -> None:
    """Register PWA routes."""
    app.router.add_get("/manifest.json", handle_manifest)
    app.router.add_get("/bee-icon.svg", handle_bee_icon)
    app.router.add_get("/sw.js", handle_service_worker)
    app.router.add_get("/offline.html", handle_offline_page)
