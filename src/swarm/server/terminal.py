"""WebSocket terminal handler — re-exports from pty.bridge.

The actual implementation lives in ``swarm.pty.bridge``.  This module
exists so that ``swarm.server.api`` can continue to import from the
same path until the import is updated.
"""

from swarm.pty.bridge import _MAX_TERMINAL_SESSIONS, handle_terminal_ws

__all__ = ["handle_terminal_ws", "_MAX_TERMINAL_SESSIONS"]
