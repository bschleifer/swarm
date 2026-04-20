"""Regression tests for `src/swarm/hooks/approval_hook.sh`.

The shell script is the PreToolUse hook Claude Code invokes for every
tool call in a Swarm-managed worker session. Its guards decide whether
the daemon is queried at all — if they break, every worker stalls on
prompts the daemon can't see, or conversely the operator's interactive
session gets gated by drone rules it shouldn't be.

Task #211 added the ``SWARM_OPERATOR=1`` escape hatch — operator-driven
interactive workers carry ``SWARM_MANAGED=1`` from the PTY holder, so
without a second marker there was no way to honor the invariant
documented at the top of the script ("operator's own Claude Code session
is never gated by drone rules"). These tests lock the guard order in.
"""

from __future__ import annotations

import json
import os
import subprocess
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from threading import Thread
from typing import ClassVar

import pytest

_HOOK = Path(__file__).parent.parent / "src" / "swarm" / "hooks" / "approval_hook.sh"


class _CountingHandler(BaseHTTPRequestHandler):
    """Records every POST to /api/hooks/approval and replies with block."""

    calls: ClassVar[list[dict]] = []

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length else b""
        try:
            payload = json.loads(body)
        except Exception:
            payload = {"_raw": body.decode("utf-8", "replace")}
        type(self).calls.append({"path": self.path, "body": payload})
        resp = json.dumps({"decision": "block", "reason": "from-test"}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(resp)))
        self.end_headers()
        self.wfile.write(resp)

    def log_message(self, *args, **kwargs):
        return


@pytest.fixture
def daemon_stub():
    """Start a local HTTP server that records every approval call."""
    _CountingHandler.calls = []
    server = HTTPServer(("127.0.0.1", 0), _CountingHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", _CountingHandler.calls
    finally:
        server.shutdown()
        server.server_close()


def _run_hook(env: dict[str, str], payload: dict) -> subprocess.CompletedProcess:
    """Invoke approval_hook.sh with the given env and stdin payload."""
    merged = {**os.environ, **env}
    return subprocess.run(
        ["bash", str(_HOOK)],
        input=json.dumps(payload).encode(),
        env=merged,
        capture_output=True,
        timeout=10,
        check=False,
    )


def test_not_swarm_managed_exits_without_hitting_daemon(daemon_stub):
    """Sessions where SWARM_MANAGED != 1 must never query the daemon.

    This is the operator-outside-swarm path — a regular terminal that
    inherits nothing from the holder. The hook must be a transparent
    no-op there.
    """
    url, calls = daemon_stub
    result = _run_hook(
        env={"SWARM_URL": url, "SWARM_MANAGED": "", "SWARM_OPERATOR": ""},
        payload={"tool_name": "Bash", "tool_input": {"command": "git push origin main"}},
    )
    assert result.returncode == 0
    assert result.stdout == b""
    assert calls == []


def test_swarm_operator_bypass_skips_daemon(daemon_stub):
    """SWARM_OPERATOR=1 opts the operator's attached session out of drone rules.

    Task #211: the operator interacts with a Swarm-managed worker to do
    their own dev work. That worker carries SWARM_MANAGED=1 from the
    holder, so without a second marker the approval hook ran for every
    operator-driven tool call. SWARM_OPERATOR=1 is that second marker —
    when set, the hook must exit early without contacting the daemon.
    """
    url, calls = daemon_stub
    result = _run_hook(
        env={"SWARM_URL": url, "SWARM_MANAGED": "1", "SWARM_OPERATOR": "1"},
        payload={"tool_name": "Bash", "tool_input": {"command": "git push origin main"}},
    )
    assert result.returncode == 0
    assert result.stdout == b""
    assert calls == []


def test_managed_non_operator_queries_daemon(daemon_stub):
    """Autonomous workers (SWARM_MANAGED=1, no SWARM_OPERATOR) must query the daemon.

    Locks in the positive path — without this we could silently disable
    the whole approval flow via a typo in the guard order.
    """
    url, calls = daemon_stub
    result = _run_hook(
        env={"SWARM_URL": url, "SWARM_MANAGED": "1", "SWARM_OPERATOR": ""},
        payload={"tool_name": "Bash", "tool_input": {"command": "ls"}},
    )
    assert result.returncode == 0
    assert b'"decision":"block"' in result.stdout
    assert len(calls) == 1
    assert calls[0]["path"] == "/api/hooks/approval"
    assert calls[0]["body"]["tool_name"] == "Bash"
