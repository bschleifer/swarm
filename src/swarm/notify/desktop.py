"""Desktop notification backend — WSL toast, Linux notify-send, macOS osascript."""

from __future__ import annotations

import platform
import shutil
import subprocess

from swarm.logging import get_logger
from swarm.notify.bus import NotifyEvent, Severity

_log = get_logger("notify.desktop")


def _is_wsl() -> bool:
    try:
        with open("/proc/version", "r") as f:
            return "microsoft" in f.read().lower()
    except OSError:
        return False


def _send_wsl_toast(title: str, message: str) -> None:
    """Send a Windows toast notification from WSL via powershell.exe."""
    ps = shutil.which("powershell.exe")
    if not ps:
        return
    # Use BurntToast if available, fall back to basic .NET toast
    script = (
        f"[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, "
        f"ContentType = WindowsRuntime] > $null; "
        f"$template = [Windows.UI.Notifications.ToastNotificationManager]::"
        f"GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02); "
        f"$textNodes = $template.GetElementsByTagName('text'); "
        f"$textNodes.Item(0).AppendChild($template.CreateTextNode('{title}')) > $null; "
        f"$textNodes.Item(1).AppendChild($template.CreateTextNode('{message}')) > $null; "
        f"$toast = [Windows.UI.Notifications.ToastNotification]::new($template); "
        f"[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('Swarm').Show($toast)"
    )
    try:
        subprocess.Popen(
            [ps, "-NoProfile", "-NonInteractive", "-Command", script],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        _log.debug("WSL toast failed", exc_info=True)


def _send_notify_send(title: str, message: str, urgency: str = "normal") -> None:
    """Send via notify-send (Linux desktop)."""
    ns = shutil.which("notify-send")
    if not ns:
        return
    try:
        subprocess.Popen(
            [ns, f"--urgency={urgency}", "--app-name=Swarm", title, message],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        _log.debug("notify-send failed", exc_info=True)


def _send_macos_notification(title: str, message: str) -> None:
    """Send a macOS notification via osascript."""
    osascript = shutil.which("osascript")
    if not osascript:
        return
    # Escape double quotes for AppleScript
    safe_title = title.replace('"', '\\"')
    safe_message = message.replace('"', '\\"')
    script = f'display notification "{safe_message}" with title "{safe_title}"'
    try:
        subprocess.Popen(
            [osascript, "-e", script],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        _log.debug("macOS notification failed", exc_info=True)


def desktop_backend(event: NotifyEvent) -> None:
    """Send a desktop notification appropriate for the current platform."""
    # Only send desktop notifications for warning/urgent events
    if event.severity == Severity.INFO:
        return

    title = event.title[:80]
    message = event.message[:200]
    urgency = "critical" if event.severity == Severity.URGENT else "normal"

    if _is_wsl():
        _send_wsl_toast(title, message)
    elif platform.system() == "Darwin":
        _send_macos_notification(title, message)
    elif platform.system() == "Linux":
        _send_notify_send(title, message, urgency)
    else:
        _log.debug("no notification backend available for platform %s", platform.system())
