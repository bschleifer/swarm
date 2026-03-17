"""Install and configure Caddy as a reverse proxy for swarm."""

from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path

_log = logging.getLogger("swarm.reverse_proxy")

_CADDYFILE_PATH = Path("/etc/caddy/Caddyfile")

_CADDYFILE_TEMPLATE = """\
{domain} {{
	reverse_proxy localhost:{port}
}}
"""


def caddy_installed() -> bool:
    """Return True if caddy is on PATH."""
    return shutil.which("caddy") is not None


def install_caddy() -> bool:
    """Install Caddy via apt. Requires sudo. Returns True on success."""
    steps = [
        ["sudo", "apt-get", "install", "-y", "debian-keyring", "debian-archive-keyring", "curl"],
        [
            "bash",
            "-c",
            "curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' "
            "| sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg",
        ],
        [
            "bash",
            "-c",
            "curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' "
            "| sudo tee /etc/apt/sources.list.d/caddy-stable.list > /dev/null",
        ],
        ["sudo", "apt-get", "update"],
        ["sudo", "apt-get", "install", "-y", "caddy"],
    ]
    for cmd in steps:
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            _log.error("Caddy install step failed: %s\n%s", " ".join(cmd), result.stderr)
            return False
    return True


def write_caddyfile(domain: str, port: int = 9090) -> bool:
    """Write the Caddyfile for reverse proxying to swarm. Requires sudo."""
    content = _CADDYFILE_TEMPLATE.format(domain=domain, port=port)
    result = subprocess.run(
        ["sudo", "tee", str(_CADDYFILE_PATH)],
        input=content,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        _log.error("Failed to write Caddyfile: %s", result.stderr)
        return False
    return True


def reload_caddy() -> bool:
    """Restart the Caddy systemd service. Requires sudo."""
    result = subprocess.run(
        ["sudo", "systemctl", "restart", "caddy"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        _log.error("Failed to restart caddy: %s", result.stderr)
        return False

    result = subprocess.run(
        ["sudo", "systemctl", "enable", "caddy"],
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


def setup_caddy(domain: str, port: int = 9090) -> bool:
    """Full Caddy setup: install if needed, write config, start service.

    Returns True if everything succeeded.
    """
    if not caddy_installed():
        if not install_caddy():
            return False

    if not write_caddyfile(domain, port):
        return False

    return reload_caddy()


def caddy_status() -> str | None:
    """Return Caddy's systemd status, or None if not installed."""
    if not caddy_installed():
        return None
    result = subprocess.run(
        ["sudo", "systemctl", "status", "caddy"],
        capture_output=True,
        text=True,
    )
    return result.stdout or result.stderr or "unknown"
