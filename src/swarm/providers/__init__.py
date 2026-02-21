"""LLM CLI provider abstraction — supports multiple AI coding assistants."""

from __future__ import annotations

from enum import Enum

from swarm.providers.base import LLMProvider


class ProviderType(Enum):
    CLAUDE = "claude"
    GEMINI = "gemini"
    CODEX = "codex"


def get_provider(name: str = "claude") -> LLMProvider:
    """Factory: return an LLMProvider instance by name.

    Raises ``ValueError`` for unknown provider names.
    """
    try:
        provider_type = ProviderType(name.lower())
    except ValueError:
        valid = ", ".join(p.value for p in ProviderType)
        raise ValueError(f"Unknown provider {name!r} — valid: {valid}")

    if provider_type == ProviderType.CLAUDE:
        from swarm.providers.claude import ClaudeProvider

        return ClaudeProvider()

    if provider_type == ProviderType.GEMINI:
        from swarm.providers.gemini import GeminiProvider

        return GeminiProvider()

    if provider_type == ProviderType.CODEX:
        from swarm.providers.codex import CodexProvider

        return CodexProvider()

    raise ValueError(f"Provider {name!r} not yet implemented")


__all__ = ["LLMProvider", "ProviderType", "get_provider"]
