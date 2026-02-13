"""Tests for queen/session.py — session persistence."""

from __future__ import annotations

import json

from swarm.queen.session import clear_session, load_session, save_session


def test_save_and_load(tmp_path, monkeypatch):
    """Round-trip save then load returns the session ID."""
    monkeypatch.setattr("swarm.queen.session.STATE_DIR", tmp_path)
    save_session("my-session", "sess-abc-123")
    assert load_session("my-session") == "sess-abc-123"


def test_load_missing_returns_none(tmp_path, monkeypatch):
    """Loading a session that was never saved returns None."""
    monkeypatch.setattr("swarm.queen.session.STATE_DIR", tmp_path)
    assert load_session("nonexistent") is None


def test_load_corrupted_json_returns_none(tmp_path, monkeypatch):
    """Corrupted JSON on disk returns None rather than crashing."""
    monkeypatch.setattr("swarm.queen.session.STATE_DIR", tmp_path)
    (tmp_path / "bad.json").write_text("{not valid json")
    assert load_session("bad") is None


def test_load_missing_key_returns_none(tmp_path, monkeypatch):
    """JSON without 'session_id' key returns None."""
    monkeypatch.setattr("swarm.queen.session.STATE_DIR", tmp_path)
    (tmp_path / "nokey.json").write_text(json.dumps({"other": "val"}))
    assert load_session("nokey") is None


def test_clear_existing(tmp_path, monkeypatch):
    """clear_session removes an existing session file."""
    monkeypatch.setattr("swarm.queen.session.STATE_DIR", tmp_path)
    save_session("to-clear", "sess-xyz")
    clear_session("to-clear")
    assert not (tmp_path / "to-clear.json").exists()
    assert load_session("to-clear") is None


def test_clear_nonexistent(tmp_path, monkeypatch):
    """clear_session on a missing file doesn't raise."""
    monkeypatch.setattr("swarm.queen.session.STATE_DIR", tmp_path)
    clear_session("doesnt-exist")  # Should not raise


def test_save_creates_directory(tmp_path, monkeypatch):
    """save_session creates the state directory if it doesn't exist."""
    deep = tmp_path / "a" / "b" / "c"
    monkeypatch.setattr("swarm.queen.session.STATE_DIR", deep)
    save_session("nested", "sess-nested")
    assert deep.exists()
    assert load_session("nested") == "sess-nested"


def test_save_overwrites(tmp_path, monkeypatch):
    """Saving the same session name twice overwrites the first value."""
    monkeypatch.setattr("swarm.queen.session.STATE_DIR", tmp_path)
    save_session("overwrite", "old-id")
    save_session("overwrite", "new-id")
    assert load_session("overwrite") == "new-id"
