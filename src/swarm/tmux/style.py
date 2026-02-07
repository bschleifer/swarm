"""Tmux session styling — borders, colors, keybindings, terminal title."""

from __future__ import annotations

import asyncio

from swarm.logging import get_logger
from swarm.tmux.cell import TmuxError, _run_tmux

log = get_logger("tmux.style")

# -- Colors (bee garden palette — matches TUI theme) --
HONEY = "#D8A03D"       # golden honey — primary accent, active borders, idle state
YELLOW = "#E6D2B5"      # creamy beeswax — BUZZING / working
RED = "#D15D4C"         # poppy red — STUNG / exited
COMB = "#8C6A38"        # dimmed gold — inactive borders, muted text
ACTIVE_BG = "#2A1B0E"   # deep hive brown — active pane background
STATUS_BG = "#362415"   # warm brown surface — status bar background
STATUS_FG = "#B0A08A"   # dimmed beeswax — status bar text

# -- Spinner --
SPINNER_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

# -- Border format --
# Uses @swarm_state values: BUZZING, RESTING, STUNG (matching WorkerState enum)
# CRITICAL: use #[fg=X]#[bold] NOT #[fg=X,bold] — tmux splits on ALL commas in #{?...}
_BORDER_FORMAT = (
    "#{?#{pane_active},"
    # Active pane: state-colored label
    "#{?#{==:#{@swarm_state},RESTING},"
    f"#[fg={HONEY}]#[bold] #{{@swarm_name}} [ IDLE - needs input ]#[default],"
    "#{?#{==:#{@swarm_state},STUNG},"
    f"#[fg={RED}]#[bold] #{{@swarm_name}} [ EXITED ]#[default],"
    f"#[fg={YELLOW}] #{{@swarm_name}} [ working... ]#[default]"
    "}},"
    # Inactive pane: warm brown label
    f"#[fg={COMB}] #{{@swarm_name}} "
    "#{?#{==:#{@swarm_state},RESTING},"
    "[ IDLE - needs input ],"
    "#{?#{==:#{@swarm_state},STUNG},"
    "[ EXITED ],"
    "[ working... ]"
    "}}#[default]}"
)


async def apply_session_style(session_name: str) -> None:
    """Apply all visual styling to a tmux session."""
    opts: list[tuple[str, str]] = [
        ("pane-border-lines", "heavy"),
        ("pane-border-status", "top"),
        ("pane-border-indicators", "arrows"),
        ("pane-border-format", _BORDER_FORMAT),
        ("pane-border-style", f"fg={COMB}"),
        ("pane-active-border-style", f"fg={HONEY},bold"),
        ("window-style", f"bg={STATUS_BG}"),
        ("window-active-style", f"bg={ACTIVE_BG}"),
        ("status-style", f"bg={STATUS_BG},fg={STATUS_FG}"),
        ("status-left", f"#[fg={HONEY},bold] #{{session_name}} #[default] "),
        ("status-right",
         f"#[fg={HONEY}]#[bold]BROOD#[default] "
         f"#[fg={COMB}]^b c:cont  C:all  y:yes  N:no  r:restart  s:status  L:log  P:pause "),
        ("status-right-length", "120"),
        ("window-status-format", " #I:#W "),
        ("window-status-current-format",
         f"#[fg={STATUS_BG}]#[bg={HONEY}]#[bold] #I:#W #[default]"),
        ("monitor-silence", "15"),
    ]
    coros = [_run_tmux("set", "-t", session_name, k, v) for k, v in opts]
    await asyncio.gather(*coros)


async def bind_session_keys(session_name: str) -> None:
    """Bind tmux hotkeys scoped to this session."""
    bindings: list[tuple[str, ...]] = [
        # ^b c — continue (send Enter)
        ("c", "send-keys", "Enter"),
        # ^b y — approve (send "y")
        ("y", "send-keys", "-l", "y"),
        # ^b N — deny (send "n") — uppercase to avoid ^b n = next-window
        ("N", "send-keys", "-l", "n"),
        # ^b C — continue all (sync on, Enter, sync off)
        ("C", "set", "synchronize-panes", "on", ";",
         "send-keys", "Enter", ";",
         "set", "synchronize-panes", "off"),
        # ^b r — restart (Ctrl-C, wait, claude --continue)
        ("r", "send-keys", "C-c", ";",
         "run-shell", "sleep 0.5", ";",
         "send-keys", "-l", "claude --continue", ";",
         "send-keys", "Enter"),
    ]
    coros = []
    for key, *cmd_parts in bindings:
        coros.append(_run_tmux("bind-key", "-T", "prefix", key, *cmd_parts))
    await asyncio.gather(*coros)


async def set_terminal_title(session_name: str, title: str) -> None:
    """Set the outer terminal title via tmux's native set-titles mechanism."""
    try:
        await _run_tmux("set", "-t", session_name, "set-titles", "on")
        await _run_tmux("set", "-t", session_name, "set-titles-string", title)
    except TmuxError:
        log.debug("failed to set terminal title for session %s", session_name)


def spinner_frame(tick: int) -> str:
    """Return the current spinner character for a given tick."""
    return SPINNER_FRAMES[tick % len(SPINNER_FRAMES)]
