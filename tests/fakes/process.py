"""Fake WorkerProcess for unit tests.

Replaces ``tests/fakes/tmux.py`` — provides an in-memory process
simulation so tests can exercise pilot/daemon/manager code without
a real PTY holder.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from swarm.pty.buffer import RingBuffer


@dataclass
class FakeWorkerProcess:
    """In-memory WorkerProcess replacement for tests.

    Usage::

        fake = FakeWorkerProcess(name="alice")
        fake.set_content("Some output\\n")
        worker.process = fake
    """

    name: str
    cwd: str = "/tmp"
    cols: int = 200
    rows: int = 50
    pid: int | None = 1234
    buffer: RingBuffer = field(default_factory=RingBuffer, repr=False)
    _alive: bool = True
    _exit_code: int | None = None
    _foreground_command: str = "claude"
    _child_foreground_command: str = "claude"
    keys_sent: list[str] = field(default_factory=list, repr=False)
    _killed: bool = False

    def set_content(self, text: str) -> None:
        """Set the buffer content (convenience for tests)."""
        self.buffer.clear()
        self.buffer.write(text.encode("utf-8"))

    def get_content(self, lines: int = 35) -> str:
        """Read from the buffer, like the real WorkerProcess."""
        return self.buffer.get_lines(lines)

    def get_foreground_command(self) -> str:
        return self._foreground_command

    def get_child_foreground_command(self) -> str:
        return self._child_foreground_command

    def feed_output(self, data: bytes) -> None:
        self.buffer.write(data)

    async def send_keys(self, text: str, enter: bool = True) -> None:
        full = text + ("\n" if enter else "")
        self.keys_sent.append(full)

    async def send_enter(self) -> None:
        self.keys_sent.append("\n")

    async def send_interrupt(self) -> None:
        self.keys_sent.append("<C-c>")

    async def send_escape(self) -> None:
        self.keys_sent.append("<Esc>")

    async def resize(self, cols: int, rows: int) -> None:
        self.cols = cols
        self.rows = rows

    async def kill(self) -> None:
        self._alive = False
        self._killed = True

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

    def subscribe_ws(self, ws: object) -> None:
        pass

    def unsubscribe_ws(self, ws: object) -> None:
        pass
