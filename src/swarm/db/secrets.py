"""Shared secret helpers for auth modules — DB-first with file fallback."""

from __future__ import annotations

import json
import time
from typing import Any

from swarm.logging import get_logger

_log = get_logger("db.secrets")


def load_secret(key: str) -> dict[str, Any] | list[Any] | None:
    """Load a secret from swarm.db secrets table. Returns None if unavailable."""
    try:
        from swarm.db.core import _DEFAULT_DB_PATH, SwarmDB

        if not _DEFAULT_DB_PATH.exists():
            return None
        db = SwarmDB()
        row = db.fetchone("SELECT value FROM secrets WHERE key = ?", (key,))
        db.close()
        if row and row[0]:
            parsed: Any = json.loads(row[0])
            if isinstance(parsed, (dict, list)):
                return parsed
    except Exception:
        _log.debug("load_secret(%s) failed", key, exc_info=True)
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
        _log.debug("save_secret(%s) failed", key, exc_info=True)
        return False
