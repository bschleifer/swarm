"""WorkerProcess — client-side representation of a worker in the pty-holder.

Each WorkerProcess communicates with the holder over the shared Unix socket.
It maintains a local RingBuffer fed by the holder's output stream, and
manages WebSocket subscribers for the web terminal.
"""

from __future__ import annotations

import asyncio
import base64
import signal
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from aiohttp import web

from swarm.logging import get_logger
from swarm.pty.buffer import RingBuffer

_log = get_logger("pty.process")

# Delay between text and Enter so TUI apps can process input
_INPUT_DRAIN_DELAY = 0.05  # seconds

# Type alias for the pool command sender bound method
_SendCmd = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


class ProcessError(Exception):
    """Raised when a PTY process operation fails."""


class WorkerProcess:
    """Client-side handle for a worker running in the pty-holder.

    Parameters
    ----------
    name:
        Unique worker name.
    cwd:
        Working directory for the worker process.
    cols, rows:
        Initial terminal dimensions.
    """

    def __init__(
        self,
        name: str,
        cwd: str,
        cols: int = 200,
        rows: int = 50,
    ) -> None:
        self.name = name
        self.cwd = cwd
        self.cols = cols
        self.rows = rows
        self.buffer = RingBuffer()
        self.pid: int | None = None
        self._alive = False
        self._exit_code: int | None = None
        self._ws_subscribers: set[web.WebSocketResponse] = set()
        # Per-subscriber ordered send queues
        self._ws_queues: dict[int, asyncio.Queue[bytes]] = {}
        self._ws_tasks: dict[int, asyncio.Task[None]] = {}
        _WS_MAX_BACKLOG = 50
        self._ws_max_backlog = _WS_MAX_BACKLOG
        # Set by the pool when connected
        self._send_cmd: _SendCmd | None = None
        # Terminal-active guard: prevents automated input while user is typing
        self._terminal_active: bool = False
        self._last_user_input: float = 0.0

    _USER_ACTIVE_WINDOW = 2.0

    @property
    def is_user_active(self) -> bool:
        """True when a user has the web terminal open and recently typed."""
        import time

        elapsed = time.time() - self._last_user_input
        return self._terminal_active and elapsed < self._USER_ACTIVE_WINDOW

    def mark_user_input(self) -> None:
        """Record that the user just sent input via the web terminal."""
        import time

        self._last_user_input = time.time()

    def set_terminal_active(self, active: bool) -> None:
        """Set whether a web terminal session is connected."""
        self._terminal_active = active

    def feed_output(self, data: bytes) -> None:
        """Feed output data from the holder into the local buffer and WS subscribers."""
        self.buffer.write(data)
        # Broadcast to WebSocket subscribers
        dead: list[web.WebSocketResponse] = []
        subscribers = self._ws_subscribers
        for ws in list(subscribers):
            if ws.closed:
                dead.append(ws)
                continue
            try:
                ws_id = id(ws)
                queue = self._ws_queues.get(ws_id)
                if not queue:
                    queue = asyncio.Queue(maxsize=self._ws_max_backlog)
                    self._ws_queues[ws_id] = queue
                    self._ws_tasks[ws_id] = asyncio.get_running_loop().create_task(
                        self._ws_sender(ws, queue)
                    )
                if queue.full():
                    _log.warning(
                        "dropping slow WS subscriber (queue %d)",
                        queue.qsize(),
                    )
                    dead.append(ws)
                    continue
                queue.put_nowait(data)
            except RuntimeError:
                dead.append(ws)
            except Exception:
                _log.debug("WS send setup failed for subscriber", exc_info=True)
                dead.append(ws)
        for ws in dead:
            self._drop_ws(ws)

    async def _ws_sender(
        self,
        ws: web.WebSocketResponse,
        queue: asyncio.Queue[bytes],
    ) -> None:
        """Send queued WS output in-order to avoid interleaving ANSI streams."""
        ws_id = id(ws)
        try:
            while True:
                data = await queue.get()
                if ws.closed:
                    break
                await ws.send_bytes(data)
        except Exception:
            _log.debug("WS sender failed", exc_info=True)
        finally:
            self._drop_ws(ws)

    def _drop_ws(self, ws: web.WebSocketResponse) -> None:
        """Remove a WS subscriber and stop its sender task."""
        ws_id = id(ws)
        self._ws_subscribers.discard(ws)
        task = self._ws_tasks.pop(ws_id, None)
        if task:
            task.cancel()
        self._ws_queues.pop(ws_id, None)

    def get_content(self, lines: int = 35) -> str:
        """Read the last N lines from the local ring buffer (synchronous).

        Used by ``classify_worker_output()`` for state detection.
        Zero subprocess calls — reads from in-process memory.
        """
        return self.buffer.get_lines(lines)

    def get_foreground_command(self) -> str:
        """Read the foreground command from /proc/{pid}/stat.

        Returns the command name (e.g. 'claude', 'bash') or '' on failure.
        """
        if not self.pid:
            return ""
        try:
            stat = Path(f"/proc/{self.pid}/stat").read_text()
            # Format: "pid (comm) state ..." — extract comm
            start = stat.index("(") + 1
            end = stat.index(")")
            return stat[start:end]
        except (FileNotFoundError, ValueError, OSError):
            return ""

    def get_child_foreground_command(self) -> str:
        """Read the foreground command of the first child process.

        The PTY holder forks a child which runs the actual command.
        The child's children are what we care about for state detection
        (e.g. 'claude' vs 'bash' after claude exits).
        """
        if not self.pid:
            return ""
        try:
            # Find child PIDs
            children_path = Path(f"/proc/{self.pid}/task/{self.pid}/children")
            if children_path.exists():
                children = children_path.read_text().strip().split()
                if children:
                    child_pid = children[0]
                    stat = Path(f"/proc/{child_pid}/stat").read_text()
                    start = stat.index("(") + 1
                    end = stat.index(")")
                    return stat[start:end]
        except (FileNotFoundError, ValueError, OSError):
            pass
        # Fallback to own command
        return self.get_foreground_command()

    async def send_keys(self, text: str, enter: bool = True) -> None:
        """Send text to the worker's PTY.

        Text and Enter are sent as separate writes so that interactive
        TUI apps (e.g. Claude Code's slash-command autocomplete) have
        time to process the input before receiving the carriage return.
        """
        await self._write(text.encode("utf-8"))
        if enter:
            await asyncio.sleep(_INPUT_DRAIN_DELAY)
            await self._write(b"\r")

    async def send_enter(self) -> None:
        """Send Enter (carriage return) to the worker."""
        await self._write(b"\r")

    async def send_interrupt(self) -> None:
        """Send SIGINT to the worker's process group."""
        await self._signal(signal.SIGINT)

    async def send_escape(self) -> None:
        """Send ESC byte to the worker's PTY."""
        await self._write(b"\x1b")

    async def send_sigwinch(self) -> None:
        """Send SIGWINCH to force TUI redraw."""
        await self._signal(signal.SIGWINCH)

    async def resize(self, cols: int, rows: int) -> None:
        """Resize the worker's PTY."""
        if cols == self.cols and rows == self.rows:
            return  # skip no-op resize, avoids SIGWINCH
        if not self._send_cmd:
            raise ProcessError(f"worker {self.name!r}: not connected to holder")
        self.cols = cols
        self.rows = rows
        self.buffer.resize(cols, rows)
        await self._send_cmd(
            {
                "cmd": "resize",
                "name": self.name,
                "cols": cols,
                "rows": rows,
            }
        )

    def subscribe_ws(self, ws: web.WebSocketResponse) -> None:
        """Add a WebSocket subscriber for real-time output."""
        self._ws_subscribers.add(ws)

    def unsubscribe_ws(self, ws: web.WebSocketResponse) -> None:
        """Remove a WebSocket subscriber."""
        self._drop_ws(ws)

    def subscribe_and_snapshot(self, ws: web.WebSocketResponse) -> bytes:
        """Add a WebSocket subscriber and return the current buffer snapshot.

        This is done in one atomic step (relative to the event loop) to
        avoid missing or duplicating data between the snapshot and live stream.
        """
        # Both buffer.snapshot() and adding to the set are synchronous.
        # Since feed_output is also called from the same event loop,
        # no data can arrive between these two lines.
        snapshot = self.buffer.snapshot()
        self._ws_subscribers.add(ws)
        return snapshot

    async def kill(self) -> None:
        """Kill the worker process via the holder."""
        if self._send_cmd:
            await self._send_cmd({"cmd": "kill", "name": self.name})
        self._alive = False

    @property
    def is_alive(self) -> bool:
        return self._alive

    @is_alive.setter
    def is_alive(self, value: bool) -> None:
        self._alive = value

    @property
    def exit_code(self) -> int | None:
        return self._exit_code

    @exit_code.setter
    def exit_code(self, value: int | None) -> None:
        self._exit_code = value

    async def _write(self, data: bytes) -> None:
        """Write raw bytes to the worker's PTY via the holder."""
        if not self._send_cmd:
            raise ProcessError(f"Worker '{self.name}' not connected to holder")
        resp = await self._send_cmd(
            {
                "cmd": "write",
                "name": self.name,
                "data": base64.b64encode(data).decode(),
            }
        )
        if not resp.get("ok"):
            raise ProcessError(f"Write failed for '{self.name}': {resp.get('error', 'unknown')}")

    async def _signal(self, sig: int) -> None:
        """Send a signal to the worker via the holder."""
        if not self._send_cmd:
            raise ProcessError(f"Worker '{self.name}' not connected to holder")
        sig_name = signal.Signals(sig).name
        resp = await self._send_cmd(
            {
                "cmd": "signal",
                "name": self.name,
                "sig": sig_name,
            }
        )
        if not resp.get("ok"):
            raise ProcessError(
                f"Signal {sig_name} failed for '{self.name}': {resp.get('error', 'unknown')}"
            )
