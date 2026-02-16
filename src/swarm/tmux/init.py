"""Write a complete ~/.tmux.conf for swarm — called by ``swarm init``."""

from __future__ import annotations

from pathlib import Path

import click

_TMUX_CONF = Path.home() / ".tmux.conf"

# Marker lines used to identify the swarm-managed block.
_MARKER_START = "# --- swarm begin ---"
_MARKER_END = "# --- swarm end ---"

# The full tmux configuration that swarm requires.
_SWARM_TMUX_BLOCK = f"""\
{_MARKER_START}
# Truecolor (24-bit RGB)
set -ag terminal-features ",xterm-256color:RGB"

# Mouse support (click to select pane, scroll)
set -g mouse on

# Clipboard & passthrough (needed for Ctrl-V paste, images, attachments)
# tmux 3.3+ defaults allow-passthrough to off, which blocks the escape
# sequences (OSC 52) that Claude Code uses for clipboard operations.
set -g allow-passthrough on
set -g set-clipboard on

# Scrollback (needed for state detection)
set -g history-limit 50000

# Activity alerts
set -g monitor-activity on
set -g activity-action other
set -g visual-activity off

# Silence alerts (agent went idle)
set -g silence-action other
set -g visual-silence off

# Bell alerts (pass through to host terminal)
set -g monitor-bell on
set -g bell-action any
set -g visual-bell off

# Pane titles in borders (requires tmux 3.2+)
set -g pane-border-status top
set -g pane-border-format " #{{pane_index}}: #{{b:pane_current_path}} "
set -g pane-border-style "fg=#504945"
set -g pane-active-border-style "fg=#458588,bold"

# Terminal title bar
set -g set-titles on
set -g set-titles-string "swarm -- #W"

# Pane numbering overlay
set -g display-panes-time 3000
set -g display-panes-colour "#504945"
set -g display-panes-active-colour "#458588"

# Window naming (prevent auto-rename)
set -g automatic-rename off
set -g allow-rename off

# Status bar (gruvbox-ish base theme)
set -g status-style "bg=#3c3836,fg=#ebdbb2"
set -g status-left \\
  "#[bg=#458588,fg=#1d2021,bold] #S #[default] "
set -g status-left-length 50
set -g status-right \\
  "#[fg=#a89984]^b c:cont  C:all  r:restart  z:zoom  ^h:copy  d:detach"
set -g status-right-length 120

# Window tabs
set -g window-status-format "  #I:#W  "
set -g window-status-current-format \\
  "#[bg=#458588,fg=#1d2021,bold]  #I:#W  #[default]"
set -g window-status-activity-style "fg=#d79921,bold"
set -g window-status-bell-style "fg=#fb4934,bold"
set -g window-status-separator ""

# Pane switching by number (^b 0..9)
bind 0 select-pane -t 0
bind 1 select-pane -t 1
bind 2 select-pane -t 2
bind 3 select-pane -t 3
bind 4 select-pane -t 4
bind 5 select-pane -t 5
bind 6 select-pane -t 6
bind 7 select-pane -t 7
bind 8 select-pane -t 8
bind 9 select-pane -t 9

# Synchronized input toggle (^b S)
bind S set-window-option synchronize-panes \\; \\
    display-message "sync #{{?synchronize-panes,ON,OFF}}"

# Disable mouse-drag auto-entering copy-mode — too easy to trigger
# accidentally while typing.  Ctrl-H enters copy-mode, then
# mouse-drag to select, Ctrl-C to copy and exit.
bind -n C-h copy-mode
bind -T root MouseDrag1Pane send-keys -M

# In copy-mode: stop-selection freezes highlight, Ctrl-C copies and exits.
bind -T copy-mode    MouseDragEnd1Pane send-keys -X stop-selection
bind -T copy-mode-vi MouseDragEnd1Pane send-keys -X stop-selection
bind -T copy-mode    C-c send-keys -X copy-selection-and-cancel
bind -T copy-mode-vi C-c send-keys -X copy-selection-and-cancel
{_MARKER_END}
"""


def write_tmux_config() -> bool:
    """Write (or update) the swarm block in ~/.tmux.conf.

    If ~/.tmux.conf already exists:
    - If it contains a swarm block, replace just that block.
    - If it has user content but no swarm block, warn and ask before appending.

    Returns True if the config was written.
    """
    if _TMUX_CONF.exists():
        existing = _TMUX_CONF.read_text()

        # Already has a swarm block — replace it
        if _MARKER_START in existing:
            before = existing[: existing.index(_MARKER_START)]
            after_marker = existing.find(_MARKER_END)
            if after_marker != -1:
                after = existing[after_marker + len(_MARKER_END) :]
                # Strip leading newline from after
                if after.startswith("\n"):
                    after = after[1:]
            else:
                after = ""
            _TMUX_CONF.write_text(before + _SWARM_TMUX_BLOCK + after)
            click.echo("  Updated swarm block in ~/.tmux.conf")
            return True

        # Has content but no swarm block — ask before modifying
        if existing.strip():
            click.echo(f"  Found existing ~/.tmux.conf ({len(existing)} bytes)")
            if not click.confirm("  Append swarm configuration block?", default=True):
                click.echo("  Skipped — no changes to ~/.tmux.conf")
                return False
            _TMUX_CONF.write_text(existing.rstrip("\n") + "\n\n" + _SWARM_TMUX_BLOCK)
            click.echo("  Appended swarm block to ~/.tmux.conf")
            return True

    # No file or empty file — write fresh
    _TMUX_CONF.write_text(_SWARM_TMUX_BLOCK)
    click.echo("  Wrote ~/.tmux.conf with swarm configuration")
    return True
