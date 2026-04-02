"""SwarmDB — unified SQLite storage for all swarm state.

Single connection, thread-safe via lock, WAL mode for concurrent reads.
All public methods acquire the lock before accessing the connection.
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from swarm.db.schema import CURRENT_VERSION, PRAGMAS, SCHEMA_V1
from swarm.logging import get_logger

_log = get_logger("db")

_DEFAULT_DB_PATH = Path.home() / ".swarm" / "swarm.db"


class SwarmDB:
    """Unified SQLite storage backend.

    Usage::

        db = SwarmDB()          # opens ~/.swarm/swarm.db
        db = SwarmDB(path)      # custom path (tests)

    The database is created with all tables on first open.  On subsequent
    opens the schema version is checked and migrations are applied if needed.
    """

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or _DEFAULT_DB_PATH
        self._lock = threading.Lock()
        self._conn: sqlite3.Connection | None = None
        self._open()

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------

    def _open(self) -> None:
        """Open (or create) the database and apply schema."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._conn = sqlite3.connect(
                str(self.path),
                check_same_thread=False,
            )
            self._conn.row_factory = sqlite3.Row
            # Apply pragmas line-by-line (executescript auto-commits)
            for line in PRAGMAS.strip().splitlines():
                line = line.strip()
                if line and not line.startswith("--"):
                    self._conn.execute(line)
            self._ensure_schema()
            # Secure permissions — secrets live in this DB
            try:
                os.chmod(str(self.path), 0o600)
            except OSError:
                pass
            _log.info("swarm.db opened at %s (v%d)", self.path, CURRENT_VERSION)
        except sqlite3.Error:
            _log.error("failed to open swarm.db at %s", self.path, exc_info=True)
            self._conn = None

    def _ensure_schema(self) -> None:
        """Create tables if needed, track schema version."""
        assert self._conn is not None
        cur = self._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_version'"
        )
        if cur.fetchone() is None:
            # Fresh DB — create everything
            self._conn.executescript(SCHEMA_V1)
            self._conn.execute(
                "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
                (CURRENT_VERSION, time.time()),
            )
            self._conn.commit()
            _log.info("created schema v%d", CURRENT_VERSION)
            return

        # Check version for future migrations
        row = self._conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
        db_version = row[0] if row and row[0] else 0
        if db_version < CURRENT_VERSION:
            self._apply_migrations(db_version)

    def _apply_migrations(self, from_version: int) -> None:
        """Apply incremental migrations."""
        assert self._conn is not None
        _log.info("migrating schema from v%d to v%d", from_version, CURRENT_VERSION)
        if from_version < 2:
            self._migrate_v2_indexes()
        self._conn.execute(
            "INSERT OR REPLACE INTO schema_version (version, applied_at) VALUES (?, ?)",
            (CURRENT_VERSION, time.time()),
        )
        self._conn.commit()

    def _migrate_v2_indexes(self) -> None:
        """v2: add indexes for approval_rules, proposals, and buzz_log."""
        assert self._conn is not None
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_approval_rules_owner"
            " ON approval_rules(owner_type, owner_id)"
        )
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_proposals_task ON proposals(task_id)")
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_proposals_status_time ON proposals(status, created_at)"
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_buzz_worker_time ON buzz_log(worker_name, timestamp)"
        )
        _log.info("v2: added 4 indexes")

    def close(self) -> None:
        """Close the database connection."""
        with self._lock:
            if self._conn:
                self._conn.close()
                self._conn = None

    @property
    def connected(self) -> bool:
        return self._conn is not None

    # ------------------------------------------------------------------
    # Low-level access (all acquire lock)
    # ------------------------------------------------------------------

    def execute(self, sql: str, params: tuple[Any, ...] | dict[str, Any] = ()) -> sqlite3.Cursor:
        """Execute a single SQL statement. Returns cursor."""
        with self._lock:
            if not self._conn:
                raise RuntimeError("SwarmDB is not connected")
            return self._conn.execute(sql, params)

    def executemany(self, sql: str, params_seq: list[tuple[Any, ...]]) -> sqlite3.Cursor:
        """Execute SQL for each parameter set."""
        with self._lock:
            if not self._conn:
                raise RuntimeError("SwarmDB is not connected")
            return self._conn.executemany(sql, params_seq)

    def executescript(self, sql: str) -> None:
        """Execute multiple SQL statements."""
        with self._lock:
            if not self._conn:
                raise RuntimeError("SwarmDB is not connected")
            self._conn.executescript(sql)

    def commit(self) -> None:
        with self._lock:
            if self._conn:
                self._conn.commit()

    def fetchone(
        self, sql: str, params: tuple[Any, ...] | dict[str, Any] = ()
    ) -> sqlite3.Row | None:
        """Execute and return first row."""
        with self._lock:
            if not self._conn:
                raise RuntimeError("SwarmDB is not connected")
            return self._conn.execute(sql, params).fetchone()

    def fetchall(
        self, sql: str, params: tuple[Any, ...] | dict[str, Any] = ()
    ) -> list[sqlite3.Row]:
        """Execute and return all rows."""
        with self._lock:
            if not self._conn:
                raise RuntimeError("SwarmDB is not connected")
            return self._conn.execute(sql, params).fetchall()

    def insert(self, table: str, data: dict[str, Any]) -> int:
        """Insert a row and return lastrowid. Auto-commits."""
        cols = ", ".join(data.keys())
        placeholders = ", ".join("?" for _ in data)
        sql = f"INSERT INTO {table} ({cols}) VALUES ({placeholders})"
        with self._lock:
            if not self._conn:
                raise RuntimeError("SwarmDB is not connected")
            cur = self._conn.execute(sql, tuple(data.values()))
            self._conn.commit()
            return cur.lastrowid or 0

    def update(
        self, table: str, data: dict[str, Any], where: str, where_params: tuple[Any, ...]
    ) -> int:
        """Update rows matching where clause. Returns rows affected."""
        set_clause = ", ".join(f"{k} = ?" for k in data)
        sql = f"UPDATE {table} SET {set_clause} WHERE {where}"
        params = tuple(data.values()) + where_params
        with self._lock:
            if not self._conn:
                raise RuntimeError("SwarmDB is not connected")
            cur = self._conn.execute(sql, params)
            self._conn.commit()
            return cur.rowcount

    def delete(self, table: str, where: str, where_params: tuple[Any, ...]) -> int:
        """Delete rows matching where clause. Returns rows affected."""
        sql = f"DELETE FROM {table} WHERE {where}"
        with self._lock:
            if not self._conn:
                raise RuntimeError("SwarmDB is not connected")
            cur = self._conn.execute(sql, where_params)
            self._conn.commit()
            return cur.rowcount

    # ------------------------------------------------------------------
    # Maintenance
    # ------------------------------------------------------------------

    def checkpoint(self) -> None:
        """Run a WAL checkpoint to consolidate the WAL file."""
        with self._lock:
            if self._conn:
                self._conn.execute("PRAGMA wal_checkpoint(PASSIVE)")

    def backup(self, dest: Path | None = None) -> Path:
        """Create a backup of the database. Returns backup path."""
        dest = dest or self.path.with_suffix(".db.bak")
        with self._lock:
            if self._conn:
                # Use SQLite backup API for consistency
                bak = sqlite3.connect(str(dest))
                try:
                    self._conn.backup(bak)
                finally:
                    bak.close()
        try:
            os.chmod(str(dest), 0o600)
        except OSError:
            pass
        _log.info("database backed up to %s", dest)
        return dest

    def integrity_check(self) -> bool:
        """Run PRAGMA integrity_check. Returns True if OK."""
        with self._lock:
            if not self._conn:
                return False
            result = self._conn.execute("PRAGMA integrity_check").fetchone()
            ok = result is not None and result[0] == "ok"
            if not ok:
                _log.error("integrity check failed: %s", result)
            return ok

    def stats(self) -> dict[str, int]:
        """Return row counts for all tables."""
        tables = [
            "config",
            "workers",
            "groups",
            "approval_rules",
            "tasks",
            "task_history",
            "proposals",
            "buzz_log",
            "messages",
            "pipelines",
            "secrets",
            "queen_sessions",
        ]
        result: dict[str, int] = {}
        with self._lock:
            if not self._conn:
                return result
            for table in tables:
                try:
                    row = self._conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
                    result[table] = row[0] if row else 0
                except sqlite3.OperationalError:
                    result[table] = -1
        return result

    def db_size(self) -> int:
        """Return total size of DB + WAL files in bytes."""
        total = 0
        for suffix in ("", "-wal", "-shm"):
            p = Path(str(self.path) + suffix)
            if p.exists():
                total += p.stat().st_size
        return total
