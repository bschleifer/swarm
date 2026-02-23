"""ProcessPool — manages connection to the pty-holder and WorkerProcess instances.

The pool is the daemon's single point of contact for all worker process
operations. It connects to the holder sidecar, spawns workers, and
routes output from the holder to the appropriate WorkerProcess instances.
"""

from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path

from swarm.logging import get_logger
from swarm.pty.holder import DEFAULT_SOCKET_PATH, start_holder_daemon
from swarm.pty.process import ProcessError, WorkerProcess

_log = get_logger("pty.pool")

_CONNECT_TIMEOUT = 5.0
_HOLDER_START_TIMEOUT = 5.0
_HOLDER_SOCKET_CHECK_DELAY = 0.1  # seconds between socket existence checks


class ProcessPool:
    """Manages the connection to the pty-holder and all WorkerProcess instances."""

    def __init__(self, socket_path: str | Path | None = None) -> None:
        self.socket_path = Path(socket_path) if socket_path else DEFAULT_SOCKET_PATH
        self._workers: dict[str, WorkerProcess] = {}
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._read_task: asyncio.Task | None = None
        self._connected = False
        # Pending command responses: maps a counter to a future
        self._pending: dict[int, asyncio.Future[dict]] = {}
        self._cmd_counter = 0
        self._cmd_lock = asyncio.Lock()

    async def connect(self) -> None:
        """Connect to the pty-holder's Unix socket."""
        try:
            self._reader, self._writer = await asyncio.wait_for(
                asyncio.open_unix_connection(str(self.socket_path)),
                timeout=_CONNECT_TIMEOUT,
            )
        except (ConnectionRefusedError, FileNotFoundError, asyncio.TimeoutError) as e:
            raise ProcessError(f"Cannot connect to holder at {self.socket_path}: {e}") from e

        self._connected = True
        self._read_task = asyncio.create_task(self._read_loop())
        _log.info("connected to holder at %s", self.socket_path)

    async def ensure_holder(self) -> None:
        """Start the holder if not running, then connect."""
        if self._connected:
            if await self._ping():
                return
            self._connected = False

        # Try connecting to existing holder
        if self.socket_path.exists():
            if await self._try_connect():
                return

        # Start a new holder and wait for it
        _log.info("starting holder daemon")
        start_holder_daemon(self.socket_path)

        for _ in range(int(_HOLDER_START_TIMEOUT / _HOLDER_SOCKET_CHECK_DELAY)):
            await asyncio.sleep(_HOLDER_SOCKET_CHECK_DELAY)
            if self.socket_path.exists() and await self._try_connect():
                return

        raise ProcessError("Failed to start holder daemon")

    async def _ping(self) -> bool:
        """Send a ping and return True if holder responds."""
        try:
            resp = await self._send_cmd({"cmd": "ping"})
            return bool(resp.get("pong"))
        except (ProcessError, OSError):
            return False

    async def _try_connect(self) -> bool:
        """Try to connect and ping the holder. Returns True on success."""
        try:
            await self.connect()
            if await self._ping():
                return True
        except (ProcessError, OSError):
            _log.info("stale holder socket — starting fresh")
        self._connected = False
        return False

    async def spawn(
        self,
        name: str,
        cwd: str,
        command: list[str] | None = None,
        cols: int = 200,
        rows: int = 50,
        shell_wrap: bool = False,
    ) -> WorkerProcess:
        """Spawn a new worker via the holder."""
        if not self._connected:
            raise ProcessError("Not connected to holder")

        resp = await self._send_cmd(
            {
                "cmd": "spawn",
                "name": name,
                "cwd": cwd,
                "command": command,
                "cols": cols,
                "rows": rows,
                "shell_wrap": shell_wrap,
            }
        )
        if not resp.get("ok"):
            raise ProcessError(f"Spawn failed: {resp.get('error', 'unknown')}")

        proc = WorkerProcess(name=name, cwd=cwd, cols=cols, rows=rows)
        proc.pid = resp["pid"]
        proc.is_alive = True
        proc._send_cmd = self._send_cmd
        self._workers[name] = proc
        _log.info("spawned worker %s (pid=%d)", name, proc.pid)
        return proc

    async def spawn_batch(
        self,
        workers: list[tuple[str, str]],
        command: list[str] | None = None,
        stagger_seconds: float = 2.0,
    ) -> list[WorkerProcess]:
        """Spawn multiple workers with staggered starts.

        Parameters
        ----------
        workers:
            List of (name, cwd) tuples.
        command:
            Command to run (default: ["claude", "--continue"]).
        stagger_seconds:
            Delay between spawns to avoid resource contention.
        """
        result: list[WorkerProcess] = []
        for i, (name, cwd) in enumerate(workers):
            proc = await self.spawn(name, cwd, command=command)
            result.append(proc)
            if i < len(workers) - 1:
                await asyncio.sleep(stagger_seconds)
        return result

    def get(self, name: str) -> WorkerProcess | None:
        """Get a worker by name."""
        return self._workers.get(name)

    def get_all(self) -> list[WorkerProcess]:
        """Get all worker processes."""
        return list(self._workers.values())

    async def kill(self, name: str) -> None:
        """Kill a worker and remove it from the pool."""
        proc = self._workers.get(name)
        if proc:
            await proc.kill()
            del self._workers[name]

    async def kill_all(self) -> None:
        """Kill all workers."""
        for name in list(self._workers):
            await self.kill(name)

    async def revive(
        self,
        name: str,
        cwd: str | None = None,
        command: list[str] | None = None,
        shell_wrap: bool = False,
    ) -> WorkerProcess | None:
        """Revive a dead worker by killing the old one and respawning."""
        old = self._workers.get(name)
        if old:
            cwd = cwd or old.cwd
            await self.kill(name)
        if not cwd:
            return None
        # Spawn fresh — caller provides the provider-specific command
        return await self.spawn(name, cwd, command=command, shell_wrap=shell_wrap)

    async def discover(self) -> list[WorkerProcess]:
        """Reconnect to existing workers in the holder.

        Used after daemon restart to resume tracking existing workers.
        """
        if not self._connected:
            raise ProcessError("Not connected to holder")

        resp = await self._send_cmd({"cmd": "list"})
        workers_data = resp.get("workers", [])

        for w in workers_data:
            name = w["name"]
            if name in self._workers:
                # Update liveness
                self._workers[name].is_alive = w["alive"]
                self._workers[name].exit_code = w.get("exit_code")
                continue
            # Create WorkerProcess for existing holder worker
            proc = WorkerProcess(
                name=name,
                cwd=w.get("cwd", "/tmp"),
                cols=int(w.get("cols", 200)),
                rows=int(w.get("rows", 50)),
            )
            proc.pid = w["pid"]
            proc.is_alive = w["alive"]
            proc.exit_code = w.get("exit_code")
            proc._send_cmd = self._send_cmd

            # Fetch the holder's buffer snapshot
            snap_resp = await self._send_cmd({"cmd": "snapshot", "name": name})
            if snap_resp.get("ok"):
                try:
                    data = base64.b64decode(snap_resp["data"])
                    proc.buffer.write(data)
                except Exception:
                    pass

            self._workers[name] = proc
            _log.info("discovered worker %s (pid=%d, alive=%s)", name, proc.pid, proc.is_alive)

        return list(self._workers.values())

    async def shutdown_holder(self) -> None:
        """Tell the holder to shut down (kills all workers)."""
        if self._connected:
            try:
                await self._send_cmd({"cmd": "shutdown"})
            except (ProcessError, OSError):
                pass
        self._workers.clear()
        await self._disconnect()

    async def _disconnect(self) -> None:
        """Close the socket connection."""
        self._connected = False
        if self._read_task:
            self._read_task.cancel()
            try:
                await self._read_task
            except asyncio.CancelledError:
                pass
            self._read_task = None
        if self._writer:
            try:
                self._writer.close()
                await self._writer.wait_closed()
            except Exception:
                pass
            self._writer = None
        self._reader = None
        # Fail all pending futures
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(ProcessError("Disconnected"))
        self._pending.clear()

    async def _send_cmd(self, msg: dict) -> dict:
        """Send a command and wait for the response.

        Uses a lock to serialize commands since the protocol is
        request-response on a single stream (interleaved with output broadcasts).
        """
        if not self._writer or not self._connected:
            raise ProcessError("Not connected to holder")

        async with self._cmd_lock:
            self._cmd_counter += 1
            cmd_id = self._cmd_counter
            fut: asyncio.Future[dict] = asyncio.get_running_loop().create_future()
            self._pending[cmd_id] = fut

            self._writer.write(json.dumps(msg).encode() + b"\n")
            await self._writer.drain()

        try:
            return await asyncio.wait_for(fut, timeout=10.0)
        except asyncio.TimeoutError:
            self._pending.pop(cmd_id, None)
            raise ProcessError(f"Command timed out: {msg.get('cmd')}")

    def _dispatch_message(self, msg: dict) -> None:
        """Route a single holder message to the appropriate handler."""
        if "output" in msg:
            proc = self._workers.get(msg["output"])
            if proc:
                try:
                    proc.feed_output(base64.b64decode(msg.get("data", "")))
                except Exception:
                    pass
        elif "died" in msg:
            name = msg["died"]
            proc = self._workers.get(name)
            if proc:
                proc.is_alive = False
                proc.exit_code = msg.get("exit_code")
                _log.info("worker %s died (exit_code=%s)", name, msg.get("exit_code"))
        elif self._pending:
            cmd_id = min(self._pending.keys())
            fut = self._pending.pop(cmd_id)
            if not fut.done():
                fut.set_result(msg)

    async def _read_loop(self) -> None:
        """Read messages from the holder, dispatching responses and output."""
        try:
            while self._connected and self._reader:
                line = await self._reader.readline()
                if not line:
                    break
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue
                self._dispatch_message(msg)
        except (asyncio.CancelledError, ConnectionError, OSError):
            pass
        finally:
            self._connected = False
