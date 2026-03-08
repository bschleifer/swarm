"""WorkerProcessProvider — Protocol for process pool abstractions."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from swarm.pty.process import WorkerProcess


class WorkerProcessProvider(Protocol):
    """Abstract interface for managing worker processes.

    The canonical implementation is :class:`~swarm.pty.pool.ProcessPool`.
    Using a Protocol allows tests to substitute lightweight fakes and
    prevents callers from reaching into pool internals (e.g. ``_disconnect``).
    """

    @property
    def is_connected(self) -> bool: ...

    async def connect(self) -> None: ...

    async def ensure_holder(self) -> None: ...

    async def spawn(
        self,
        name: str,
        cwd: str,
        command: list[str] | None = None,
        cols: int = 200,
        rows: int = 50,
        shell_wrap: bool = False,
    ) -> WorkerProcess: ...

    def get(self, name: str) -> WorkerProcess | None: ...

    def get_all(self) -> list[WorkerProcess]: ...

    async def kill(self, name: str) -> None: ...

    async def kill_all(self) -> None: ...

    async def revive(
        self,
        name: str,
        cwd: str | None = None,
        command: list[str] | None = None,
        shell_wrap: bool = False,
    ) -> WorkerProcess | None: ...

    async def discover(self) -> list[WorkerProcess]: ...

    async def disconnect(self) -> None: ...

    async def shutdown_holder(self) -> None: ...
