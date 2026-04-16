"""Metadata-quality tests for Swarm MCP tool definitions.

These tests ensure every tool exposed to workers has:
  - A rich description (>= 150 chars) — workers rely on descriptions to
    know *when* and *how* to call a tool, not just *what* it does.
  - An ``examples`` block in the inputSchema so workers can see a
    concrete payload.

Adding new MCP tools? Update them to meet this bar or update this test
with an intentional rationale.
"""

from __future__ import annotations

from swarm.mcp.tools import TOOLS

MIN_DESCRIPTION_CHARS = 150


def test_every_tool_has_rich_description() -> None:
    thin = [t["name"] for t in TOOLS if len(t.get("description", "")) < MIN_DESCRIPTION_CHARS]
    assert not thin, (
        f"These MCP tools have descriptions under {MIN_DESCRIPTION_CHARS} chars "
        f"(workers need context on when/how to call): {thin}"
    )


def test_every_tool_has_examples() -> None:
    missing = [t["name"] for t in TOOLS if not t.get("inputSchema", {}).get("examples")]
    assert not missing, (
        f"These MCP tools lack an 'examples' field in inputSchema "
        f"(workers benefit from concrete payloads): {missing}"
    )


def test_examples_are_well_formed() -> None:
    """Each example must be a dict matching the tool's property shape."""
    for tool in TOOLS:
        schema = tool.get("inputSchema", {})
        examples = schema.get("examples") or []
        properties = schema.get("properties", {})
        required = set(schema.get("required", []))
        assert isinstance(examples, list), f"{tool['name']}: examples must be a list"
        assert examples, f"{tool['name']}: examples list is empty"
        for i, ex in enumerate(examples):
            assert isinstance(ex, dict), f"{tool['name']} example[{i}] must be dict"
            missing_required = required - ex.keys()
            assert not missing_required, (
                f"{tool['name']} example[{i}] missing required keys: {missing_required}"
            )
            unknown = ex.keys() - properties.keys()
            assert not unknown, f"{tool['name']} example[{i}] has keys not in schema: {unknown}"


def test_every_tool_description_explains_when() -> None:
    """Descriptions should include a 'when to call' hint — heuristic:
    contain one of a handful of trigger words."""
    trigger_words = ("when", "before", "after", "at the start", "use when", "call")
    weak = []
    for tool in TOOLS:
        desc = tool.get("description", "").lower()
        if not any(word in desc for word in trigger_words):
            weak.append(tool["name"])
    assert not weak, (
        f"These MCP tools' descriptions don't hint at *when* to call them "
        f"(include a trigger word like 'when', 'before', 'after', 'call at'): {weak}"
    )
