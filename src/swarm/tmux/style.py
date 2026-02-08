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
         f"#[fg={COMB}]alt-enter:focus  alt-c:cont  alt-C:all  alt-y:yes  alt-N:no  alt-r:restart  "
         f"alt-d:detach  alt-z:zoom  alt-[]:win  alt-o:pane "),
        ("status-right-length", "120"),
        ("window-status-format", " #I:#W "),
        ("window-status-current-format",
         f"#[fg={STATUS_BG}]#[bg={HONEY}]#[bold] #I:#W #[default]"),
        ("monitor-silence", "15"),
    ]
    coros = [_run_tmux("set", "-t", session_name, k, v) for k, v in opts]
    await asyncio.gather(*coros)


async def bind_session_keys(session_name: str) -> None:
    """Bind alt-key tmux hotkeys scoped to this session (no prefix needed)."""
    bindings: list[tuple[str, ...]] = [
        # --- Swarm workflow ---
        # Alt+c — continue (send Enter)
        ("M-c", "send-keys", "Enter"),
        # Alt+y — approve (send "y")
        ("M-y", "send-keys", "-l", "y"),
        # Alt+N — deny (send "n") — uppercase to avoid Alt+n conflicts
        ("M-N", "send-keys", "-l", "n"),
        # Alt+C — continue all (sync on, Enter, sync off)
        ("M-C", "set", "synchronize-panes", "on", ";",
         "send-keys", "Enter", ";",
         "set", "synchronize-panes", "off"),
        # Alt+r — restart (Ctrl-C, wait, claude --continue)
        ("M-r", "send-keys", "C-c", ";",
         "run-shell", "sleep 0.5", ";",
         "send-keys", "-l", "claude --continue", ";",
         "send-keys", "Enter"),
        # --- Standard tmux replacements ---
        # Alt+d — detach from session
        ("M-d", "detach-client"),
        # Alt+z — zoom/fullscreen toggle for current pane
        ("M-z", "resize-pane", "-Z"),
        # Alt+] — next window
        ("M-]", "next-window"),
        # Alt+[ — previous window
        ("M-[", "previous-window"),
        # Alt+o — cycle to next pane
        ("M-o", "select-pane", "-t", ":.+"),
        # Alt+Enter — swap current pane into the focus position (index 0)
        ("M-Enter", "swap-pane", "-t", ":.0"),
    ]
    coros = []
    for key, *cmd_parts in bindings:
        coros.append(_run_tmux("bind-key", "-n", key, *cmd_parts))
    await asyncio.gather(*coros)


async def bind_click_to_swap(session_name: str) -> None:
    """Set an after-select-pane hook that swaps clicked pane into focus (index 0).

    Clicking focus itself (pane_index 0) does nothing, preventing an infinite loop.
    Uses ``swap-pane -t :.0`` (no ``-s``) so tmux swaps the *current* pane with
    index 0 — avoids ``#{pane_id}`` expansion issues in hook context.
    Pane IDs follow the swap (tmux swaps pane objects), so Worker.pane_id stays valid.
    """
    await _run_tmux(
        "set-hook", "-t", session_name, "after-select-pane",
        'if-shell -F "#{!=:#{pane_index},0}" "swap-pane -t :.0 ; select-pane -t :.0"',
    )


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
