"""Tmux session styling — borders, colors, keybindings, terminal title."""

from __future__ import annotations

import asyncio

from swarm.logging import get_logger
from swarm.tmux.cell import TmuxError, run_tmux

log = get_logger("tmux.style")

# -- Colors (bee garden palette — matches TUI theme) --
HONEY = "#D8A03D"  # golden honey — primary accent, active borders, idle state
YELLOW = "#E6D2B5"  # creamy beeswax — BUZZING / working
RED = "#D15D4C"  # poppy red — STUNG / exited
COMB = "#8C6A38"  # dimmed gold — inactive borders, muted text
ACTIVE_BG = "#2A1B0E"  # deep hive brown — active pane background
STATUS_BG = "#362415"  # warm brown surface — status bar background
STATUS_FG = "#B0A08A"  # dimmed beeswax — status bar text

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


async def setup_tmux_for_session(session_name: str) -> None:
    """Set required tmux session options for mouse, colors, scrollback.

    Called at session creation time so swarm works on fresh installs without
    requiring a custom ~/.tmux.conf.
    """
    coros = [
        run_tmux("set", "-t", session_name, "mouse", "on"),
        run_tmux("set", "-t", session_name, "history-limit", "50000"),
        run_tmux("set", "-t", session_name, "default-terminal", "tmux-256color"),
        # Global terminal-features (must use -g, applies server-wide)
        run_tmux("set", "-ga", "terminal-features", ",xterm-256color:RGB"),
        # Activity / silence / bell alerts
        run_tmux("set", "-t", session_name, "monitor-activity", "on"),
        run_tmux("set", "-t", session_name, "activity-action", "other"),
        run_tmux("set", "-t", session_name, "visual-activity", "off"),
        run_tmux("set", "-t", session_name, "silence-action", "other"),
        run_tmux("set", "-t", session_name, "visual-silence", "off"),
        run_tmux("set", "-t", session_name, "monitor-bell", "on"),
        run_tmux("set", "-t", session_name, "bell-action", "any"),
        run_tmux("set", "-t", session_name, "visual-bell", "off"),
        # Pane overlay display time
        run_tmux("set", "-t", session_name, "display-panes-time", "3000"),
        run_tmux("set", "-t", session_name, "display-panes-colour", "#504945"),
        run_tmux("set", "-t", session_name, "display-panes-active-colour", "#458588"),
        # Window naming (prevent auto-rename)
        run_tmux("set", "-t", session_name, "automatic-rename", "off"),
        run_tmux("set", "-t", session_name, "allow-rename", "off"),
    ]
    await asyncio.gather(*coros)
    log.info("tmux session options configured for %s", session_name)


