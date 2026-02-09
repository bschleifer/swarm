"""Fake tmux runner for integration tests.

Replaces ``swarm.tmux.cell.run_tmux`` with an in-memory simulator
so tests can exercise hive/cell/manager code without a real tmux server.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class FakePane:
    pane_id: str
    window_index: str = "0"
    pane_index: str = "0"
    path: str = "/tmp"
    command: str = "claude"
    content: str = ""
    options: dict[str, str] = field(default_factory=dict)
    keys_sent: list[str] = field(default_factory=list)


class FakeTmux:
    """In-memory tmux simulator.

    Usage::

        fake = FakeTmux()
        fake.add_pane(FakePane(pane_id="%0", ...))

        # Monkey-patch the real runner
        monkeypatch.setattr("swarm.tmux.cell.run_tmux", fake.run)
    """

    def __init__(self) -> None:
        self.panes: dict[str, FakePane] = {}
        self.sessions: dict[str, list[str]] = {}  # session_name -> [pane_ids]

    def add_pane(self, pane: FakePane, session: str = "swarm") -> None:
        self.panes[pane.pane_id] = pane
        self.sessions.setdefault(session, []).append(pane.pane_id)

    def remove_pane(self, pane_id: str) -> None:
        if pane_id in self.panes:
            del self.panes[pane_id]
        for pane_ids in self.sessions.values():
            if pane_id in pane_ids:
                pane_ids.remove(pane_id)

    async def run(self, *args: str) -> str:  # noqa: C901
        """Dispatch tmux commands to fake handlers."""
        if not args:
            return ""
        cmd = args[0]

        if cmd == "capture-pane":
            return self._capture_pane(args)
        elif cmd == "display-message":
            return self._display_message(args)
        elif cmd == "send-keys":
            return self._send_keys(args)
        elif cmd == "list-panes":
            return self._list_panes(args)
        elif cmd == "list-sessions":
            return self._list_sessions(args)
        elif cmd == "has-session":
            return self._has_session(args)
        elif cmd == "set":
            return self._set_option(args)
        elif cmd == "show":
            return self._show_option(args)
        elif cmd == "list-windows":
            return self._list_windows(args)
        elif cmd in (
            "new-session",
            "split-window",
            "new-window",
            "select-layout",
            "kill-session",
            "kill-pane",
            "rename-window",
            "bind-key",
            "list-clients",
        ):
            return ""
        return ""

    def _get_pane_id(self, args: tuple[str, ...]) -> str | None:
        for i, a in enumerate(args):
            if a == "-t" and i + 1 < len(args):
                target = args[i + 1]
                # Handle "session:window.pane" format
                if target in self.panes:
                    return target
                # Try to find by prefix
                for pid in self.panes:
                    if target.endswith(pid):
                        return pid
        return None

    def _capture_pane(self, args: tuple[str, ...]) -> str:
        pane_id = self._get_pane_id(args)
        if pane_id and pane_id in self.panes:
            return self.panes[pane_id].content
        return ""

    def _display_message(self, args: tuple[str, ...]) -> str:
        pane_id = self._get_pane_id(args)
        if pane_id and pane_id in self.panes:
            fmt = args[-1] if args else ""
            if "pane_current_command" in fmt:
                return self.panes[pane_id].command
        return ""

    def _send_keys(self, args: tuple[str, ...]) -> str:
        pane_id = self._get_pane_id(args)
        if pane_id and pane_id in self.panes:
            # Collect what was sent
            text_parts = []
            skip = False
            for i, a in enumerate(args):
                if skip:
                    skip = False
                    continue
                if a in ("-t", "-l"):
                    skip = True
                    continue
                if a == "send-keys":
                    continue
                if a not in ("-t", "-l") and not skip:
                    text_parts.append(a)
            self.panes[pane_id].keys_sent.extend(text_parts)
        return ""

    def _list_panes(self, args: tuple[str, ...]) -> str:
        session = None
        for i, a in enumerate(args):
            if a == "-t" and i + 1 < len(args):
                session = args[i + 1]
        if not session:
            return ""

        pane_ids = self.sessions.get(session, [])
        lines = []
        for pid in pane_ids:
            p = self.panes.get(pid)
            if not p:
                continue
            name = p.options.get("@swarm_name", "")
            lines.append(f"{p.pane_id}\t{p.window_index}\t{p.pane_index}\t{name}\t{p.path}")
        return "\n".join(lines)

    def _list_sessions(self, args: tuple[str, ...]) -> str:
        return "\n".join(self.sessions.keys())

    def _has_session(self, args: tuple[str, ...]) -> str:
        # In real tmux, returncode matters. For fake, we just return empty.
        return ""

    def _set_option(self, args: tuple[str, ...]) -> str:
        # set -p -t PANE_ID @key value
        if "-p" in args:
            pane_id = self._get_pane_id(args)
            if pane_id and pane_id in self.panes:
                # Last two args are key, value
                key = args[-2]
                value = args[-1]
                self.panes[pane_id].options[key] = value
        return ""

    def _show_option(self, args: tuple[str, ...]) -> str:
        if "-p" in args:
            pane_id = self._get_pane_id(args)
            if pane_id and pane_id in self.panes:
                key = args[-1]
                return self.panes[pane_id].options.get(key, "")
        return ""

    def _list_windows(self, args: tuple[str, ...]) -> str:
        # Return simple window list
        windows: set[str] = set()
        session = None
        for i, a in enumerate(args):
            if a == "-t" and i + 1 < len(args):
                session = args[i + 1]
        if session:
            for pid in self.sessions.get(session, []):
                p = self.panes.get(pid)
                if p:
                    windows.add(p.window_index)
        return "\n".join(f"{w}\twindow-{w}" for w in sorted(windows))
