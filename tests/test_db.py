"""Tests for the unified SQLite storage module."""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

import pytest

from swarm.db.core import SwarmDB
from swarm.db.migrate import auto_migrate


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "test.db"


@pytest.fixture
def db(db_path: Path) -> SwarmDB:
    return SwarmDB(db_path)


class TestSwarmDB:
    def test_creates_db_file(self, db: SwarmDB, db_path: Path) -> None:
        assert db_path.exists()
        assert db.connected

    def test_schema_version(self, db: SwarmDB) -> None:
        row = db.fetchone("SELECT MAX(version) FROM schema_version")
        assert row is not None
        assert row[0] == 1

    def test_tables_exist(self, db: SwarmDB) -> None:
        tables = db.fetchall("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
        names = {r[0] for r in tables}
        expected = {
            "schema_version",
            "config",
            "workers",
            "groups",
            "group_workers",
            "config_overrides",
            "approval_rules",
            "tasks",
            "task_history",
            "proposals",
            "buzz_log",
            "messages",
            "pipelines",
            "pipeline_stages",
            "secrets",
            "queen_sessions",
        }
        assert expected.issubset(names)

    def test_insert_and_fetch(self, db: SwarmDB) -> None:
        db.insert("config", {"key": "port", "value": "9090", "updated_at": time.time()})
        row = db.fetchone("SELECT value FROM config WHERE key = ?", ("port",))
        assert row is not None
        assert row[0] == "9090"

    def test_update(self, db: SwarmDB) -> None:
        db.insert("config", {"key": "port", "value": "9090", "updated_at": time.time()})
        affected = db.update("config", {"value": "8080"}, "key = ?", ("port",))
        assert affected == 1
        row = db.fetchone("SELECT value FROM config WHERE key = ?", ("port",))
        assert row is not None
        assert row[0] == "8080"

    def test_delete(self, db: SwarmDB) -> None:
        db.insert("config", {"key": "test", "value": "x", "updated_at": time.time()})
        affected = db.delete("config", "key = ?", ("test",))
        assert affected == 1
        row = db.fetchone("SELECT value FROM config WHERE key = ?", ("test",))
        assert row is None

    def test_insert_task(self, db: SwarmDB) -> None:
        db.insert(
            "tasks",
            {
                "id": "abc123",
                "number": 1,
                "title": "Test task",
                "status": "pending",
                "priority": "normal",
                "task_type": "chore",
                "created_at": time.time(),
            },
        )
        row = db.fetchone("SELECT title FROM tasks WHERE id = ?", ("abc123",))
        assert row is not None
        assert row[0] == "Test task"

    def test_foreign_key_task_history(self, db: SwarmDB) -> None:
        db.insert(
            "tasks",
            {"id": "t1", "number": 1, "title": "T1", "created_at": time.time()},
        )
        db.insert(
            "task_history",
            {"task_id": "t1", "action": "CREATED", "actor": "user", "created_at": time.time()},
        )
        rows = db.fetchall("SELECT action FROM task_history WHERE task_id = ?", ("t1",))
        assert len(rows) == 1

    def test_stats(self, db: SwarmDB) -> None:
        stats = db.stats()
        assert "tasks" in stats
        assert "config" in stats
        assert all(v >= 0 for v in stats.values())

    def test_db_size(self, db: SwarmDB, db_path: Path) -> None:
        size = db.db_size()
        assert size > 0

    def test_integrity_check(self, db: SwarmDB) -> None:
        assert db.integrity_check()

    def test_backup(self, db: SwarmDB, tmp_path: Path) -> None:
        db.insert("config", {"key": "test", "value": "v", "updated_at": time.time()})
        bak_path = tmp_path / "backup.db"
        result = db.backup(bak_path)
        assert result == bak_path
        assert bak_path.exists()
        # Verify backup has the data
        bak = sqlite3.connect(str(bak_path))
        row = bak.execute("SELECT value FROM config WHERE key = 'test'").fetchone()
        bak.close()
        assert row is not None
        assert row[0] == "v"

    def test_checkpoint(self, db: SwarmDB) -> None:
        db.checkpoint()  # Should not raise

    def test_close_and_reopen(self, db_path: Path) -> None:
        db1 = SwarmDB(db_path)
        db1.insert("config", {"key": "x", "value": "1", "updated_at": time.time()})
        db1.close()
        assert not db1.connected
        db2 = SwarmDB(db_path)
        row = db2.fetchone("SELECT value FROM config WHERE key = ?", ("x",))
        assert row is not None
        assert row[0] == "1"
        db2.close()

    def test_wal_mode(self, db: SwarmDB) -> None:
        row = db.fetchone("PRAGMA journal_mode")
        assert row is not None
        assert row[0] == "wal"

    def test_permissions(self, db_path: Path) -> None:
        import os
        import stat

        db = SwarmDB(db_path)
        mode = os.stat(db_path).st_mode
        assert not (mode & stat.S_IROTH)
        assert not (mode & stat.S_IWOTH)
        db.close()


class TestMigration:
    def test_migrate_tasks(self, db: SwarmDB, tmp_path: Path) -> None:
        tasks_file = tmp_path / "tasks.json"
        tasks_file.write_text(
            json.dumps(
                [
                    {
                        "id": "task1",
                        "number": 42,
                        "title": "Fix bug",
                        "status": "completed",
                        "priority": "high",
                        "task_type": "bug",
                        "resolution": "Fixed it",
                        "created_at": 1000.0,
                    }
                ]
            )
        )
        from swarm.db.migrate import _migrate_tasks

        result = _migrate_tasks(db, tasks_file)
        assert result == 1
        row = db.fetchone("SELECT title, status FROM tasks WHERE id = ?", ("task1",))
        assert row is not None
        assert row[0] == "Fix bug"
        assert row[1] == "completed"
        assert tasks_file.with_suffix(".json.migrated").exists()

    def test_migrate_messages(self, db: SwarmDB, tmp_path: Path) -> None:
        msg_db_path = tmp_path / "messages.db"
        conn = sqlite3.connect(str(msg_db_path))
        conn.execute(
            """CREATE TABLE messages (
                id INTEGER PRIMARY KEY, sender TEXT, recipient TEXT,
                msg_type TEXT, content TEXT, created_at REAL, read_at REAL
            )"""
        )
        conn.execute(
            "INSERT INTO messages VALUES (1, 'alice', 'bob', 'warning', 'hi', 100.0, NULL)"
        )
        conn.commit()
        conn.close()

        from swarm.db.migrate import _migrate_messages

        result = _migrate_messages(db, msg_db_path)
        assert result == 1
        row = db.fetchone("SELECT sender, content FROM messages WHERE recipient = ?", ("bob",))
        assert row is not None
        assert row[0] == "alice"
        assert row[1] == "hi"

    def test_migrate_secrets(self, db: SwarmDB, tmp_path: Path) -> None:
        tokens = tmp_path / "graph_tokens.json"
        tokens.write_text('{"access_token": "secret123"}')

        from swarm.db.migrate import _migrate_secrets

        result = _migrate_secrets(db, tmp_path)
        assert result == 1
        row = db.fetchone("SELECT value FROM secrets WHERE key = ?", ("graph_tokens",))
        assert row is not None
        data = json.loads(row[0])
        assert data["access_token"] == "secret123"

    def test_migrate_skips_if_data_exists(self, db: SwarmDB, tmp_path: Path) -> None:
        db.insert(
            "tasks",
            {"id": "existing", "number": 1, "title": "Already here", "created_at": time.time()},
        )
        tasks_file = tmp_path / "tasks.json"
        tasks_file.write_text(json.dumps([{"id": "new", "number": 2, "title": "New"}]))

        from swarm.db.migrate import _migrate_tasks

        result = _migrate_tasks(db, tasks_file)
        assert result == 0  # Skipped — data already exists
        assert not tasks_file.with_suffix(".json.migrated").exists()

    def test_auto_migrate_no_files(self, db: SwarmDB, tmp_path: Path) -> None:
        count = auto_migrate(db, tmp_path)
        assert count == 0
