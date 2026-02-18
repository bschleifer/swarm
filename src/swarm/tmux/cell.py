"""Low-level tmux pane operations: capture-pane, send-keys."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from swarm.logging import get_logger

_log = get_logger("tmux.cell")

_TMUX_TIMEOUT = 10  # seconds for tmux commands


@dataclass(frozen=True, slots=True)
class PaneSnapshot:
    """Metadata for a single tmux pane from a batch list-panes call."""

    pane_id: str
    command: str
    zoomed: bool
    active: bool
    window_index: str = ""


class PaneGoneError(Exception):
    """Raised when a tmux pane no longer exists."""


class TmuxError(Exception):
    """Raised when a tmux command fails."""


#: Exception tuple for all recoverable tmux/subprocess errors.
TMUX_ERRORS = (OSError, asyncio.TimeoutError, PaneGoneError, TmuxError)


async def run_tmux(*args: str) -> str:
    proc = await asyncio.create_subprocess_exec(
        "tmux",
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=_TMUX_TIMEOUT)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
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
        result = await run_tmux("display-message", "-p", "-t", pane_id, "#{pane_id}")
        return bool(result.strip())
    except TmuxError:
        return False


async def get_pane_id(target: str) -> str:
    """Get the actual pane ID (e.g. %5) for a tmux target like session:window.pane."""
    return await run_tmux("display-message", "-p", "-t", target, "#{pane_id}")


def _is_pane_gone(error: TmuxError) -> bool:
    """Check if a TmuxError indicates the pane no longer exists."""
    msg = str(error).lower()
    return (
        "can't find pane" in msg
        or "no pane" in msg
        or "pane index" in msg
        or "session not found" in msg
        or "no such" in msg
        or "can't find window" in msg
    )


async def capture_pane(pane_id: str, lines: int = 500) -> str:
    """Capture the last N lines from a tmux pane."""
    try:
        return await run_tmux("capture-pane", "-p", "-t", pane_id, "-S", str(-lines))
    except TmuxError as e:
        if _is_pane_gone(e):
            raise PaneGoneError(str(e)) from e
        raise


async def get_pane_command(pane_id: str) -> str:
    """Get the foreground command running in a pane."""
    try:
        return await run_tmux("display-message", "-p", "-t", pane_id, "#{pane_current_command}")
    except TmuxError as e:
        if _is_pane_gone(e):
            raise PaneGoneError(str(e)) from e
        raise


async def _exit_copy_mode(pane_id: str) -> None:
    """Exit copy/scroll mode if active.

    tmux send-keys hangs indefinitely when a pane is in copy mode (scrolled up).
    Calling ``copy-mode -q`` cleanly exits the mode; it's a no-op error if the
    pane is not in copy mode.
    """
    try:
        await run_tmux("copy-mode", "-q", "-t", pane_id)
    except TmuxError:
        pass  # Not in copy mode — expected


async def send_keys(pane_id: str, text: str, enter: bool = True) -> None:
    """Send text to a pane. Uses -l for literal text, then Enter separately."""
    await _exit_copy_mode(pane_id)
    # Send text as literal (prevents tmux key interpretation)
    await run_tmux("send-keys", "-t", pane_id, "-l", text)
    if enter:
        # Small delay so the UI processes the text before Enter
        await asyncio.sleep(0.05)
        await run_tmux("send-keys", "-t", pane_id, "Enter")


async def send_interrupt(pane_id: str) -> None:
    """Send Ctrl-C to a pane."""
    await _exit_copy_mode(pane_id)
    await run_tmux("send-keys", "-t", pane_id, "C-c")


async def send_enter(pane_id: str) -> None:
    """Send Enter to a pane."""
    await _exit_copy_mode(pane_id)
    await run_tmux("send-keys", "-t", pane_id, "Enter")


async def get_zoomed_pane(session_name: str) -> str | None:
    """Return pane_id of the active pane in a zoomed window, or None."""
    try:
        raw = await run_tmux(
            "list-panes",
            "-s",
            "-t",
            session_name,
            "-F",
            "#{pane_id}\t#{pane_active}\t#{window_zoomed_flag}",
        )
    except TmuxError:
        return None
    for line in raw.splitlines():
        parts = line.split("\t")
        if len(parts) >= 3 and parts[1] == "1" and parts[2] == "1":
            return parts[0]
    return None


async def batch_pane_info(session_name: str) -> dict[str, PaneSnapshot]:
    """Fetch metadata for all panes in a session with a single tmux call.

    Returns a dict keyed by pane_id.  A pane missing from the result means
    it no longer exists (replaces individual ``pane_exists`` checks).
    The ``command`` field replaces ``get_pane_command``, and the ``zoomed``
    + ``active`` fields replace ``get_zoomed_pane``.
    """
    try:
        raw = await run_tmux(
            "list-panes",
            "-s",
            "-t",
            session_name,
            "-F",
            "#{pane_id}\t#{pane_current_command}\t#{window_zoomed_flag}\t#{pane_active}\t#{window_index}",
        )
    except TmuxError:
        return {}
    result: dict[str, PaneSnapshot] = {}
    for line in raw.splitlines():
        parts = line.split("\t")
        if len(parts) < 5:
            continue
        pane_id, command, zoomed_flag, active_flag, win_idx = (
            parts[0],
            parts[1],
            parts[2],
            parts[3],
            parts[4],
        )
        result[pane_id] = PaneSnapshot(
            pane_id=pane_id,
            command=command,
            zoomed=zoomed_flag == "1",
            active=active_flag == "1",
            window_index=win_idx,
        )
    return result


async def send_escape(pane_id: str) -> None:
    """Send Escape to a pane (interrupt in Claude Code)."""
    await run_tmux("send-keys", "-t", pane_id, "Escape")
