"""Tests for the BaseStore mixin (shared store helpers)."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from swarm.db.base_store import BaseStore
from swarm.db.core import SwarmDB


@pytest.fixture
def db(tmp_path: Path) -> SwarmDB:
    return SwarmDB(tmp_path / "test.db")


class TestParseJsonField:
    """BaseStore._parse_json_field — safe JSON parsing with fallback."""

    def test_valid_json_list(self) -> None:
        assert BaseStore._parse_json_field('["a", "b"]', []) == ["a", "b"]

    def test_valid_json_dict(self) -> None:
        assert BaseStore._parse_json_field('{"k": 1}', {}) == {"k": 1}

    def test_none_returns_default(self) -> None:
        assert BaseStore._parse_json_field(None, []) == []

    def test_empty_string_returns_default(self) -> None:
        assert BaseStore._parse_json_field("", {}) == {}

    def test_invalid_json_returns_default(self) -> None:
        assert BaseStore._parse_json_field("{bad json", []) == []

    def test_wrong_type_list_expected_got_dict(self) -> None:
        # Caller expects a list but the JSON decodes to a dict
        assert BaseStore._parse_json_field('{"a": 1}', []) == []

    def test_wrong_type_dict_expected_got_list(self) -> None:
        # Caller expects a dict but the JSON decodes to a list
        assert BaseStore._parse_json_field("[1, 2]", {}) == {}

    def test_valid_nested_structure(self) -> None:
        result = BaseStore._parse_json_field('[{"id": 1}]', [])
        assert result == [{"id": 1}]

    def test_string_default(self) -> None:
        # Non-list/dict defaults pass through without type guard
        assert BaseStore._parse_json_field('"hello"', "") == "hello"

    def test_numeric_json(self) -> None:
        assert BaseStore._parse_json_field("42", 0) == 42


class _PruneStore(BaseStore):
    """Concrete subclass for testing _prune_older_than."""

    def __init__(self, db: SwarmDB) -> None:
        self._db = db


class TestPruneOlderThan:
    """BaseStore._prune_older_than — time-based row cleanup."""

    def test_deletes_old_rows(self, db: SwarmDB) -> None:
        store = _PruneStore(db)
        now = time.time()
        # Insert old and recent buzz_log entries
        db.insert(
            "buzz_log",
            {
                "timestamp": now - 86400 * 60,  # 60 days ago
                "action": "OLD",
                "worker_name": "w1",
            },
        )
        db.insert(
            "buzz_log",
            {
                "timestamp": now,
                "action": "NEW",
                "worker_name": "w1",
            },
        )
        deleted = store._prune_older_than("buzz_log", "timestamp", 30)
        assert deleted == 1
        remaining = db.fetchall("SELECT action FROM buzz_log")
        assert len(remaining) == 1
        assert remaining[0]["action"] == "NEW"

    def test_no_rows_to_delete(self, db: SwarmDB) -> None:
        store = _PruneStore(db)
        db.insert(
            "buzz_log",
            {
                "timestamp": time.time(),
                "action": "RECENT",
                "worker_name": "w1",
            },
        )
        deleted = store._prune_older_than("buzz_log", "timestamp", 30)
        assert deleted == 0

    def test_extra_where_clause(self, db: SwarmDB) -> None:
        store = _PruneStore(db)
        now = time.time()
        old_ts = now - 86400 * 60  # 60 days ago
        # Insert two old proposals: one pending, one approved
        db.insert(
            "proposals",
            {
                "id": "p1",
                "worker_name": "w1",
                "proposal_type": "assignment",
                "status": "approved",
                "created_at": old_ts,
                "resolved_at": old_ts,
            },
        )
        db.insert(
            "proposals",
            {
                "id": "p2",
                "worker_name": "w1",
                "proposal_type": "assignment",
                "status": "pending",
                "created_at": old_ts,
                "resolved_at": old_ts,
            },
        )
        # Only prune resolved (non-pending) proposals
        deleted = store._prune_older_than(
            "proposals",
            "resolved_at",
            30,
            extra_where="status != 'pending'",
        )
        assert deleted == 1
        remaining = db.fetchall("SELECT id FROM proposals")
        assert len(remaining) == 1
        assert remaining[0]["id"] == "p2"

    def test_zero_days_deletes_all(self, db: SwarmDB) -> None:
        store = _PruneStore(db)
        db.insert(
            "buzz_log",
            {
                "timestamp": time.time() - 1,
                "action": "X",
                "worker_name": "w1",
            },
        )
        deleted = store._prune_older_than("buzz_log", "timestamp", 0)
        assert deleted == 1
