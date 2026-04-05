"""Collect diagnostic attachments for a feedback report.

Pulls version info, recent log lines, drone events, and a redacted copy
of swarm.yaml. Each attachment is returned as an independent section so
the UI can toggle them on/off and let the user edit each one before
submission.
"""

from __future__ import annotations

import collections
import platform
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import yaml

import swarm
from swarm.feedback.redact import redact_config_dict, redact_text

if TYPE_CHECKING:
    from swarm.server.daemon import SwarmDaemon

_DEFAULT_LOG_PATH = Path("~/.swarm/swarm.log").expanduser()
_DEFAULT_LOG_LINES = 200
_DEFAULT_DRONE_EVENTS = 50

# Match `$VAR_NAME` references in swarm.yaml so we can scrub their values.
_ENV_REF_RE = re.compile(r"\$([A-Z_][A-Z0-9_]*)")


@dataclass
class Attachment:
    """A single piece of diagnostic context attached to a feedback report."""

    key: str  # stable identifier (e.g. "environment", "logs", "drone_events")
    label: str  # human-readable section title
    content: str  # redacted body (markdown / plain text)
    redacted_count: int = 0  # items scrubbed during redaction


def _tail_file(path: Path, lines: int) -> str:
    """Return the last *lines* lines of *path*, or empty string on any error."""
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            buf: collections.deque[str] = collections.deque(f, maxlen=lines)
    except (OSError, ValueError):
        return ""
    return "".join(buf)


def _find_env_refs(yaml_text: str) -> list[str]:
    """Extract every ``$VAR_NAME`` reference from raw swarm.yaml text."""
    return sorted(set(_ENV_REF_RE.findall(yaml_text)))


def _collect_environment() -> Attachment:
    body = "\n".join(
        [
            f"- **Swarm**: {swarm.__version__}",
            f"- **Python**: {sys.version.split()[0]}",
            f"- **Platform**: {platform.platform()}",
            f"- **Machine**: {platform.machine()}",
        ]
    )
    return Attachment(key="environment", label="Environment", content=body)


def _collect_install_id() -> Attachment:
    from swarm.feedback.install_id import get_install_id

    return Attachment(
        key="install_id",
        label="Install ID",
        content=get_install_id(),
    )


def _collect_logs(log_path: Path | None, lines: int) -> Attachment:
    path = log_path or _DEFAULT_LOG_PATH
    raw = _tail_file(path, lines)
    if not raw:
        return Attachment(
            key="logs",
            label=f"Recent logs ({path})",
            content="(no log file found or empty)",
        )
    redacted, count = redact_text(raw)
    return Attachment(
        key="logs",
        label=f"Recent logs (last {lines} lines, from {path.name})",
        content=redacted,
        redacted_count=count,
    )


def _collect_drone_events(daemon: SwarmDaemon | None, limit: int) -> Attachment:
    if daemon is None or not hasattr(daemon, "drone_log"):
        return Attachment(
            key="drone_events",
            label="Recent drone events",
            content="(no drone log available)",
        )
    try:
        entries = list(daemon.drone_log.entries)[-limit:]
    except Exception:  # defensive — never break the report flow
        return Attachment(
            key="drone_events",
            label="Recent drone events",
            content="(drone log unavailable)",
        )
    if not entries:
        return Attachment(
            key="drone_events",
            label="Recent drone events",
            content="(no recent events)",
        )
    lines = [entry.display for entry in entries]
    raw = "\n".join(lines)
    redacted, count = redact_text(raw)
    return Attachment(
        key="drone_events",
        label=f"Recent drone events (last {len(entries)})",
        content=redacted,
        redacted_count=count,
    )


def _collect_config(daemon: SwarmDaemon | None) -> Attachment:
    source_path: str | None = None
    if daemon is not None:
        source_path = getattr(daemon.config, "source_path", None)

    if not source_path:
        return Attachment(
            key="config",
            label="swarm.yaml (redacted)",
            content="(no swarm.yaml loaded — using defaults)",
        )

    path = Path(source_path).expanduser()
    try:
        raw_text = path.read_text(encoding="utf-8")
    except OSError:
        return Attachment(
            key="config",
            label="swarm.yaml (redacted)",
            content=f"(could not read {source_path})",
        )

    # Find env var references so we can scrub their resolved values
    env_refs = _find_env_refs(raw_text)

    # Parse → walk → blank sensitive keys → re-serialize
    try:
        parsed = yaml.safe_load(raw_text) or {}
    except yaml.YAMLError:
        # If YAML is malformed, fall back to regex-scrubbing the raw text
        redacted_raw, count = redact_text(raw_text, env_refs=env_refs)
        return Attachment(
            key="config",
            label="swarm.yaml (redacted, raw)",
            content=redacted_raw,
            redacted_count=count,
        )

    scrubbed, key_count = redact_config_dict(parsed)
    try:
        serialized = yaml.safe_dump(scrubbed, sort_keys=False, default_flow_style=False)
    except yaml.YAMLError:
        serialized = str(scrubbed)

    # Second pass: regex scrub to catch any remaining secret-shaped values
    final, regex_count = redact_text(serialized, env_refs=env_refs)
    return Attachment(
        key="config",
        label="swarm.yaml (redacted)",
        content=final,
        redacted_count=key_count + regex_count,
    )


def collect_attachments(
    daemon: SwarmDaemon | None = None,
    *,
    log_path: Path | None = None,
    log_lines: int = _DEFAULT_LOG_LINES,
    drone_event_limit: int = _DEFAULT_DRONE_EVENTS,
) -> list[Attachment]:
    """Collect all default attachments, in the order they appear in the UI.

    The caller (API route) chooses which ones to include based on the
    user's selected category (Bug / Feature / Question).
    """
    return [
        _collect_environment(),
        _collect_install_id(),
        _collect_logs(log_path, log_lines),
        _collect_drone_events(daemon, drone_event_limit),
        _collect_config(daemon),
    ]
