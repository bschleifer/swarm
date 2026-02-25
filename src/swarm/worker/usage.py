"""Read LLM CLI session JSONL files to extract per-worker token usage."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from swarm.logging import get_logger
from swarm.worker.worker import TokenUsage

if TYPE_CHECKING:
    from swarm.providers.base import LLMProvider

_log = get_logger("worker.usage")

# Published Claude pricing per million tokens (as of 2025).
# Used to estimate worker cost from token counts.
_PRICE_PER_M_INPUT = 3.0  # $3/M input tokens
_PRICE_PER_M_OUTPUT = 15.0  # $15/M output tokens
_PRICE_PER_M_CACHE_READ = 0.30  # $0.30/M cache read tokens
_PRICE_PER_M_CACHE_CREATE = 3.75  # $3.75/M cache creation tokens


@dataclass(frozen=True)
class PricingTier:
    """Per-million-token pricing for an LLM provider."""

    input: float  # per million tokens
    output: float
    cache_read: float
    cache_creation: float


_PRICING: dict[str, PricingTier] = {
    "claude": PricingTier(input=3.0, output=15.0, cache_read=0.30, cache_creation=3.75),
    "gemini": PricingTier(input=1.25, output=10.0, cache_read=0.315, cache_creation=4.50),
    "codex": PricingTier(input=2.50, output=10.0, cache_read=0.25, cache_creation=2.50),
}


def estimate_cost(usage: TokenUsage) -> float:
    """Estimate USD cost from token counts using published Claude pricing."""
    return (
        usage.input_tokens * _PRICE_PER_M_INPUT
        + usage.output_tokens * _PRICE_PER_M_OUTPUT
        + usage.cache_read_tokens * _PRICE_PER_M_CACHE_READ
        + usage.cache_creation_tokens * _PRICE_PER_M_CACHE_CREATE
    ) / 1_000_000


def estimate_cost_for_provider(usage: TokenUsage, provider_name: str) -> float:
    """Estimate USD cost using provider-specific pricing (falls back to Claude)."""
    tier = _PRICING.get(provider_name, _PRICING["claude"])
    return (
        usage.input_tokens * tier.input
        + usage.output_tokens * tier.output
        + usage.cache_read_tokens * tier.cache_read
        + usage.cache_creation_tokens * tier.cache_creation
    ) / 1_000_000


def project_dir(worker_path: str) -> Path:
    """Encode a worker's project path to the Claude Code session directory.

    Claude Code stores sessions in ``~/.claude/projects/`` with the absolute
    path encoded by replacing ``/`` with ``-`` (leading slash becomes leading dash).
    """
    # Mirrors Claude Code's own path encoding scheme: replace / with -
    encoded = worker_path.replace("/", "-")
    return Path.home() / ".claude" / "projects" / encoded


def find_active_session(proj_dir: Path, since: float) -> Path | None:
    """Find the most recently modified JSONL session file started after *since*."""
    if not proj_dir.is_dir():
        return None
    candidates: list[tuple[float, Path]] = []
    for p in proj_dir.glob("*.jsonl"):
        try:
            mtime = p.stat().st_mtime
        except OSError:
            continue
        if mtime >= since:
            candidates.append((mtime, p))
    if not candidates:
        return None
    candidates.sort(reverse=True)
    return candidates[0][1]


def read_session_usage(jsonl_path: Path) -> TokenUsage:
    """Read a session JSONL file and sum token usage from assistant messages."""
    total = TokenUsage()
    try:
        with jsonl_path.open() as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if entry.get("type") != "assistant":
                    continue
                msg = entry.get("message", {})
                if not isinstance(msg, dict):
                    continue
                usage = msg.get("usage", {})
                if not isinstance(usage, dict):
                    continue
                total.add(
                    TokenUsage(
                        input_tokens=usage.get("input_tokens", 0),
                        output_tokens=usage.get("output_tokens", 0),
                        cache_read_tokens=usage.get("cache_read_input_tokens", 0),
                        cache_creation_tokens=usage.get("cache_creation_input_tokens", 0),
                    )
                )
    except OSError:
        _log.debug("failed to read session file: %s", jsonl_path)
    total.cost_usd = estimate_cost(total)
    return total


def get_worker_usage(
    worker_path: str,
    since: float,
    provider: LLMProvider | None = None,
) -> TokenUsage:
    """Get accumulated token usage for a worker from its session files.

    Uses ``provider.session_dir()`` when available; falls back to the
    Claude Code default path encoding.
    """
    if provider is not None:
        sess_dir = provider.session_dir(worker_path)
        if sess_dir is None:
            return TokenUsage()
        proj = sess_dir
    else:
        proj = project_dir(worker_path)
    session = find_active_session(proj, since)
    if not session:
        return TokenUsage()
    return read_session_usage(session)
