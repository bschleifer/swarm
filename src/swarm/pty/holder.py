"""PTY Holder Sidecar — owns PTY master FDs so workers survive daemon restarts.

Architecture::

    swarm daemon  <-->  Unix socket  <-->  pty-holder  <-->  PTY FDs  <-->  claude processes

The holder is a standalone process (double-forked daemon) that:
- Creates PTYs and forks child processes on request
- Holds PTY master FDs open (workers survive daemon death)
- Reads from each PTY master, streams output over the socket
- Accepts input commands from the daemon (write, resize, signal, kill)
- Buffers output while daemon is disconnected (ring buffer persists)

Protocol: JSON lines over Unix domain socket.
"""

from __future__ import annotations

import asyncio
import base64
import errno
import fcntl
import json
import os
import signal
import struct
import sys
import termios
from dataclasses import dataclass, field
from pathlib import Path

from swarm.logging import get_logger
from swarm.pty.buffer import RingBuffer

_log = get_logger("pty.holder")

_SWARM_DIR = Path.home() / ".swarm"
DEFAULT_SOCKET_PATH = _SWARM_DIR / "holder.sock"
DEFAULT_PID_PATH = _SWARM_DIR / "holder.pid"

_READ_SIZE = 4096
_DEFAULT_COLS = 200
_DEFAULT_ROWS = 50


class HolderError(Exception):
    """Raised when a holder operation fails."""


@dataclass
class HeldWorker:
    """A worker process owned by the holder."""

    name: str
    pid: int
    master_fd: int
    cwd: str
    command: list[str]
    cols: int = _DEFAULT_COLS
    rows: int = _DEFAULT_ROWS
    buffer: RingBuffer = field(default_factory=RingBuffer, repr=False)
    exit_code: int | None = None

    @property
    def alive(self) -> bool:
        if self.exit_code is not None:
            return False
        try:
            pid, status = os.waitpid(self.pid, os.WNOHANG)
            if pid != 0:
                self.exit_code = os.WEXITSTATUS(status) if os.WIFEXITED(status) else -1
                return False
        except ChildProcessError:
            self.exit_code = -1
            return False
        return True


def _resolve_user_path() -> str:
    """Build a PATH that includes common tool manager bin dirs.

    The holder is double-forked and may not inherit the user's full
    interactive-shell PATH (nvm, cargo, etc.).  Scan for well-known
    locations and prepend any that exist.
    """
    home = Path.home()
    extra_dirs: list[str] = []

    # nvm — pick the highest installed node version
    nvm_dir = home / ".nvm" / "versions" / "node"
    if nvm_dir.is_dir():
        versions = sorted(nvm_dir.iterdir(), reverse=True)
        for v in versions:
            bin_dir = v / "bin"
            if bin_dir.is_dir():
                extra_dirs.append(str(bin_dir))
                break

    # Other common tool managers
    for candidate in [
        home / ".cargo" / "bin",
        home / ".local" / "bin",
        home / ".deno" / "bin",
        Path("/usr/local/bin"),
    ]:
        if candidate.is_dir():
            extra_dirs.append(str(candidate))

    current = os.environ.get("PATH", "")
    current_set = set(current.split(":"))
    new_parts = [d for d in extra_dirs if d not in current_set]
    if new_parts:
        return ":".join(new_parts) + ":" + current
    return current


def _set_pty_size(fd: int, rows: int, cols: int) -> None:
    """Set the window size on a PTY file descriptor."""
    winsize = struct.pack("HHHH", rows, cols, 0, 0)
    fcntl.ioctl(fd, termios.TIOCSWINSZ, winsize)


def _make_nonblocking(fd: int) -> None:
    """Set a file descriptor to non-blocking mode."""
    flags = fcntl.fcntl(fd, fcntl.F_GETFL)
    fcntl.fcntl(fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)


