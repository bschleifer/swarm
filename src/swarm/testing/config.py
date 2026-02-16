"""TestConfig — configuration for test mode runs."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class TestConfig:
    """Settings for ``swarm test`` supervised orchestration testing."""

    enabled: bool = False
    port: int = 9091  # dedicated test port (separate from main web UI)
    auto_resolve_delay: float = 4.0  # seconds before Queen resolves proposal
    report_dir: str = "~/.swarm/reports"
    auto_complete_min_idle: float = 10.0  # shorter idle threshold for test mode
