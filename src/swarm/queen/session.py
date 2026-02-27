"""Queen session persistence — save/restore session IDs."""

from __future__ import annotations

import json
import os
from pathlib import Path

STATE_DIR = Path.home() / ".swarm" / "queen"


def save_session(session_name: str, session_id: str) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    path = STATE_DIR / f"{session_name}.json"
    tmp = path.with_suffix(f".tmp.{os.getpid()}")
    tmp.write_text(json.dumps({"session_id": session_id}))
    os.replace(tmp, path)


def load_session(session_name: str) -> str | None:
    path = STATE_DIR / f"{session_name}.json"
    if path.exists():
        try:
            data = json.loads(path.read_text())
            return data.get("session_id")
        except (json.JSONDecodeError, OSError, KeyError):
            return None
    return None


def clear_session(session_name: str) -> None:
    path = STATE_DIR / f"{session_name}.json"
    if path.exists():
        path.unlink()
