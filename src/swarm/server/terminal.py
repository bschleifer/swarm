"""WebSocket terminal handler — interactive tmux attach via PTY."""

from __future__ import annotations

import asyncio
import fcntl
import hmac
import json
import os
import pty
import signal
import struct
import termios

from aiohttp import web

from swarm.logging import get_logger
from swarm.server.api import _get_api_password, _get_daemon

_log = get_logger("server.terminal")


def _log_task_exception(task: asyncio.Task[object]) -> None:
    """Log unhandled exceptions from fire-and-forget tasks."""
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        _log.error("fire-and-forget task failed: %s", exc, exc_info=exc)


_MAX_TERMINAL_SESSIONS = 20
_READ_SIZE = 4096
_SHUTDOWN_TIMEOUT = 5
_DEFAULT_COLS = 80
_DEFAULT_ROWS = 24


def _set_pty_size(fd: int, rows: int, cols: int) -> None:
    """Set the window size on a PTY file descriptor."""
    winsize = struct.pack("HHHH", rows, cols, 0, 0)
    fcntl.ioctl(fd, termios.TIOCSWINSZ, winsize)


async def handle_terminal_ws(request: web.Request) -> web.WebSocketResponse:  # noqa: C901
    """WebSocket endpoint for interactive terminal access to the tmux session.

    Attaches to the full tmux session (all panes visible).  An optional
    ``?pane=`` query parameter pre-selects a specific pane by its tmux
    pane-id (e.g. ``%3``).
    """
    daemon = _get_daemon(request)

    # --- Auth (same token pattern as /ws) ---
    password = _get_api_password(daemon)
    if password:
        token = request.query.get("token", "")
        if not hmac.compare_digest(token, password):
            return web.Response(status=401, text="Unauthorized")

    # --- Concurrency limit (checked before WS upgrade) ---
    sessions: set = request.app.setdefault("_terminal_sessions", set())
    if len(sessions) >= _MAX_TERMINAL_SESSIONS:
        return web.json_response(
            {"error": "Too many terminal sessions"},
            status=503,
        )

    main_session = daemon.config.session_name

    # Reserve the slot atomically before any await to prevent race conditions
    temp_session = f"swarm-web-{os.getpid()}-{id(request)}"
    sessions.add(temp_session)

    # --- Prepare WebSocket ---
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    daemon.terminal_ws_clients.add(ws)
    _log.info("terminal attach: session=%s temp=%s", main_session, temp_session)

    master_fd: int | None = None
    slave_fd: int | None = None
    proc: asyncio.subprocess.Process | None = None
    loop = asyncio.get_running_loop()

    try:
        # --- Create grouped tmux session ---
        # A grouped session shares all windows with the main session but has
        # its own independent current-window/pane selection, so it doesn't
        # interfere with anyone using another terminal.
        rc = await asyncio.create_subprocess_exec(
            "tmux",
            "new-session",
            "-d",
            "-t",
            main_session,
            "-s",
            temp_session,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr_data = await rc.communicate()
        if rc.returncode != 0:
            err = stderr_data.decode().strip()
            _log.warning("tmux new-session failed (rc=%d): %s", rc.returncode, err)
            await ws.close(code=1011, message=b"Failed to create tmux session")
            return ws

        _log.info("grouped session created: %s", temp_session)

        # Ensure mouse support is on for the temp session (session options
        # are not inherited from the target session in grouped mode).
        mouse_proc = await asyncio.create_subprocess_exec(
            "tmux",
            "set",
            "-t",
            temp_session,
            "mouse",
            "on",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await mouse_proc.wait()

        # Optionally pre-select a pane
        pane_id = request.query.get("pane", "")
        zoom_requested = request.query.get("zoom", "") == "1"
        did_zoom = False
        # App-level tracking: which temp session "owns" the zoom for each pane.
        # Prevents race conditions where an old WS cleanup unzooms a pane that
        # a new WS connection just zoomed (e.g. on page refresh).
        zoom_owners: dict[str, str] = request.app.setdefault("_zoom_owners", {})
        if pane_id:
            sel = await asyncio.create_subprocess_exec(
                "tmux",
                "select-pane",
                "-t",
                pane_id,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await sel.wait()

            # Zoom the pane so it fills the entire window (inline terminal)
            if zoom_requested:
                # Register as zoom owner BEFORE zooming — this tells any
                # concurrent cleanup that we've taken over this pane.
                zoom_owners[pane_id] = temp_session

                # If the window is already zoomed (leaked from a previous
                # connection whose cleanup was skipped or raced), unzoom
                # first to normalize state before zooming our target pane.
                chk = await asyncio.create_subprocess_exec(
                    "tmux",
                    "display-message",
                    "-t",
                    pane_id,
                    "-p",
                    "#{window_zoomed_flag}",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                chk_out, _ = await chk.communicate()
                if chk_out.strip() == b"1":
                    # Window is zoomed (possibly wrong pane) — unzoom first
                    pre_unzoom = await asyncio.create_subprocess_exec(
                        "tmux",
                        "resize-pane",
                        "-Z",
                        "-t",
                        pane_id,
                        stdout=asyncio.subprocess.DEVNULL,
                        stderr=asyncio.subprocess.DEVNULL,
                    )
                    await pre_unzoom.wait()

                zoom = await asyncio.create_subprocess_exec(
                    "tmux",
                    "resize-pane",
                    "-Z",
                    "-t",
                    pane_id,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                await zoom.wait()
                did_zoom = zoom.returncode == 0

        # --- PTY pair ---
        master_fd, slave_fd = pty.openpty()

        # Set initial window size BEFORE spawning tmux — a 0×0 terminal
        # causes tmux to exit immediately.
        _set_pty_size(slave_fd, _DEFAULT_ROWS, _DEFAULT_COLS)

        # Make master non-blocking for async reads
        flags = fcntl.fcntl(master_fd, fcntl.F_GETFL)
        fcntl.fcntl(master_fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)

        # --- Spawn tmux attach on the slave side ---
        env = os.environ.copy()
        env.setdefault("TERM", "xterm-256color")

        proc = await asyncio.create_subprocess_exec(
            "tmux",
            "attach-session",
            "-t",
            temp_session,
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            start_new_session=True,
            env=env,
        )
        # Parent no longer needs the slave fd
        os.close(slave_fd)
        slave_fd = None

        _log.info("tmux attach spawned: pid=%d temp=%s", proc.pid, temp_session)

        # --- I/O bridge ---
        closed = asyncio.Event()

        def _on_master_readable() -> None:
            """Called when the PTY master has data to read."""
            try:
                data = os.read(master_fd, _READ_SIZE)
                if data:
                    if not ws.closed:
                        fut = asyncio.ensure_future(ws.send_bytes(data))
                        fut.add_done_callback(_log_task_exception)
                else:
                    closed.set()
            except OSError:
                closed.set()

        loop.add_reader(master_fd, _on_master_readable)

        async def _ws_to_pty() -> None:
            """Forward WebSocket messages to the PTY."""
            async for msg in ws:
                if msg.type == web.WSMsgType.BINARY:
                    try:
                        os.write(master_fd, msg.data)
                    except OSError:
                        break
                elif msg.type == web.WSMsgType.TEXT:
                    # JSON resize message: {"cols": N, "rows": N}
                    try:
                        payload = json.loads(msg.data)
                        cols = int(payload.get("cols", 80))
                        rows = int(payload.get("rows", 24))
                        _set_pty_size(master_fd, rows, cols)
                        # Signal tmux about the size change — start_new_session=True
                        # detaches from the controlling terminal so the automatic
                        # SIGWINCH from TIOCSWINSZ doesn't reach it.
                        if proc and proc.pid and proc.returncode is None:
                            try:
                                os.kill(proc.pid, signal.SIGWINCH)
                            except OSError:
                                pass
                    except (ValueError, KeyError, OSError):
                        pass
                elif msg.type in (web.WSMsgType.CLOSE, web.WSMsgType.ERROR):
                    break
            closed.set()

        ws_task = asyncio.create_task(_ws_to_pty())

        # Wait for either side to close
        proc_wait = asyncio.create_task(proc.wait())
        await asyncio.wait(
            [ws_task, proc_wait, asyncio.create_task(closed.wait())],
            return_when=asyncio.FIRST_COMPLETED,
        )

        if proc.returncode is not None:
            _log.info("tmux attach exited: rc=%d temp=%s", proc.returncode, temp_session)

    finally:
        # --- Cleanup ---
        daemon.terminal_ws_clients.discard(ws)

        if master_fd is not None:
            try:
                loop.remove_reader(master_fd)
            except Exception:
                pass
            try:
                os.close(master_fd)
            except OSError:
                pass

        if slave_fd is not None:
            try:
                os.close(slave_fd)
            except OSError:
                pass

        if proc is not None and proc.returncode is None:
            try:
                proc.send_signal(signal.SIGHUP)
                try:
                    await asyncio.wait_for(proc.wait(), timeout=_SHUTDOWN_TIMEOUT)
                except asyncio.TimeoutError:
                    proc.kill()
                    await proc.wait()
            except ProcessLookupError:
                pass

        # Unzoom the pane if we zoomed it — but ONLY if we're still the owner.
        # A newer WS connection may have taken over the zoom for this pane
        # (e.g. on page refresh), so we must not toggle their zoom off.
        if did_zoom and pane_id:
            zoom_owners = request.app.get("_zoom_owners", {})
            is_owner = zoom_owners.get(pane_id) == temp_session
            if is_owner:
                zoom_owners.pop(pane_id, None)
            if is_owner:
                try:
                    chk = await asyncio.wait_for(
                        asyncio.create_subprocess_exec(
                            "tmux",
                            "display-message",
                            "-t",
                            pane_id,
                            "-p",
                            "#{window_zoomed_flag}",
                            stdout=asyncio.subprocess.PIPE,
                            stderr=asyncio.subprocess.DEVNULL,
                        ),
                        timeout=3,
                    )
                    stdout_data, _ = await asyncio.wait_for(chk.communicate(), timeout=3)
                    if stdout_data.strip() == b"1":
                        unzoom = await asyncio.wait_for(
                            asyncio.create_subprocess_exec(
                                "tmux",
                                "resize-pane",
                                "-Z",
                                "-t",
                                pane_id,
                                stdout=asyncio.subprocess.DEVNULL,
                                stderr=asyncio.subprocess.DEVNULL,
                            ),
                            timeout=3,
                        )
                        await asyncio.wait_for(unzoom.wait(), timeout=3)
                except Exception:
                    pass

        # Kill the temporary grouped session (timeout to avoid hang)
        try:
            kill_proc = await asyncio.wait_for(
                asyncio.create_subprocess_exec(
                    "tmux",
                    "kill-session",
                    "-t",
                    temp_session,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                ),
                timeout=3,
            )
            await asyncio.wait_for(kill_proc.wait(), timeout=3)
        except Exception:
            pass

        sessions.discard(temp_session)
        _log.info("terminal detached: temp=%s", temp_session)

        if not ws.closed:
            await ws.close()

    return ws
