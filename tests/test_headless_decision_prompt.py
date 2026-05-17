"""Tests for the headless-decision prompt seeding path (task #253)."""

from __future__ import annotations

from swarm.config.models import HiveConfig, QueenConfig
from swarm.queen.queen import HEADLESS_DECISION_PROMPT


class TestHeadlessDecisionPromptConstant:
    def test_is_non_empty(self):
        assert HEADLESS_DECISION_PROMPT, "HEADLESS_DECISION_PROMPT must be non-empty"
        assert len(HEADLESS_DECISION_PROMPT) > 500, "prompt should carry real guidance, not a stub"

    def test_contains_expected_role_markers(self):
        """The prompt must name itself as the headless Queen and cover the
        decision shapes the callers (drone auto-assign, oversight, hive
        coordination, analyzer) expect."""
        p = HEADLESS_DECISION_PROMPT
        assert "headless Queen" in p
        assert "stateless decision" in p
        assert "Hierarchy" in p
        # Decision shapes named in the prompt — these are the callers' contract.
        assert "auto-assignment" in p.lower()
        assert "oversight" in p.lower()
        assert "completion evaluation" in p.lower()
        assert "hive coordination" in p.lower()
        # Anchor back to the interactive Queen's CLAUDE.md.
        assert "~/.swarm/queen/workdir/CLAUDE.md" in p

    def test_no_ask_queen_ui_references(self):
        """The old UI is gone — the prompt should not still reference it."""
        p = HEADLESS_DECISION_PROMPT
        assert "Ask Queen" not in p
        assert "dashboard button" not in p.lower()

    def test_contains_playbook_synthesis_contract(self):
        """Phase 1 of the playbook-synthesis-loop adds decision shape #7;
        the PlaybookSynthesizer parses exactly the keys named here."""
        p = HEADLESS_DECISION_PROMPT
        assert "Playbook synthesis" in p
        for key in ('"synthesize"', '"name"', '"trigger"', '"body"', '"confidence"'):
            assert key in p, f"synthesis contract missing {key}"
        # Must instruct the decline path the synthesizer relies on.
        assert "synthesize" in p and "false" in p

    def test_contains_playbook_consolidation_contract(self):
        """Phase 3 adds decision shape #8; PlaybookConsolidator parses
        exactly the keys named here."""
        p = HEADLESS_DECISION_PROMPT
        assert "Playbook consolidation" in p
        for key in ('"merge"', '"keep"', '"trigger"', '"body"'):
            assert key in p, f"consolidation contract missing {key}"


class TestDaemonSeedBehavior:
    """The daemon's __init__ seeds ``HEADLESS_DECISION_PROMPT`` into
    ``config.queen.system_prompt`` when the field is empty. Any
    non-empty operator override (via swarm.yaml or DB) is preserved.
    This mirrors the ``if not config.queen.system_prompt: ...`` block
    in ``src/swarm/server/daemon.py``.
    """

    def _seed(self, config: HiveConfig) -> None:
        if not config.queen.system_prompt:
            config.queen.system_prompt = HEADLESS_DECISION_PROMPT

    def test_seeds_when_empty(self):
        cfg = HiveConfig(queen=QueenConfig(system_prompt=""))
        assert cfg.queen.system_prompt == ""
        self._seed(cfg)
        assert cfg.queen.system_prompt == HEADLESS_DECISION_PROMPT
        assert "headless Queen" in cfg.queen.system_prompt

    def test_preserves_operator_override(self):
        """Operator-supplied prompts (non-empty) win — the seed is a
        fallback, not a clobber."""
        operator_prompt = "Always route to nexus workers first."
        cfg = HiveConfig(queen=QueenConfig(system_prompt=operator_prompt))
        self._seed(cfg)
        assert cfg.queen.system_prompt == operator_prompt

    def test_default_config_gets_seed(self):
        """A freshly-constructed HiveConfig (like a first-run deployment)
        starts with empty and gets the seed applied."""
        cfg = HiveConfig()
        assert cfg.queen.system_prompt == ""
        self._seed(cfg)
        assert cfg.queen.system_prompt
        assert "stateless decision" in cfg.queen.system_prompt
