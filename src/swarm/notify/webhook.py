"""Webhook notification backend — POST JSON to a configurable URL."""

from __future__ import annotations

import json
import urllib.request
from typing import TYPE_CHECKING

from swarm.logging import get_logger
from swarm.notify.bus import NotifyEvent

if TYPE_CHECKING:
    from swarm.config.models import WebhookConfig

_log = get_logger("notify.webhook")

_TIMEOUT = 5  # seconds


def make_webhook_backend(config: WebhookConfig) -> callable:
    """Create a webhook backend callable from a WebhookConfig.

    Returns a function compatible with NotificationBus.add_backend().
    The backend POSTs a JSON payload to the configured URL.
    If ``config.events`` is non-empty, only matching event types are sent.
    """
    url = config.url
    allowed_events = set(config.events) if config.events else None

    def webhook_backend(event: NotifyEvent) -> None:
        if allowed_events and event.event_type.value not in allowed_events:
            return

        payload = json.dumps(
            {
                "event": event.event_type.value,
                "title": event.title,
                "message": event.message,
                "severity": event.severity.value,
                "worker": event.worker_name,
                "timestamp": event.timestamp,
            }
        ).encode()

        req = urllib.request.Request(
            url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=_TIMEOUT):
                pass
        except Exception:
            _log.warning("webhook POST to %s failed", url, exc_info=True)

    return webhook_backend