async def apply_session_style(session_name: str) -> None:
    """Apply all visual styling to a tmux session.

    Session options (status bar, titles) are set once on the session.
    Window options (borders, pane styles) must be set on *each* window
    explicitly — ``tmux set -t session`` for a window option only affects
    the current window, not all windows.
    """
    # --- Session-level options (status bar, titles) ---
    session_opts: list[tuple[str, str]] = [
        ("status-style", f"bg={STATUS_BG},fg={STATUS_FG}"),
        ("status-left", f"#[fg={HONEY},bold] #{{session_name}} #[default] "),
        (
            "status-right",
            f"#[fg={HONEY}]#[bold]BROOD#[default] "
            f"#[fg={COMB}]alt-enter:focus  alt-c:cont  alt-C:all  "
            f"alt-y:yes  alt-N:no  alt-r:restart  "
            f"alt-d:detach  alt-z:zoom  alt-[]:win  alt-o:pane ",
        ),
        ("status-right-length", "120"),
    ]

    # --- Window-level options (borders, pane backgrounds) ---
    window_opts: list[tuple[str, str]] = [
        ("pane-border-lines", "heavy"),
        ("pane-border-status", "top"),
        ("pane-border-indicators", "arrows"),
        ("pane-border-format", _BORDER_FORMAT),
        ("pane-border-style", f"fg={COMB}"),
        ("pane-active-border-style", f"fg={HONEY},bold"),
        ("window-style", f"bg={STATUS_BG}"),
        ("window-active-style", f"bg={ACTIVE_BG}"),
        ("window-status-format", " #I:#W "),
        ("window-status-current-format", f"#[fg={STATUS_BG}]#[bg={HONEY}]#[bold] #I:#W #[default]"),
        ("monitor-silence", "15"),
    ]

    # Discover all windows in the session
    raw = await run_tmux(
        "list-windows",
        "-t",
        session_name,
        "-F",
        "#{window_index}",
    )
    windows = [line.strip() for line in raw.splitlines() if line.strip()]

    coros: list = []
    # Session options
    for k, v in session_opts:
        coros.append(run_tmux("set", "-t", session_name, k, v))
    # Window options — applied to every window
    for win_idx in windows:
        target = f"{session_name}:{win_idx}"
        for k, v in window_opts:
            coros.append(run_tmux("set", "-w", "-t", target, k, v))

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
        (
            "M-C",
            "set",
            "synchronize-panes",
            "on",
            ";",
            "send-keys",
            "Enter",
            ";",
            "set",
            "synchronize-panes",
            "off",
        ),
        # Alt+r — restart (Ctrl-C, wait, claude --continue)
        (
            "M-r",
            "send-keys",
            "C-c",
            ";",
            "run-shell",
            "sleep 0.5",
            ";",
            "send-keys",
            "-l",
            "claude --continue",
            ";",
            "send-keys",
            "Enter",
        ),
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
        # --- Alt+1..8 — quick-switch: swap pane N into focus (index 0) ---
        ("M-1", "swap-pane", "-s", ":.1", "-t", ":.0", ";", "select-pane", "-t", ":.0"),
        ("M-2", "swap-pane", "-s", ":.2", "-t", ":.0", ";", "select-pane", "-t", ":.0"),
        ("M-3", "swap-pane", "-s", ":.3", "-t", ":.0", ";", "select-pane", "-t", ":.0"),
        ("M-4", "swap-pane", "-s", ":.4", "-t", ":.0", ";", "select-pane", "-t", ":.0"),
        ("M-5", "swap-pane", "-s", ":.5", "-t", ":.0", ";", "select-pane", "-t", ":.0"),
        ("M-6", "swap-pane", "-s", ":.6", "-t", ":.0", ";", "select-pane", "-t", ":.0"),
        ("M-7", "swap-pane", "-s", ":.7", "-t", ":.0", ";", "select-pane", "-t", ":.0"),
        ("M-8", "swap-pane", "-s", ":.8", "-t", ":.0", ";", "select-pane", "-t", ":.0"),
    ]
    coros = []
    for key, *cmd_parts in bindings:
        coros.append(run_tmux("bind-key", "-n", key, *cmd_parts))
    await asyncio.gather(*coros)


async def bind_click_to_swap(session_name: str) -> None:
    """Override MouseDown1Pane so clicking a small pane swaps it into focus (index 0).

    Uses a direct mouse binding instead of an ``after-select-pane`` hook because
    hooks fire unreliably for mouse-triggered pane selection.

    The binding is a single ``if-shell -F`` command (no ``;`` at the top level) to
    avoid tmux treating ``;`` as a top-level command separator during bind-key parsing.

    The condition ``#{&&:#{!=:#{pane_index},0},#{!=:#{@swarm_name},}}`` ensures:
    - Only non-focus panes (index != 0) trigger a swap
    - Only swarm-managed panes (``@swarm_name`` set) are affected
    - Clicking in the focus pane or non-swarm panes passes the mouse event through
    """
    try:
        await run_tmux(
            "bind-key",
            "-n",
            "MouseDown1Pane",
            "if-shell",
            "-F",
            "#{&&:#{!=:#{pane_index},0},#{!=:#{@swarm_name},}}",
            "select-pane -t = ; swap-pane -t :.0 ; select-pane -t :.0",
            "select-pane -t = ; send-keys -M",
        )
    except TmuxError:
        log.warning("failed to bind click-to-swap for session %s", session_name)


async def set_terminal_title(session_name: str, title: str) -> None:
    """Set the outer terminal title via tmux's native set-titles mechanism."""
    try:
        await run_tmux("set", "-t", session_name, "set-titles", "on")
        await run_tmux("set", "-t", session_name, "set-titles-string", title)
    except TmuxError:
        log.debug("failed to set terminal title for session %s", session_name)


def spinner_frame(tick: int) -> str:
    """Return the current spinner character for a given tick."""
    return SPINNER_FRAMES[tick % len(SPINNER_FRAMES)]
