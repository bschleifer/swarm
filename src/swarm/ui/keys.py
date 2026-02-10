"""Keybinding definitions for the Bee Hive TUI."""

from __future__ import annotations

from textual.binding import Binding

# Alt+letter combos — pass through tmux, don't conflict with Input widget
# text entry, and aren't intercepted by the terminal.
BINDINGS = [
    # Footer — core workflow actions (always visible)
    Binding("alt+b", "toggle_drones", "Drones", priority=True),
    Binding("alt+c", "continue_worker", "Continue", priority=True),
    Binding("alt+a", "continue_all", "Continue All", priority=True),
    Binding("alt+m", "send_message", "Send Message", priority=True),
    Binding("alt+e", "send_escape", "Escape", priority=True),
    Binding("alt+k", "kill_worker", "Kill", priority=True),
    Binding("alt+r", "revive", "Revive", priority=True),
    Binding("alt+x", "quit", "Quit", priority=True),
    # System palette only — shortcuts work but hidden from footer
    Binding("alt+o", "open_config", "Config", show=False, priority=True),
    Binding("alt+w", "toggle_web", "Web", show=False, priority=True),
    Binding("alt+q", "ask_queen", "Queen", show=False, priority=True),
    Binding("alt+n", "create_task", "Task", show=False, priority=True),
    Binding("alt+d", "assign_task", "Assign", show=False, priority=True),
    Binding("alt+t", "attach", "Tmux", show=False, priority=True),
    Binding("alt+s", "save_screenshot", "Screenshot", show=False, priority=True),
    Binding("alt+f", "complete_task", "Finish Task", show=False, priority=True),
    Binding("alt+l", "fail_task", "Fail Task", show=False, priority=True),
    Binding("alt+p", "remove_task", "Remove Task", show=False, priority=True),
    Binding("alt+i", "send_interrupt", "Interrupt", show=False, priority=True),
    Binding("alt+h", "kill_session", "Kill Session", show=False, priority=True),
    Binding("alt+g", "edit_task", "Edit Task", show=False, priority=True),
]
