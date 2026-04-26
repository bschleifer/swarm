"""Tests for the verifier subprocess module (``swarm.queen.verifier``).

The subprocess wrapper is mostly orchestration of ``claude -p`` — the
hot path is unit-tested by stubbing ``LLMProvider.headless_command`` and
the response parser directly. We also exercise the JSON extraction
helpers across plain, fenced, and balanced-brace formats.
"""

from __future__ import annotations

from swarm.queen.verifier import (
    VERIFIER_PROMPT,
    VerifierVerdict,
    _build_prompt,
    _extract_json,
    _parse_verdict,
)

# ---------------------------------------------------------------------------
# VerifierVerdict
# ---------------------------------------------------------------------------


def test_verdict_is_pass_for_verified_uncertain_error():
    """VERIFIED, UNCERTAIN, and ERROR all default-pass; only FAILED reopens."""
    assert VerifierVerdict("VERIFIED", "ok").is_pass is True
    assert VerifierVerdict("UNCERTAIN", "ambiguous").is_pass is True
    assert VerifierVerdict("ERROR", "subprocess crashed").is_pass is True
    assert VerifierVerdict("FAILED", "wrong").is_pass is False


def test_verdict_is_failed_only_for_failed():
    assert VerifierVerdict("FAILED", "x").is_failed is True
    for v in ("VERIFIED", "UNCERTAIN", "ERROR"):
        assert VerifierVerdict(v, "x").is_failed is False


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------


def test_build_prompt_includes_system_role_and_inputs():
    prompt = _build_prompt(
        title="Add widget",
        description="implement X",
        criteria=["X is added", "tests pass"],
        diff="diff --git a b\n+changed",
        resolution="did the thing",
        peer_warnings="",
    )
    # System role is embedded
    assert "verifier" in prompt.lower()
    assert "VERIFIED" in prompt and "UNCERTAIN" in prompt and "FAILED" in prompt
    # Inputs land in named sections
    assert "Add widget" in prompt
    assert "X is added" in prompt
    assert "tests pass" in prompt
    assert "+changed" in prompt
    assert "did the thing" in prompt
    # No criteria → no Acceptance criteria header
    assert "Peer warnings" not in prompt


def test_build_prompt_includes_peer_warnings_when_present():
    prompt = _build_prompt(
        title="t",
        description="",
        criteria=[],
        diff="d",
        resolution="r",
        peer_warnings="hub: shared file changed",
    )
    assert "Peer warnings" in prompt
    assert "shared file changed" in prompt


def test_verifier_prompt_pins_role_and_tools():
    """Hard contract: the system prompt forbids Bash and frames the role tightly."""
    assert "Bash" in VERIFIER_PROMPT  # prompt explicitly forbids Bash
    assert "Read, Glob, Grep" in VERIFIER_PROMPT
    assert "VERIFIED" in VERIFIER_PROMPT
    assert "FAILED" in VERIFIER_PROMPT


# ---------------------------------------------------------------------------
# Verdict parsing
# ---------------------------------------------------------------------------


def test_parse_plain_json_verdict():
    text = '{"verdict": "VERIFIED", "reason": "looks good"}'
    v = _parse_verdict(text)
    assert v.verdict == "VERIFIED"
    assert v.reason == "looks good"


def test_parse_fenced_json_verdict():
    text = '```json\n{"verdict": "FAILED", "reason": "diff missing X"}\n```\n'
    v = _parse_verdict(text)
    assert v.verdict == "FAILED"
    assert "diff missing X" in v.reason


def test_parse_balanced_brace_with_trailing_text():
    text = 'Sure, here you go: {"verdict":"UNCERTAIN","reason":"ambiguous"} — done.'
    v = _parse_verdict(text)
    assert v.verdict == "UNCERTAIN"


def test_parse_unknown_verdict_returns_error():
    text = '{"verdict": "MAYBE", "reason": "?"}'
    v = _parse_verdict(text)
    assert v.verdict == "ERROR"
    assert "MAYBE" in v.reason


def test_parse_invalid_json_returns_error():
    text = "this is not json at all"
    v = _parse_verdict(text)
    assert v.verdict == "ERROR"
    assert "valid JSON" in v.reason


def test_parse_truncates_overlong_reason():
    """The verdict reason gets capped at 240 chars to prevent log bloat."""
    text = '{"verdict":"FAILED","reason":"' + ("x" * 1000) + '"}'
    v = _parse_verdict(text)
    assert len(v.reason) <= 240


def test_parse_missing_reason_uses_placeholder():
    text = '{"verdict":"VERIFIED","reason":""}'
    v = _parse_verdict(text)
    assert v.verdict == "VERIFIED"
    assert v.reason  # non-empty placeholder


# ---------------------------------------------------------------------------
# JSON extraction edge cases
# ---------------------------------------------------------------------------


def test_extract_json_handles_quoted_braces():
    """Strings containing ``{`` shouldn't trip the brace counter."""
    text = '{"reason": "saw a {"}'
    obj = _extract_json(text)
    assert obj == {"reason": "saw a {"}


def test_extract_json_returns_none_for_garbage():
    assert _extract_json("xxxx no json here") is None
    assert _extract_json("") is None
    assert _extract_json("just { not closed") is None
