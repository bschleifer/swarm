"""Shared secret helpers for auth modules — DB-first with file fallback."""

from __future__ import annotations

import json
import time
from typing import Any

from swarm.logging import get_logger

_log = get_logger("db.secrets")


def load_secret(key: str) -> dict | list | None:
    """Load a secret from swarm.db secrets table. Returns None if unavailable."""
    try:
        from swarm.db.core import _DEFAULT_DB_PATH, SwarmDB

        if not _DEFAULT_DB_PATH.exists():
            return None
        db = SwarmDB()
        row = db.fetchone("SELECT value FROM secrets WHERE key = ?", (key,))
        db.close()
        if row and row[0]:
            return json.loads(row[0])
    except Exception:
        pass
    return None


def save_secret(key: str, data: Any) -> bool:
    """Save a secret to swarm.db secrets table. Returns True on success."""
    try:
        from swarm.db.core import _DEFAULT_DB_PATH, SwarmDB

        if not _DEFAULT_DB_PATH.exists():
            return False
        db = SwarmDB()
        db.execute(
            "INSERT OR REPLACE INTO secrets (key, value, updated_at) VALUES (?, ?, ?)",
            (key, json.dumps(data), time.time()),
        )
        db.commit()
        db.close()
        return True
    except Exception:
        return False
