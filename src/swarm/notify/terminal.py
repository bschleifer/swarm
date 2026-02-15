"""Terminal notification backend — bell + OSC 777 desktop passthrough."""

from __future__ import annotations

import sys

from swarm.logging import get_logger
from swarm.notify.bus import NotifyEvent, Severity

_log = get_logger("notify.terminal")


def terminal_bell_backend(event: NotifyEvent) -> None:
    """Ring the terminal bell for warnings and urgent events."""
    if event.severity in (Severity.WARNING, Severity.URGENT):
        try:
            sys.stderr.write("\a")
            sys.stderr.flush()
        except BrokenPipeError:
            pass


def osc777_backend(event: NotifyEvent) -> None:
    """Send OSC 777 notification for terminal emulators that support it.

    Works with: iTerm2, kitty, foot, and terminals that support
    the de-facto OSC 777;notify;title;body ST sequence.
    """
    title = event.title.replace(";", " ")
    body = event.message.replace(";", " ")[:120]
    seq = f"\033]777;notify;{title};{body}\033\\"
    try:
        sys.stderr.write(seq)
        sys.stderr.flush()
    except Exception:
        _log.debug("OSC 777 write failed", exc_info=True)
