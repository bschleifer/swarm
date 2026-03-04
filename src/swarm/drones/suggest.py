"""Pattern suggestion engine — generate drone approval rules from log details."""

from __future__ import annotations

import re
from dataclasses import dataclass

from swarm.drones.rules import _ALWAYS_ESCALATE

# Known tool names that appear in Claude Code choice prompts.
_TOOL_NAMES = frozenset(
    {
        "Bash",
        "Read",
        "Write",
        "Edit",
        "Glob",
        "Grep",
        "NotebookEdit",
        "WebFetch",
        "WebSearch",
        "Agent",
    }
)

# Common command prefixes worth capturing in suggestions.
_COMMAND_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\b(npm\s+(?:install|run|test|build))\b"), "npm"),
    (re.compile(r"\b(yarn\s+(?:install|add|run|test|build))\b"), "yarn"),
    (re.compile(r"\b(pip\s+install)\b"), "pip"),
    (re.compile(r"\b(uv\s+(?:run|sync|pip))\b"), "uv"),
    (re.compile(r"\b(pytest\b[\w\s./:-]*)"), "pytest"),
    (re.compile(r"\b(git\s+(?:status|diff|log|add|commit|checkout|branch))\b"), "git"),
    (re.compile(r"\b(cargo\s+(?:build|test|run|check))\b"), "cargo"),
    (re.compile(r"\b(make\b\s*\w*)"), "make"),
    (re.compile(r"\b(docker\s+(?:build|run|compose))\b"), "docker"),
    (re.compile(r"\b(curl\s)"), "curl"),
    (re.compile(r"\b(ruff\s+(?:check|format))\b"), "ruff"),
    (re.compile(r"\b(python\s)"), "python"),
    (re.compile(r"\b(node\s)"), "node"),
]


@dataclass
class RuleSuggestion:
    """A suggested drone approval rule generated from log details."""

    pattern: str  # proposed regex
    action: str  # "approve" or "escalate"
    confidence: float  # 0.0-1.0
    explanation: str  # human-readable reason


def _escape_for_regex(text: str) -> str:
    """Escape regex metacharacters in a literal string."""
    return re.escape(text)


def _extract_tool_name(text: str) -> str | None:
    """Extract the first known tool name from text."""
    for tool in _TOOL_NAMES:
        if re.search(rf"\b{tool}\b", text):
            return tool
    return None


def _extract_command(text: str) -> str | None:
    """Extract the first recognized command signature from text."""
    for pat, _label in _COMMAND_PATTERNS:
        m = pat.search(text)
        if m:
            return m.group(1).strip()
    return None


def _find_common_tokens(details: list[str]) -> list[str]:
    """Find significant tokens common to all detail strings."""
    if not details:
        return []
    # Tokenize each detail into word-like tokens
    token_sets = [set(re.findall(r"\b\w{3,}\b", d)) for d in details]
    common = token_sets[0]
    for ts in token_sets[1:]:
        common &= ts
    # Remove very generic tokens
    stopwords = {"the", "and", "for", "that", "this", "with", "from", "are", "was", "not"}
    common -= stopwords
    # Sort by length descending (longer = more specific)
    return sorted(common, key=len, reverse=True)


def _build_pattern(tokens: list[str], source_text: str = "") -> str:
    """Build a regex pattern from extracted tokens with word boundaries.

    When *source_text* is provided, tokens are ordered by their first
    appearance in the text so the resulting ``.*`` joins match naturally.
    """
    if not tokens:
        return ""
    selected = tokens[:4]  # cap at 4 tokens
    if source_text:
        selected = sorted(selected, key=lambda t: source_text.lower().find(t.lower()))
    parts = [rf"\b{_escape_for_regex(t)}\b" for t in selected]
    return r".*".join(parts)


def _extract_pattern_parts(details: list[str]) -> tuple[list[str], list[str]]:
    """Extract regex parts and explanation fragments from detail strings.

    Returns (pattern_parts, explanation_parts).
    """
    combined = " ".join(details)
    parts: list[str] = []
    explanation_parts: list[str] = []

    # 1. Extract tool name
    tool = _extract_tool_name(combined)
    if tool:
        parts.append(rf"\b{_escape_for_regex(tool)}\b")
        explanation_parts.append(f"tool: {tool}")

    # 2. Extract command signature
    command = _extract_command(combined)
    if command:
        cmd_escaped = _escape_for_regex(command)
        cmd_pattern = re.sub(r"\\ ", r"\\s+", cmd_escaped)
        parts.append(rf"\b{cmd_pattern}\b")
        explanation_parts.append(f"command: {command}")

    # 3. Multiple details → find common tokens as fallback
    if not parts and len(details) > 1:
        common = _find_common_tokens(details)
        if common:
            parts.append(_build_pattern(common[:3], combined))
            explanation_parts.append(f"common tokens: {', '.join(common[:3])}")

    # 4. Last resort — significant words from first detail
    if not parts:
        words = re.findall(r"\b\w{4,}\b", details[0])
        if words:
            words.sort(key=len, reverse=True)
            parts.append(_build_pattern(words[:3], details[0]))
            explanation_parts.append(f"key words: {', '.join(words[:3])}")

    return parts, explanation_parts


_DANGEROUS_SAMPLES = [
    "DROP TABLE users",
    "rm -rf /",
    "git push origin main",
    "git reset --hard",
    "--no-verify",
]


def _validate_pattern(pattern: str) -> str | None:
    """Compile *pattern* and check it doesn't match dangerous operations.

    Returns an error message string on failure, or None on success.
    """
    try:
        compiled = re.compile(pattern, re.IGNORECASE)
    except re.error:
        return f"Generated pattern failed to compile: {pattern}"

    for dangerous in _DANGEROUS_SAMPLES:
        if compiled.search(dangerous):
            return f"Pattern would match dangerous operation: {dangerous}"
    return None


def suggest_rule(details: list[str], action: str = "approve") -> RuleSuggestion:
    """Suggest a drone approval rule pattern from one or more log detail strings.

    Algorithm (conservative — overly-specific, user can relax):
    1. Extract tool names from detail text
    2. Extract command signatures (npm install, git status, pytest, etc.)
    3. Multiple details → find common significant tokens
    4. Build regex with word boundaries, escape metacharacters
    5. Validate: must compile, must NOT match _ALWAYS_ESCALATE
    6. Score confidence based on specificity
    """
    if not details:
        return RuleSuggestion(
            pattern="", action=action, confidence=0.0, explanation="No detail text provided"
        )

    # Safety pre-check: refuse to generate rules from dangerous content
    combined_text = " ".join(details)
    if _ALWAYS_ESCALATE.search(combined_text):
        return RuleSuggestion(
            pattern="",
            action=action,
            confidence=0.0,
            explanation="Input matches always-escalate safety patterns",
        )

    parts, explanation_parts = _extract_pattern_parts(details)

    if not parts:
        return RuleSuggestion(
            pattern="",
            action=action,
            confidence=0.0,
            explanation="Could not extract meaningful pattern tokens",
        )

    pattern = r".*".join(parts) if len(parts) > 1 else parts[0]

    error = _validate_pattern(pattern)
    if error:
        return RuleSuggestion(pattern="", action=action, confidence=0.0, explanation=error)

    confidence = min(0.3 + 0.2 * len(parts) + 0.1 * (len(details) - 1), 1.0)
    explanation = f"Matches {', '.join(explanation_parts)}"

    return RuleSuggestion(
        pattern=pattern,
        action=action,
        confidence=round(confidence, 2),
        explanation=explanation,
    )