class PtyHolder:
    """Holds PTY master FDs for worker processes.

    Designed to run as a standalone sidecar process. The daemon connects
    over a Unix domain socket and issues commands.
    """

    def __init__(self, socket_path: str | Path | None = None) -> None:
        self.socket_path = Path(socket_path) if socket_path else DEFAULT_SOCKET_PATH
        self.workers: dict[str, HeldWorker] = {}
        self._server: asyncio.AbstractServer | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._clients: list[asyncio.StreamWriter] = []
        self._running = False

    def spawn_worker(
        self,
        name: str,
        cwd: str,
        command: list[str] | None = None,
        cols: int = _DEFAULT_COLS,
        rows: int = _DEFAULT_ROWS,
    ) -> HeldWorker:
        """Create a PTY, fork a child process, and register the worker.

        This is synchronous — called from within the holder's event loop
        via a command handler, but the actual fork/pty work is synchronous.
        """
        if name in self.workers:
            old = self.workers[name]
            if old.alive:
                raise HolderError(f"Worker '{name}' already exists and is alive")
            # Clean up dead worker
            self._cleanup_worker(name)

        if not command:
            from swarm.providers import get_provider

            command = get_provider().worker_command()
        master_fd, slave_fd = os.openpty()

        _set_pty_size(slave_fd, rows, cols)

        pid = os.fork()
        if pid == 0:
            # Child process
            try:
                os.close(master_fd)
                os.setsid()
                # Set slave as controlling terminal
                fcntl.ioctl(slave_fd, termios.TIOCSCTTY, 0)
                os.dup2(slave_fd, 0)
                os.dup2(slave_fd, 1)
                os.dup2(slave_fd, 2)
                if slave_fd > 2:
                    os.close(slave_fd)
                os.chdir(cwd)
                env = os.environ.copy()
                env["TERM"] = "xterm-256color"
                env["PATH"] = _resolve_user_path()
                os.execvpe(command[0], command, env)
            except Exception:
                os._exit(1)
        else:
            # Parent
            os.close(slave_fd)
            _make_nonblocking(master_fd)

            worker = HeldWorker(
                name=name,
                pid=pid,
                master_fd=master_fd,
                cwd=cwd,
                command=command,
                cols=cols,
                rows=rows,
            )
            self.workers[name] = worker

            # Register reader for this worker's PTY output
            if self._loop:
                self._loop.add_reader(master_fd, self._on_pty_readable, name)

            _log.info("spawned worker %s: pid=%d, fd=%d", name, pid, master_fd)
            return worker

    def _on_pty_readable(self, name: str) -> None:
        """Called when a worker's PTY master has data."""
        worker = self.workers.get(name)
        if not worker:
            return
        try:
            data = os.read(worker.master_fd, _READ_SIZE)
        except OSError as e:
            if e.errno in (errno.EIO, errno.EBADF):
                # PTY closed — child exited
                if self._loop:
                    self._loop.remove_reader(worker.master_fd)
                worker.alive  # triggers waitpid
                self._broadcast_death(name, worker.exit_code)
                return
            raise
        if not data:
            if self._loop:
                self._loop.remove_reader(worker.master_fd)
            return
        worker.buffer.write(data)
        # Stream to connected clients
        self._broadcast_output(name, data)

    def _broadcast_output(self, name: str, data: bytes) -> None:
        """Send output data to all connected daemon clients."""
        msg = (
            json.dumps(
                {
                    "output": name,
                    "data": base64.b64encode(data).decode(),
                }
            )
            + "\n"
        )
        encoded = msg.encode()
        dead: list[asyncio.StreamWriter] = []
        for writer in list(self._clients):
            try:
                writer.write(encoded)
            except (ConnectionError, OSError):
                dead.append(writer)
        for w in dead:
            if w in self._clients:
                self._clients.remove(w)

    def _broadcast_death(self, name: str, exit_code: int | None) -> None:
        """Notify connected clients that a worker process has died."""
        msg = json.dumps({"died": name, "exit_code": exit_code}) + "\n"
        encoded = msg.encode()
        dead: list[asyncio.StreamWriter] = []
        for writer in list(self._clients):
            try:
                writer.write(encoded)
            except (ConnectionError, OSError):
                dead.append(writer)
        for w in dead:
            if w in self._clients:
                self._clients.remove(w)

    def _cleanup_worker(self, name: str) -> None:
        """Clean up a worker's resources."""
        worker = self.workers.pop(name, None)
        if not worker:
            return
        if self._loop:
            try:
                self._loop.remove_reader(worker.master_fd)
            except Exception:
                pass
        try:
            os.close(worker.master_fd)
        except OSError:
            pass

    def kill_worker(self, name: str) -> bool:
        """Kill a worker process and clean up."""
        worker = self.workers.get(name)
        if not worker:
            return False
        if worker.alive:
            try:
                os.killpg(os.getpgid(worker.pid), signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
            try:
                os.kill(worker.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            try:
                os.waitpid(worker.pid, 0)
            except ChildProcessError:
                pass
        self._cleanup_worker(name)
        return True

    def write_to_worker(self, name: str, data: bytes) -> bool:
        """Write data to a worker's PTY master."""
        worker = self.workers.get(name)
        if not worker or not worker.alive:
            return False
        try:
            os.write(worker.master_fd, data)
            return True
        except OSError:
            return False

    def resize_worker(self, name: str, cols: int, rows: int) -> bool:
        """Resize a worker's PTY."""
        worker = self.workers.get(name)
        if not worker:
            return False
        try:
            _set_pty_size(worker.master_fd, rows, cols)
            worker.cols = cols
            worker.rows = rows
            if worker.alive:
                os.killpg(os.getpgid(worker.pid), signal.SIGWINCH)
            return True
        except (OSError, ProcessLookupError):
            return False

    def signal_worker(self, name: str, sig: int) -> bool:
        """Send a signal to a worker's process group."""
        worker = self.workers.get(name)
        if not worker or not worker.alive:
            return False
        try:
            os.killpg(os.getpgid(worker.pid), sig)
            return True
        except (ProcessLookupError, PermissionError):
            return False

    def list_workers(self) -> list[dict[str, object]]:
        """Return metadata for all held workers."""
        result = []
        for w in self.workers.values():
            result.append(
                {
                    "name": w.name,
                    "pid": w.pid,
                    "alive": w.alive,
                    "exit_code": w.exit_code,
                    "cwd": w.cwd,
                    "command": w.command,
                    "cols": w.cols,
                    "rows": w.rows,
                }
            )
        return result

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        """Handle a connected daemon client."""
        self._clients.append(writer)
        _log.info("client connected (%d total)", len(self._clients))
        try:
            while True:
                line = await reader.readline()
                if not line:
                    break
                try:
                    msg = json.loads(line)
                    response = self._handle_command(msg)
                    writer.write(json.dumps(response).encode() + b"\n")
                    await writer.drain()
                except json.JSONDecodeError:
                    err = {"error": "invalid JSON"}
                    writer.write(json.dumps(err).encode() + b"\n")
                    await writer.drain()
        except (ConnectionError, OSError):
            pass
        finally:
            self._clients.remove(writer)
            _log.info("client disconnected (%d remaining)", len(self._clients))
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    def _handle_command(self, msg: dict) -> dict:  # noqa: C901
        """Dispatch a command from the daemon. Returns response dict."""
        cmd = msg.get("cmd", "")

        if cmd == "ping":
            return {"pong": True}

        if cmd == "spawn":
            name = msg.get("name", "")
            cwd = msg.get("cwd", "/tmp")
            command = msg.get("command")
            cols = int(msg.get("cols", _DEFAULT_COLS))
            rows = int(msg.get("rows", _DEFAULT_ROWS))
            try:
                worker = self.spawn_worker(name, cwd, command, cols, rows)
                return {"ok": True, "name": worker.name, "pid": worker.pid}
            except HolderError as e:
                return {"ok": False, "error": str(e)}
            except OSError as e:
                return {"ok": False, "error": str(e)}

        if cmd == "list":
            return {"workers": self.list_workers()}

        if cmd == "write":
            name = msg.get("name", "")
            try:
                data = base64.b64decode(msg.get("data", ""))
            except Exception:
                return {"ok": False, "error": "invalid base64"}
            ok = self.write_to_worker(name, data)
            return {"ok": ok}

        if cmd == "signal":
            name = msg.get("name", "")
            sig_name = msg.get("sig", "SIGINT")
            sig = getattr(signal, sig_name, signal.SIGINT)
            ok = self.signal_worker(name, sig)
            return {"ok": ok}

        if cmd == "resize":
            name = msg.get("name", "")
            cols = int(msg.get("cols", _DEFAULT_COLS))
            rows = int(msg.get("rows", _DEFAULT_ROWS))
            ok = self.resize_worker(name, cols, rows)
            return {"ok": ok}

        if cmd == "kill":
            name = msg.get("name", "")
            ok = self.kill_worker(name)
            return {"ok": ok}

        if cmd == "snapshot":
            name = msg.get("name", "")
            worker = self.workers.get(name)
            if not worker:
                return {"ok": False, "error": "worker not found"}
            data = worker.buffer.snapshot()
            return {"ok": True, "data": base64.b64encode(data).decode()}

        if cmd == "shutdown":
            self._shutdown_all()
            return {"ok": True}

        return {"error": f"unknown command: {cmd}"}

    def _shutdown_all(self) -> None:
        """Kill all workers and stop the holder."""
        for name in list(self.workers):
            self.kill_worker(name)
        self._running = False
        if self._server:
            self._server.close()

    async def serve(self) -> None:
        """Start the Unix socket server and run until stopped."""
        self._loop = asyncio.get_running_loop()
        self._running = True

        # Ensure socket dir exists
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)

        # Remove stale socket
        if self.socket_path.exists():
            self.socket_path.unlink()

        self._server = await asyncio.start_unix_server(
            self._handle_client,
            path=str(self.socket_path),
        )
        # Restrict socket permissions
        os.chmod(str(self.socket_path), 0o700)

        _log.info("holder listening on %s", self.socket_path)

        try:
            while self._running:
                await asyncio.sleep(1)
                # Reap dead children
                self._reap_children()
        except asyncio.CancelledError:
            pass
        finally:
            self._server.close()
            await self._server.wait_closed()
            if self.socket_path.exists():
                self.socket_path.unlink()
            _log.info("holder stopped")

    def _reap_children(self) -> None:
        """Check for dead child processes and update exit codes."""
        for worker in list(self.workers.values()):
            if worker.exit_code is None:
                was_alive = worker.alive  # triggers waitpid via property
                if not was_alive and worker.exit_code is not None:
                    self._broadcast_death(worker.name, worker.exit_code)


def start_holder_daemon(socket_path: str | Path | None = None) -> int:
    """Double-fork to start the holder as a background daemon.

    Returns the holder's PID.
    """
    socket_path = Path(socket_path) if socket_path else DEFAULT_SOCKET_PATH
    pid_path = socket_path.with_suffix(".pid")

    # First fork
    pid = os.fork()
    if pid > 0:
        # Parent waits briefly for the daemon to start
        return pid

    # First child: create new session
    os.setsid()

    # Second fork
    pid = os.fork()
    if pid > 0:
        # Write PID file
        pid_path.parent.mkdir(parents=True, exist_ok=True)
        pid_path.write_text(str(pid))
        os._exit(0)

    # Daemon process
    # Redirect stdio
    devnull = os.open(os.devnull, os.O_RDWR)
    os.dup2(devnull, 0)
    os.dup2(devnull, 1)
    os.dup2(devnull, 2)
    os.close(devnull)

    holder = PtyHolder(socket_path)
    asyncio.run(holder.serve())
    sys.exit(0)
