"""Built-in service handlers for automated pipeline steps."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from swarm.services.registry import ServiceRegistry


def register_defaults(registry: ServiceRegistry) -> None:
    """Register all built-in service handlers."""
    from swarm.services.handlers.file_uploader import FileUploader
    from swarm.services.handlers.shell_command import ShellCommand
    from swarm.services.handlers.webhook_notify import WebhookNotify
    from swarm.services.handlers.youtube_scraper import YouTubeScraper
    from swarm.worker.headless import HeadlessClaude

    registry.register("youtube_scraper", YouTubeScraper())
    registry.register("file_uploader", FileUploader())
    registry.register("headless_claude", HeadlessClaude())
    registry.register("webhook_notify", WebhookNotify())
    registry.register("shell_command", ShellCommand())
