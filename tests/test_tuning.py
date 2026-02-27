"""Tests for drones/tuning.py — override tracking and auto-tuning suggestions."""

import time

from swarm.drones.store import LogStore
from swarm.drones.tuning import (
    OverrideType,
    TuningSuggestion,
    analyze_overrides,
    record_override,
)


class TestRecordOverride:
    def test_records_rejected_approval(self, tmp_path):
        store = LogStore(db_path=tmp_path / "test.db")
        now = time.time()
        store.insert(
            timestamp=now - 5,
            action="CONTINUED",
            worker_name="api",
            detail="auto-approved",
        )

        row_id = record_override(
            store,
            worker_name="api",
            override_type=OverrideType.REJECTED_APPROVAL,
            detail="interrupted",
        )
        assert row_id is not None

        rows = store.query(overridden=True)
        assert len(rows) == 1
        assert rows[0]["action"] == "CONTINUED"
        assert "rejected_approval" in rows[0]["override_action"]
        store.close()

    def test_records_approved_after_skip(self, tmp_path):
        store = LogStore(db_path=tmp_path / "test.db")
        now = time.time()
        store.insert(
            timestamp=now - 5,
            action="ESCALATED",
            worker_name="web",
            detail="needs approval",
        )

        row_id = record_override(
            store,
            worker_name="web",
            override_type=OverrideType.APPROVED_AFTER_SKIP,
            detail="continued (manual)",
        )
        assert row_id is not None

        rows = store.query(overridden=True)
        assert len(rows) == 1
        assert rows[0]["action"] == "ESCALATED"
        store.close()

    def test_records_redirected_worker(self, tmp_path):
        store = LogStore(db_path=tmp_path / "test.db")
        now = time.time()
        store.insert(
            timestamp=now - 5,
            action="CONTINUED",
            worker_name="api",
        )

        row_id = record_override(
            store,
            worker_name="api",
            override_type=OverrideType.REDIRECTED_WORKER,
            detail="sent message",
        )
        assert row_id is not None
        store.close()

    def test_no_match_returns_none(self, tmp_path):
        store = LogStore(db_path=tmp_path / "test.db")
        row_id = record_override(
            store,
            worker_name="api",
            override_type=OverrideType.REJECTED_APPROVAL,
        )
        assert row_id is None
        store.close()

    def test_filter_only_matches_relevant_actions(self, tmp_path):
        """REJECTED_APPROVAL should only match CONTINUED actions."""
        store = LogStore(db_path=tmp_path / "test.db")
        now = time.time()
        store.insert(
            timestamp=now - 5,
            action="ESCALATED",
            worker_name="api",
        )

        row_id = record_override(
            store,
            worker_name="api",
            override_type=OverrideType.REJECTED_APPROVAL,
        )
        # ESCALATED doesn't match REJECTED_APPROVAL filter
        assert row_id is None
        store.close()


class TestAnalyzeOverrides:
    def _seed_escalation_pattern(self, store: LogStore, override_rate: float) -> None:
        """Seed escalation data with a given override rate."""
        now = time.time()
        total = 10
        overridden_count = int(total * override_rate)
        for i in range(total):
            row_id = store.insert(
                timestamp=now - (i * 3600),
                action="ESCALATED",
                worker_name="api",
                detail=f"escalation {i}",
            )
            if row_id is not None and i < overridden_count:
                store.mark_overridden(row_id, "approved_after_skip")

    def test_no_suggestions_with_low_overrides(self, tmp_path):
        store = LogStore(db_path=tmp_path / "test.db")
        now = time.time()
        # Only 1 override — below threshold
        store.insert(
            timestamp=now - 100,
            action="ESCALATED",
            worker_name="api",
        )
        suggestions = analyze_overrides(store)
        assert len(suggestions) == 0
        store.close()

    def test_escalation_suggestion(self, tmp_path):
        store = LogStore(db_path=tmp_path / "test.db")
        self._seed_escalation_pattern(store, override_rate=0.8)

        suggestions = analyze_overrides(store)
        esc_suggestions = [s for s in suggestions if "escalation" in s.config_path.lower()]
        assert len(esc_suggestions) == 1
        s = esc_suggestions[0]
        assert s.override_rate >= 0.6
        assert s.override_count >= 3
        assert "escalation_threshold" in s.config_path
        store.close()

    def test_no_escalation_suggestion_with_low_rate(self, tmp_path):
        store = LogStore(db_path=tmp_path / "test.db")
        self._seed_escalation_pattern(store, override_rate=0.3)

        suggestions = analyze_overrides(store)
        esc_suggestions = [s for s in suggestions if "escalation" in s.config_path.lower()]
        assert len(esc_suggestions) == 0
        store.close()

    def test_approval_suggestion(self, tmp_path):
        store = LogStore(db_path=tmp_path / "test.db")
        now = time.time()
        # Seed 10 auto-approvals, 5 overridden (50% rate)
        for i in range(10):
            row_id = store.insert(
                timestamp=now - (i * 3600),
                action="CONTINUED",
                worker_name="api",
                detail=f"auto-approved {i}",
            )
            if row_id is not None and i < 5:
                store.mark_overridden(row_id, "rejected_approval")

        suggestions = analyze_overrides(store)
        rule_suggestions = [s for s in suggestions if "approval_rules" in s.config_path]
        assert len(rule_suggestions) == 1
        assert rule_suggestions[0].override_rate >= 0.4
        store.close()

    def test_redirect_suggestion(self, tmp_path):
        store = LogStore(db_path=tmp_path / "test.db")
        now = time.time()
        # Seed 5 redirects for same worker
        for i in range(5):
            row_id = store.insert(
                timestamp=now - (i * 3600),
                action="CONTINUED",
                worker_name="api",
                detail="working",
            )
            if row_id is not None:
                store.mark_overridden(row_id, "redirected_worker: sent message")

        suggestions = analyze_overrides(store)
        redirect_suggestions = [s for s in suggestions if "oversight" in s.config_path]
        assert len(redirect_suggestions) == 1
        assert "api" in redirect_suggestions[0].description
        store.close()

    def test_custom_analysis_window(self, tmp_path):
        store = LogStore(db_path=tmp_path / "test.db")
        now = time.time()
        # Seed old data (8 days ago)
        for i in range(10):
            row_id = store.insert(
                timestamp=now - (8 * 86400) - (i * 100),
                action="ESCALATED",
                worker_name="api",
            )
            if row_id is not None:
                store.mark_overridden(row_id, "approved_after_skip")

        # 7-day window should find nothing
        suggestions = analyze_overrides(store, days=7)
        assert len(suggestions) == 0

        # 10-day window should find them
        suggestions = analyze_overrides(store, days=10)
        assert len(suggestions) > 0
        store.close()


class TestTuningSuggestionDataclass:
    def test_fields(self):
        s = TuningSuggestion(
            id="test_1",
            description="Test suggestion",
            config_path="drones.threshold",
            current_value=60,
            suggested_value=90,
            reason="test reason",
            override_count=5,
            total_decisions=10,
            override_rate=0.5,
        )
        assert s.id == "test_1"
        assert s.override_rate == 0.5
        assert s.status.value == "pending"
