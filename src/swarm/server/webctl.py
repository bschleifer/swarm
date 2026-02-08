"""Web dashboard process control — start/stop/status."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
from pathlib import Path

_PID_DIR = Path.home() / ".swarm"
_WEB_PID_FILE = _PID_DIR / "web.pid"
_WEB_LOG_FILE = _PID_DIR / "web.log"


def web_is_running() -> int | None:
    """Return the PID if the web dashboard is running, else None."""
    if not _WEB_PID_FILE.exists():
        return None
    try:
        pid = int(_WEB_PID_FILE.read_text().strip())
        os.kill(pid, 0)  # Check if process exists
        return pid
    except (ProcessLookupError, ValueError, OSError):
        _WEB_PID_FILE.unlink(missing_ok=True)
        return None


def web_start(
    host: str = "localhost",
    port: int = 8080,
    config_path: str | None = None,
    session: str | None = None,
) -> tuple[bool, str]:
    """Start the web dashboard in the background.

    Returns (success, message).
    """
    pid = web_is_running()
    if pid is not None:
        return False, f"Already running (PID {pid}) — http://{host}:{port}"

    _PID_DIR.mkdir(parents=True, exist_ok=True)

    cmd = [sys.executable, "-m", "swarm", "serve", "--host", host, "--port", str(port)]
    if config_path:
        cmd.extend(["-c", config_path])
    if session:
        cmd.extend(["-s", session])

    log_file = open(_WEB_LOG_FILE, "a")
    proc = subprocess.Popen(
        cmd,
        stdout=log_file,
        stderr=log_file,
        start_new_session=True,
    )
    _WEB_PID_FILE.write_text(str(proc.pid))

    return True, f"Started (PID {proc.pid}) — http://{host}:{port}"


def web_stop() -> tuple[bool, str]:
    """Stop the background web dashboard.

    Returns (success, message).
    """
    if not _WEB_PID_FILE.exists():
        return False, "Not running"

    try:
        pid = int(_WEB_PID_FILE.read_text().strip())
    except (ValueError, OSError):
        _WEB_PID_FILE.unlink(missing_ok=True)
        return False, "Invalid PID file (cleaned up)"

    try:
        os.kill(pid, signal.SIGTERM)
        msg = f"Stopped (PID {pid})"
    except ProcessLookupError:
        msg = f"Process {pid} already gone"

    _WEB_PID_FILE.unlink(missing_ok=True)
    return True, msg
