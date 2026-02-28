"""Tests for TerminalEvent types and base provider event methods."""

from __future__ import annotations

from swarm.providers.base import LLMProvider
from swarm.providers.events import EventType, TerminalEvent
from swarm.worker.worker import WorkerState

# --- EventType enum ---


class TestEventType:
    def test_all_values_present(self) -> None:
        expected = {
            "tool_call",
            "thinking",
            "prompt",
            "choice",
            "plan",
            "accept_edits",
            "user_question",
            "error",
            "unknown",
        }
        assert {e.value for e in EventType} == expected

    def test_enum_by_name(self) -> None:
        assert EventType.TOOL_CALL.value == "tool_call"
        assert EventType.THINKING.value == "thinking"
        assert EventType.PROMPT.value == "prompt"
        assert EventType.UNKNOWN.value == "unknown"


# --- TerminalEvent dataclass ---


class TestTerminalEvent:
    def test_minimal_construction(self) -> None:
        evt = TerminalEvent(EventType.UNKNOWN)
        assert evt.event_type == EventType.UNKNOWN
        assert evt.content == ""
        assert evt.tool_name is None
        assert evt.metadata == {}

    def test_with_content(self) -> None:
        evt = TerminalEvent(EventType.THINKING, "esc to interrupt")
        assert evt.content == "esc to interrupt"

    def test_with_tool_name(self) -> None:
        evt = TerminalEvent(EventType.TOOL_CALL, "ls -la", tool_name="Bash")
        assert evt.tool_name == "Bash"

    def test_with_metadata(self) -> None:
        evt = TerminalEvent(EventType.CHOICE, metadata={"summary": "Yes"})
        assert evt.metadata["summary"] == "Yes"

    def test_frozen(self) -> None:
        evt = TerminalEvent(EventType.PROMPT)
        try:
            evt.event_type = EventType.ERROR  # type: ignore[misc]
            assert False, "Should have raised"
        except AttributeError:
            pass

    def test_equality(self) -> None:
        a = TerminalEvent(EventType.PROMPT, "hello")
        b = TerminalEvent(EventType.PROMPT, "hello")
        assert a == b

    def test_inequality(self) -> None:
        a = TerminalEvent(EventType.PROMPT, "hello")
        b = TerminalEvent(EventType.THINKING, "hello")
        assert a != b


# --- Base provider defaults ---


class TestBaseParseEvents:
    """Test default parse_events() and classify_with_events() on a concrete provider."""

    def _get_provider(self) -> LLMProvider:
        from swarm.providers.claude import ClaudeProvider

        return ClaudeProvider()

    def test_parse_events_returns_list(self) -> None:
        provider = self._get_provider()
        events = provider.parse_events("some content")
        assert isinstance(events, list)
        assert len(events) == 1

    def test_parse_events_default_unknown(self) -> None:
        provider = self._get_provider()
        events = provider.parse_events("anything")
        assert events[0].event_type == EventType.UNKNOWN
        assert events[0].content == "anything"

    def test_classify_with_events_returns_tuple(self) -> None:
        provider = self._get_provider()
        state, events = provider.classify_with_events("claude", "esc to interrupt")
        assert isinstance(state, WorkerState)
        assert isinstance(events, list)

    def test_classify_with_events_state_matches_classify_output(self) -> None:
        provider = self._get_provider()
        content = "esc to interrupt"
        state_direct = provider.classify_output("claude", content)
        state_combined, _ = provider.classify_with_events("claude", content)
        assert state_direct == state_combined
