"""Phase 2 of the Apr–May 2026 Anthropic-features bundle.

Wires the existing ``Task.acceptance_criteria`` field — present in the
schema since v1.0 but unread by the verifier — into the verifier's
output. The verifier now optionally returns a ``criteria`` array of
``{"text", "passed"}`` objects, the drone surfaces failed criteria
verbatim in ``verification_reason``, and ``swarm_create_task`` accepts
``acceptance_criteria`` so workers and the Queen can declare success
contracts at task creation time.

Behavioural contract:

* When a task has no criteria, behavior is unchanged from before
  Phase 2 (verifier returns ``verdict`` + ``reason`` only).
* When a task HAS criteria and the verifier returns per-criterion
  results, ``VerifierVerdict.criteria_results`` carries them.
* When any criterion fails, the drone's logged reason cites which
  criterion(s) failed, not just the LLM's prose summary.

The verifier prompt change is additive — older verifier responses
without a ``criteria`` array still parse cleanly with an empty
``criteria_results`` list.
"""

from __future__ import annotations

from swarm.queen.verifier import (
    VERIFIER_PROMPT,
    VerifierVerdict,
    _build_prompt,
    _parse_verdict,
)

# ---------------------------------------------------------------------------
# Prompt asks for per-criterion verdicts when criteria are present
# ---------------------------------------------------------------------------


def test_prompt_requests_per_criterion_verdicts_when_criteria_present() -> None:
    prompt = _build_prompt(
        title="t",
        description="d",
        criteria=["returns 200 for new tasks", "logs creation event"],
        diff="diff",
        resolution="done",
        peer_warnings="",
    )
    # The prompt must explicitly request an array of per-criterion
    # objects when criteria are present, otherwise the LLM has no
    # contract to honour.
    assert "criteria" in prompt.lower()
    assert "passed" in prompt.lower() or '"passed"' in prompt


def test_prompt_omits_criteria_request_when_none_given() -> None:
    """No criteria → no per-task criteria header bloating the prompt."""
    prompt = _build_prompt(
        title="t",
        description="d",
        criteria=[],
        diff="diff",
        resolution="done",
        peer_warnings="",
    )
    # The bolded markdown header that flags the per-task criteria list
    # must be absent when the task has none. (The system prompt itself
    # documents the optional field shape — that's allowed.)
    assert "**Acceptance criteria:**" not in prompt


def test_verifier_prompt_documents_criteria_field() -> None:
    """System prompt explains the optional criteria array shape."""
    # The role-pinning prompt must describe the new optional output
    # field so the LLM has a contract to honour. We only check for
    # the presence of the key markers, not exact wording.
    assert "criteria" in VERIFIER_PROMPT.lower()


# ---------------------------------------------------------------------------
# Parser carries per-criterion results when present
# ---------------------------------------------------------------------------


def test_parse_verdict_with_per_criterion_results() -> None:
    text = (
        '{"verdict":"FAILED","reason":"missed criterion",'
        '"criteria":[{"text":"returns 200","passed":false},'
        '{"text":"logs event","passed":true}]}'
    )
    v = _parse_verdict(text)
    assert v.verdict == "FAILED"
    assert len(v.criteria_results) == 2
    assert v.criteria_results[0]["text"] == "returns 200"
    assert v.criteria_results[0]["passed"] is False
    assert v.criteria_results[1]["passed"] is True


def test_parse_verdict_without_criteria_yields_empty_list() -> None:
    """Backwards compat: old-shape responses still parse, criteria empty."""
    text = '{"verdict": "VERIFIED", "reason": "ok"}'
    v = _parse_verdict(text)
    assert v.verdict == "VERIFIED"
    assert v.criteria_results == []


def test_parse_verdict_tolerates_malformed_criteria() -> None:
    """Malformed criteria entries don't crash the parser — just dropped."""
    text = (
        '{"verdict":"VERIFIED","reason":"ok",'
        '"criteria":[{"text":"a","passed":true},'
        '{"text":42},'  # missing passed
        '"not even an object",'  # not a dict
        '{"passed":false}]}'  # missing text
    )
    v = _parse_verdict(text)
    assert v.verdict == "VERIFIED"
    # Only the well-formed entry survives
    assert len(v.criteria_results) == 1
    assert v.criteria_results[0]["text"] == "a"


