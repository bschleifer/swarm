"""Keybinding definitions for the Bee Hive TUI."""

from __future__ import annotations

from textual.binding import Binding

# Alt+letter combos — pass through tmux, don't conflict with Input widget
# text entry, and aren't intercepted by the terminal.
BINDINGS = [
    Binding("alt+c", "continue_worker", "Continue", priority=True),
    Binding("alt+a", "continue_all", "Cont.All", priority=True),
    Binding("alt+m", "send_message", "Send", priority=True),
    Binding("alt+e", "send_escape", "Esc→Wkr", priority=True),
    Binding("alt+t", "attach", "Attach", priority=True),
    Binding("alt+r", "revive", "Revive", priority=True),
    Binding("alt+k", "kill_worker", "Kill", priority=True),
    Binding("alt+q", "ask_queen", "Queen", priority=True),
    Binding("alt+n", "create_task", "NewTask", priority=True),
    Binding("alt+d", "assign_task", "Assign", priority=True),
    Binding("alt+b", "toggle_buzz", "Buzz", priority=True),
    Binding("alt+s", "save_screenshot", "Screenshot", priority=True),
    Binding("alt+x", "quit", "Quit", priority=True),
]
