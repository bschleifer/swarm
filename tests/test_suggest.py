"""Tests for drones/suggest.py — pattern suggestion engine."""

import re

from swarm.drones.suggest import RuleSuggestion, suggest_rule


class TestSuggestRuleBasic:
    def test_empty_details(self):
        result = suggest_rule([])
        assert result.pattern == ""
        assert result.confidence == 0.0

    def test_returns_rule_suggestion_type(self):
        result = suggest_rule(["Bash: npm install"])
        assert isinstance(result, RuleSuggestion)

    def test_pattern_compiles(self):
        result = suggest_rule(["Bash: npm install"])
        assert result.pattern
        re.compile(result.pattern, re.IGNORECASE)


class TestSuggestRuleTools:
    def test_bash_tool(self):
        result = suggest_rule(["Bash: npm install express"])
        assert result.pattern
        assert r"\bBash\b" in result.pattern
        assert result.confidence > 0

    def test_read_tool(self):
        result = suggest_rule(["Read(/home/user/project/src/main.py)"])
        assert r"\bRead\b" in result.pattern

    def test_edit_tool(self):
        result = suggest_rule(["Edit: modified src/app.ts"])
        assert r"\bEdit\b" in result.pattern

    def test_grep_tool(self):
        result = suggest_rule(["Grep: searching for patterns"])
        assert r"\bGrep\b" in result.pattern


class TestSuggestRuleCommands:
    def test_npm_install(self):
        result = suggest_rule(["Bash: npm install express"])
        assert result.pattern
        compiled = re.compile(result.pattern, re.IGNORECASE)
        assert compiled.search("Bash: npm install express")

    def test_pytest(self):
        result = suggest_rule(["Bash: pytest tests/ -q"])
        assert result.pattern
        compiled = re.compile(result.pattern, re.IGNORECASE)
        assert compiled.search("Bash: pytest tests/ -q")

    def test_git_status(self):
        result = suggest_rule(["Bash: git status"])
        assert result.pattern
        compiled = re.compile(result.pattern, re.IGNORECASE)
        assert compiled.search("Bash: git status")

    def test_uv_run(self):
        result = suggest_rule(["Bash: uv run ruff check"])
        assert result.pattern
        compiled = re.compile(result.pattern, re.IGNORECASE)
        assert compiled.search("Bash: uv run ruff check")


class TestSuggestRuleMultipleDetails:
    def test_common_tokens(self):
        result = suggest_rule(
            [
                "Bash: pytest tests/test_api.py -q",
                "Bash: pytest tests/test_store.py -v",
            ]
        )
        assert result.pattern
        assert result.confidence > 0.3

    def test_higher_confidence_with_more_details(self):
        single = suggest_rule(["Bash: npm install"])
        multi = suggest_rule(["Bash: npm install", "Bash: npm install lodash"])
        assert multi.confidence >= single.confidence


class TestSuggestRuleSafety:
    def test_never_matches_drop_table(self):
        """Suggestions must never produce patterns matching dangerous operations."""
        result = suggest_rule(["DROP TABLE users"])
        assert result.pattern == ""
        assert result.confidence == 0.0

    def test_never_matches_rm_rf(self):
        result = suggest_rule(["rm -rf /important"])
        assert result.pattern == ""
        assert result.confidence == 0.0

    def test_never_matches_force_push(self):
        result = suggest_rule(["git push origin main"])
        assert result.pattern == ""
        assert result.confidence == 0.0

    def test_never_matches_git_reset_hard(self):
        result = suggest_rule(["git reset --hard HEAD~1"])
        assert result.pattern == ""
        assert result.confidence == 0.0

    def test_never_matches_no_verify(self):
        result = suggest_rule(["commit --no-verify"])
        assert result.pattern == ""
        assert result.confidence == 0.0


class TestSuggestRuleAction:
    def test_default_approve(self):
        result = suggest_rule(["Bash: npm test"])
        assert result.action == "approve"

    def test_escalate_action(self):
        result = suggest_rule(["Bash: npm test"], action="escalate")
        assert result.action == "escalate"


class TestSuggestRuleFallback:
    def test_no_tool_no_command_uses_keywords(self):
        """When no tool or command is recognized, falls back to significant words."""
        result = suggest_rule(["deploying application to staging environment"])
        assert result.pattern
        # Should have extracted some significant words
        compiled = re.compile(result.pattern, re.IGNORECASE)
        assert compiled.search("deploying application to staging environment")

    def test_very_short_text(self):
        """Very short text with no significant words."""
        result = suggest_rule(["ok"])
        assert result.pattern == ""
        assert result.confidence == 0.0


class TestSuggestRuleExplanation:
    def test_has_explanation(self):
        result = suggest_rule(["Bash: npm install"])
        assert result.explanation
        assert "tool" in result.explanation.lower() or "command" in result.explanation.lower()

    def test_empty_explanation_on_failure(self):
        result = suggest_rule([])
        assert result.explanation
