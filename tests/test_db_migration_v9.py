"""Tests for the v9 SQLite migration that renames task status enum values.

v9 maps the four legacy spellings to the new operator-facing vocabulary:
``proposed`` → ``backlog``, ``pending`` → ``unassigned``, ``in_progress`` →
``active``, ``completed`` → ``done``. ``assigned`` and ``failed`` are
unchanged. The migration runs once at daemon startup and is idempotent —
re-running on a v9 DB is a no-op because the source values no longer exist.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest

from swarm.db.core import SwarmDB
from swarm.db.schema import CURRENT_VERSION


@pytest.fixture
def legacy_db(tmp_path: Path) -> Path:
    """Build a v8 DB by hand with one row per legacy status."""
    path = tmp_path / "legacy.db"
    conn = sqlite3.connect(str(path))
    # Minimum schema needed for the migration to run — tasks + schema_version.
    conn.executescript("""
        CREATE TABLE schema_version (version INTEGER, applied_at REAL);
        CREATE TABLE tasks (
            id TEXT PRIMARY KEY,
            number INTEGER,
            title TEXT,
            description TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'pending',
            priority TEXT NOT NULL DEFAULT 'normal',
            task_type TEXT NOT NULL DEFAULT 'chore',
            assigned_worker TEXT,
            created_at REAL,
            updated_at REAL,
            completed_at REAL,
            resolution TEXT,
            tags TEXT,
            attachments TEXT,
            depends_on TEXT,
            source_email_id TEXT,
            jira_key TEXT,
            is_cross_project INTEGER NOT NULL DEFAULT 0,
            source_worker TEXT,
            target_worker TEXT,
            dependency_type TEXT,
            acceptance_criteria TEXT,
            context_refs TEXT,
            cost_budget REAL DEFAULT 0,
            cost_spent REAL DEFAULT 0,
            learnings TEXT,
            verification_status TEXT NOT NULL DEFAULT 'not_run',
            verification_reason TEXT NOT NULL DEFAULT '',
            verification_reopen_count INTEGER NOT NULL DEFAULT 0
        );
    """)
    conn.execute("INSERT INTO schema_version VALUES (?, ?)", (8, time.time()))
    legacy = [
        ("t-prop", 1, "Proposed task", "proposed"),
        ("t-pend", 2, "Pending task", "pending"),
        ("t-asgn", 3, "Assigned task", "assigned"),
        ("t-prog", 4, "In progress", "in_progress"),
        ("t-comp", 5, "Completed", "completed"),
        ("t-fail", 6, "Failed", "failed"),
    ]
    for tid, num, title, status in legacy:
        conn.execute(
            "INSERT INTO tasks (id, number, title, status) VALUES (?, ?, ?, ?)",
            (tid, num, title, status),
        )
    conn.commit()
    conn.close()
    return path


def test_v9_renames_legacy_statuses(legacy_db: Path) -> None:
    db = SwarmDB(legacy_db)
    rows = {r["id"]: r["status"] for r in db.fetchall("SELECT id, status FROM tasks")}
    assert rows == {
        "t-prop": "backlog",
        "t-pend": "unassigned",
        "t-asgn": "assigned",
        "t-prog": "active",
        "t-comp": "done",
        "t-fail": "failed",
    }
    db.close()


def test_v9_bumps_schema_version(legacy_db: Path) -> None:
    db = SwarmDB(legacy_db)
    row = db.fetchone("SELECT MAX(version) FROM schema_version")
    assert row is not None
    assert row[0] >= 9
    assert CURRENT_VERSION >= 9
    db.close()


def test_v9_is_idempotent(legacy_db: Path) -> None:
    """Running the migration on a DB that's already at v9 must be a no-op."""
    db = SwarmDB(legacy_db)
    # First run already applied during _open. Snapshot, run a second time
    # by calling the migration helper directly, and confirm nothing changes.
    before = {r["id"]: r["status"] for r in db.fetchall("SELECT id, status FROM tasks")}
    db._migrate_v9_status_rename()  # type: ignore[attr-defined]
    after = {r["id"]: r["status"] for r in db.fetchall("SELECT id, status FROM tasks")}
    assert before == after
    db.close()