# ---------------------------------------------------------------------------
# VerifierVerdict default
# ---------------------------------------------------------------------------


def test_verifier_verdict_default_criteria_empty() -> None:
    v = VerifierVerdict("VERIFIED", "ok")
    assert v.criteria_results == []


def test_verifier_verdict_carries_criteria_results() -> None:
    v = VerifierVerdict(
        verdict="FAILED",
        reason="x",
        criteria_results=[{"text": "c1", "passed": False}],
    )
    assert v.criteria_results == [{"text": "c1", "passed": False}]


# ---------------------------------------------------------------------------
# Drone-side: verification_reason cites failing criteria
# ---------------------------------------------------------------------------


def test_format_verification_reason_with_failed_criteria() -> None:
    """The drone's reason-formatting helper cites failed criteria verbatim."""
    from swarm.drones.verifier import _format_verification_reason

    reason = _format_verification_reason(
        prose="diff missed criterion",
        criteria_results=[
            {"text": "returns 200", "passed": False},
            {"text": "logs event", "passed": True},
            {"text": "updates index", "passed": False},
        ],
    )
    assert "returns 200" in reason
    assert "updates index" in reason
    # Passed criteria don't pollute the reason
    assert "logs event" not in reason
    # The LLM's prose is preserved as the primary explanation
    assert "diff missed criterion" in reason


def test_format_verification_reason_without_criteria_passes_prose_through() -> None:
    """No criteria → reason is just the prose, no "0/0" noise."""
    from swarm.drones.verifier import _format_verification_reason

    reason = _format_verification_reason(prose="looks good", criteria_results=[])
    assert reason == "looks good"


def test_format_verification_reason_all_pass_uses_prose() -> None:
    """All criteria pass → no "failed" suffix, prose stays primary."""
    from swarm.drones.verifier import _format_verification_reason

    reason = _format_verification_reason(
        prose="all green",
        criteria_results=[
            {"text": "a", "passed": True},
            {"text": "b", "passed": True},
        ],
    )
    assert reason == "all green"


# ---------------------------------------------------------------------------
# MCP tool: swarm_create_task accepts and persists acceptance_criteria
# ---------------------------------------------------------------------------


def test_swarm_create_task_schema_advertises_acceptance_criteria() -> None:
    """Schema must advertise acceptance_criteria so workers can find it."""
    from swarm.mcp.tools import TOOLS

    create = next(t for t in TOOLS if t["name"] == "swarm_create_task")
    schema = create["inputSchema"]["properties"]
    assert "acceptance_criteria" in schema
    assert schema["acceptance_criteria"]["type"] == "array"
    assert schema["acceptance_criteria"]["items"]["type"] == "string"


def test_handle_create_task_persists_acceptance_criteria() -> None:
    """A list[str] passed in args lands on the created task via edit_task."""
    from unittest.mock import MagicMock

    from swarm.mcp.tools import _handle_create_task

    daemon = MagicMock()
    created = MagicMock()
    created.id = "t-1"
    daemon.create_task = MagicMock(return_value=created)
    daemon.edit_task = MagicMock(return_value=True)

    _handle_create_task(
        daemon,
        "alpha",
        {
            "title": "Add widget",
            "description": "ship it",
            "acceptance_criteria": ["returns 200", "logs event"],
        },
    )

    daemon.edit_task.assert_called_once()
    kwargs = daemon.edit_task.call_args.kwargs
    assert kwargs["acceptance_criteria"] == ["returns 200", "logs event"]


def test_handle_create_task_skips_edit_when_criteria_empty() -> None:
    """Empty/whitespace-only criteria list → no edit_task call (no DB churn)."""
    from unittest.mock import MagicMock

    from swarm.mcp.tools import _handle_create_task

    daemon = MagicMock()
    created = MagicMock()
    created.id = "t-1"
    daemon.create_task = MagicMock(return_value=created)
    daemon.edit_task = MagicMock(return_value=True)

    _handle_create_task(
        daemon,
        "alpha",
        {"title": "Add widget", "acceptance_criteria": ["", "  "]},
    )

    # edit_task may still be called with target_worker / cross-project args,
    # but never with acceptance_criteria when nothing meaningful was given.
    for call in daemon.edit_task.call_args_list:
        assert "acceptance_criteria" not in call.kwargs
