"""Low-level tmux pane operations: capture-pane, send-keys."""

from __future__ import annotations

import asyncio

from swarm.logging import get_logger

_log = get_logger("tmux.cell")

_TMUX_TIMEOUT = 10  # seconds for tmux commands


class PaneGoneError(Exception):
    """Raised when a tmux pane no longer exists."""


class TmuxError(Exception):
    """Raised when a tmux command fails."""


async def _run_tmux(*args: str) -> str:
    proc = await asyncio.create_subprocess_exec(
        "tmux", *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=_TMUX_TIMEOUT
        )
    except asyncio.TimeoutError:
        proc.kill()
        _log.warning("tmux command timed out: tmux %s", " ".join(args))
        raise TmuxError(f"tmux command timed out: tmux {' '.join(args)}")
    if proc.returncode != 0:
        err_msg = stderr.decode().strip()
        # Some commands (like display-message on missing panes) are expected to fail
        _log.debug("tmux exited %d: %s (cmd: %s)", proc.returncode, err_msg, " ".join(args))
        raise TmuxError(f"tmux {' '.join(args)}: {err_msg}")
    return stdout.decode().rstrip("\n")


async def pane_exists(pane_id: str) -> bool:
    """Check if a tmux pane still exists."""
    try:
        result = await _run_tmux("display-message", "-p", "-t", pane_id, "#{pane_id}")
        return bool(result.strip())
    except TmuxError:
        return False


async def get_pane_id(target: str) -> str:
    """Get the actual pane ID (e.g. %5) for a tmux target like session:window.pane."""
    return await _run_tmux("display-message", "-p", "-t", target, "#{pane_id}")


async def capture_pane(pane_id: str, lines: int = 500) -> str:
    """Capture the last N lines from a tmux pane."""
    try:
        return await _run_tmux("capture-pane", "-p", "-t", pane_id, "-S", str(-lines))
    except TmuxError as e:
        raise PaneGoneError(str(e)) from e


async def get_pane_command(pane_id: str) -> str:
    """Get the foreground command running in a pane."""
    try:
        return await _run_tmux("display-message", "-p", "-t", pane_id, "#{pane_current_command}")
    except TmuxError as e:
        raise PaneGoneError(str(e)) from e


async def send_keys(pane_id: str, text: str, enter: bool = True) -> None:
    """Send text to a pane. Uses -l for literal text, then Enter separately."""
    # Send text as literal (prevents tmux key interpretation)
    await _run_tmux("send-keys", "-t", pane_id, "-l", text)
    if enter:
        # Small delay so the TUI processes the text before Enter
        await asyncio.sleep(0.05)
        await _run_tmux("send-keys", "-t", pane_id, "Enter")


async def send_interrupt(pane_id: str) -> None:
    """Send Ctrl-C to a pane."""
    await _run_tmux("send-keys", "-t", pane_id, "C-c")


async def send_enter(pane_id: str) -> None:
    """Send Enter to a pane."""
    await _run_tmux("send-keys", "-t", pane_id, "Enter")


async def send_escape(pane_id: str) -> None:
    """Send Escape to a pane (interrupt in Claude Code)."""
    await _run_tmux("send-keys", "-t", pane_id, "Escape")
