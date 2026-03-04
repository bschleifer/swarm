"""LLM CLI provider abstraction — supports multiple AI coding assistants."""

from __future__ import annotations

from enum import Enum
from typing import TYPE_CHECKING

from swarm.providers.base import LLMProvider

if TYPE_CHECKING:
    from swarm.config import CustomLLMConfig, ProviderTuning


class ProviderType(Enum):
    CLAUDE = "claude"
    GEMINI = "gemini"
    CODEX = "codex"
    OPENCODE = "opencode"


_BUILTIN_NAMES: frozenset[str] = frozenset(p.value for p in ProviderType)

# Custom provider registry — populated at daemon startup via register_custom_providers().
_custom_registry: dict[str, CustomLLMConfig] = {}

# Provider tuning overrides for built-in providers — populated via register_provider_overrides().
_overrides_registry: dict[str, ProviderTuning] = {}


def register_custom_providers(llms: list[CustomLLMConfig]) -> None:
    """Replace the custom provider registry with the given list."""
    _custom_registry.clear()
    for llm in llms:
        _custom_registry[llm.name] = llm


def register_provider_overrides(overrides: dict[str, ProviderTuning]) -> None:
    """Replace the built-in provider overrides registry."""
    _overrides_registry.clear()
    _overrides_registry.update(overrides)


def get_valid_providers() -> frozenset[str]:
    """Return all valid provider names (built-in + custom)."""
    if not _custom_registry:
        return _BUILTIN_NAMES
    return _BUILTIN_NAMES | frozenset(_custom_registry)


# Backward-compatible alias — existing imports of ``VALID_PROVIDERS`` still work
# at module level, but call sites that need custom providers should use
# ``get_valid_providers()`` instead.
VALID_PROVIDERS: frozenset[str] = _BUILTIN_NAMES


def list_builtin_providers() -> list[dict[str, object]]:
    """Return built-in provider info dicts for the UI, including detection defaults."""
    _BUILTIN_INFO: list[dict[str, object]] = [
        {
            "name": "claude",
            "display_name": "Claude Code",
            "command": "claude",
            "defaults": {
                "busy_pattern": "esc to interrupt",
                "idle_pattern": r"^\s*[>❯]",
                "choice_pattern": r"^\s*[>❯]\s*\d+\.",
                "user_question_pattern": "chat about this|type something",
                "approval_key": r"\r",
                "rejection_key": r"\x1b",
                "env_strip_prefixes": "CLAUDE",
                "tail_lines": 30,
            },
        },
        {
            "name": "gemini",
            "display_name": "Gemini CLI",
            "command": "gemini",
            "defaults": {
                "busy_pattern": "esc to cancel",
                "idle_pattern": r"^gemini>\s*$",
                "choice_pattern": r"Approve\?\s*\(y/n/always\)",
                "user_question_pattern": "Awaiting Further Direction",
                "approval_key": r"y\r",
                "rejection_key": r"n\r",
                "env_strip_prefixes": "GEMINI, GOOGLE_API",
                "tail_lines": 30,
            },
        },
        {
            "name": "codex",
            "display_name": "Codex",
            "command": "codex",
            "defaults": {
                "busy_pattern": r"[▶▷]",
                "idle_pattern": r"[◇□]",
                "approval_key": r"y\r",
                "rejection_key": r"n\r",
                "env_strip_prefixes": "OPENAI",
                "tail_lines": 30,
            },
        },
        {
            "name": "opencode",
            "display_name": "OpenCode",
            "command": "opencode",
            "defaults": {
                "busy_pattern": (
                    r"Thinking\.\.\.|Working\.\.\.|Generating\.\.\.|Loading\.\.\."
                    r"|Preparing prompt\.\.\.|Building command\.\.\.|Preparing edit\.\.\."
                    r"|Finding files\.\.\.|Searching content\.\.\.|Listing directory\.\.\."
                    r"|Searching code\.\.\.|Reading file\.\.\.|Preparing write\.\.\."
                    r"|Preparing patch\.\.\.|Writing fetch\.\.\."
                    r"|Waiting for (?:tool )?response\.\.\."
                    r"|Building tool call\.\.\.|Initializing LSP\.\.\."
                ),
                "idle_pattern": r"[>❯]\s*$|press enter to send|ctrl\+\? help",
                "choice_pattern": r"Permission Required|Allow \(a\)|Deny \(d\)|Allow for session",
                "user_question_pattern": r"Agent is working, please wait",
                "approval_key": "a",
                "rejection_key": "d",
                "env_strip_prefixes": "OPENCODE, ANTHROPIC_API, OPENAI_API",
                "tail_lines": 30,
            },
        },
    ]
    return _BUILTIN_INFO


def list_providers() -> list[str]:
    """Return provider names in definition order (for UI dropdowns)."""
    names = [p.value for p in ProviderType]
    names.extend(_custom_registry)
    return names


def _maybe_wrap_tuned(provider: LLMProvider, tuning: object | None) -> LLMProvider:
    """Wrap a provider with TunedProvider if tuning has non-default values."""
    if tuning is None:
        return provider
    from swarm.config import ProviderTuning

    if not isinstance(tuning, ProviderTuning) or not tuning.has_tuning():
        return provider
    from swarm.providers.tuned import TunedProvider

    return TunedProvider(provider, tuning)


def get_provider(name: str = "claude") -> LLMProvider:
    """Factory: return an LLMProvider instance by name.

    Raises ``ValueError`` for unknown provider names.
    """
    # Try built-in first
    try:
        provider_type = ProviderType(name.lower())
    except ValueError:
        # Check custom registry
        if name in _custom_registry:
            from swarm.providers.generic import GenericProvider

            cfg = _custom_registry[name]
            provider = GenericProvider(
                name=cfg.name,
                command=cfg.command,
                display=cfg.display_name,
            )
            return _maybe_wrap_tuned(provider, cfg.tuning)
        valid = ", ".join(list_providers())
        raise ValueError(f"Unknown provider {name!r} — valid: {valid}")

    if provider_type == ProviderType.CLAUDE:
        from swarm.providers.claude import ClaudeProvider

        provider = ClaudeProvider()
    elif provider_type == ProviderType.GEMINI:
        from swarm.providers.gemini import GeminiProvider

        provider = GeminiProvider()
    elif provider_type == ProviderType.CODEX:
        from swarm.providers.codex import CodexProvider

        provider = CodexProvider()
    elif provider_type == ProviderType.OPENCODE:
        from swarm.providers.opencode import OpenCodeProvider

        provider = OpenCodeProvider()
    else:
        raise ValueError(f"Provider {name!r} not yet implemented")

    return _maybe_wrap_tuned(provider, _overrides_registry.get(name))


__all__ = [
    "VALID_PROVIDERS",
    "LLMProvider",
    "ProviderType",
    "get_provider",
    "get_valid_providers",
    "list_builtin_providers",
    "list_providers",
    "register_custom_providers",
    "register_provider_overrides",
]
