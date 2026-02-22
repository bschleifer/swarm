"""Shared HTTP helpers for the API and web layers."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from aiohttp import web

if TYPE_CHECKING:
    from swarm.server.daemon import SwarmDaemon

WORKER_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]+$")
MAX_QUERY_LIMIT = 1000


def json_error(msg: str, status: int = 400) -> web.Response:
    """Return a JSON error response."""
    return web.json_response({"error": msg}, status=status)


def get_daemon(request: web.Request) -> SwarmDaemon:
    """Extract the SwarmDaemon from the request's app dict."""
    return request.app["daemon"]


def parse_limit(request: web.Request, *, default: int = 50) -> int:
    """Parse a 'limit' query parameter, clamped to MAX_QUERY_LIMIT."""
    try:
        return min(int(request.query.get("limit", str(default))), MAX_QUERY_LIMIT)
    except ValueError:
        return default


def validate_worker_name(name: str) -> str | None:
    """Validate worker name, return error message or None."""
    if not name or not WORKER_NAME_RE.match(name):
        return f"Invalid worker name: '{name}'. Use alphanumeric, dash, or underscore only."
    return None


async def read_file_field(request: web.Request, field_name: str = "file") -> tuple[str, bytes]:
    """Read a multipart file upload field.

    Returns ``(filename, data)``.
    Raises ``ValueError`` on missing/wrong field or empty data so the
    caller's error-handling decorator can map it to a 400 response.
    """
    reader = await request.multipart()
    field = await reader.next()
    if not field or field.name != field_name:
        raise ValueError(f"{field_name} field required")
    filename = field.filename or "upload"
    data = await field.read(decode=False)
    if not data:
        raise ValueError("empty file")
    return filename, data
