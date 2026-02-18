"""Tests for swarm.update — GitHub-based update detection."""

from __future__ import annotations

import json
import time
from unittest.mock import AsyncMock, patch

import pytest

from swarm.update import (
    UpdateResult,
    _VERSION_RE,
    _fetch_latest_commit,
    _fetch_remote_version,
    _get_installed_version,
    _read_cache,
    _write_cache,
    check_for_update,
    check_for_update_sync,
    perform_update,
    update_result_to_dict,
)


@pytest.fixture()
def cache_dir(tmp_path, monkeypatch):
    """Redirect cache file to a temp directory."""
    cache_file = tmp_path / "update_cache.json"
    monkeypatch.setattr("swarm.update._CACHE_DIR", tmp_path)
    monkeypatch.setattr("swarm.update._CACHE_FILE", cache_file)
    return cache_file


# --- _get_installed_version ---


def test_get_installed_version():
    """Returns the importlib metadata version."""
    with patch("importlib.metadata.version", return_value="1.2.3"):
        assert _get_installed_version() == "1.2.3"


def test_get_installed_version_fallback():
    """Falls back to __version__ on PackageNotFoundError."""
    import importlib.metadata

    with (
        patch("importlib.metadata.version", side_effect=importlib.metadata.PackageNotFoundError),
        patch("swarm.__version__", "0.9.0"),
    ):
        assert _get_installed_version() == "0.9.0"


# --- _VERSION_RE ---


def test_version_regex_double_quotes():
    text = '__version__ = "1.2.3"'
    m = _VERSION_RE.search(text)
    assert m and m.group(1) == "1.2.3"


def test_version_regex_single_quotes():
    text = "__version__ = '2.0.0-beta'"
    m = _VERSION_RE.search(text)
    assert m and m.group(1) == "2.0.0-beta"


def test_version_regex_no_match():
    text = "version = 123"
    assert _VERSION_RE.search(text) is None


# --- _fetch_remote_version ---


@pytest.mark.asyncio()
async def test_fetch_remote_version_success():
    """Mocked curl returning a valid __init__.py."""
    mock_proc = AsyncMock()
    mock_proc.returncode = 0
    mock_proc.communicate.return_value = (b'__version__ = "2.0.0"\n', b"")

    with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
        version, error = await _fetch_remote_version()
    assert version == "2.0.0"
    assert error == ""


@pytest.mark.asyncio()
async def test_fetch_remote_version_network_error():
    """Mocked curl failure → error string."""
    mock_proc = AsyncMock()
    mock_proc.returncode = 1
    mock_proc.communicate.return_value = (b"", b"Connection refused")

    with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
        version, error = await _fetch_remote_version()
    assert version == ""
    assert "Connection refused" in error


@pytest.mark.asyncio()
async def test_fetch_remote_version_parse_error():
    """Curl succeeds but content has no __version__."""
    mock_proc = AsyncMock()
    mock_proc.returncode = 0
    mock_proc.communicate.return_value = (b"# empty file\n", b"")

    with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
        version, error = await _fetch_remote_version()
    assert version == ""
    assert "could not parse" in error


# --- _fetch_latest_commit ---


@pytest.mark.asyncio()
async def test_fetch_latest_commit_success():
    """Mocked GitHub API returning commit info."""
    commit_json = json.dumps(
        [
            {
                "sha": "abcdef1234567890",
                "commit": {
                    "message": "fix: something important\n\ndetails",
                    "committer": {"date": "2025-01-15T10:30:00Z"},
                },
            }
        ]
    ).encode()
    mock_proc = AsyncMock()
    mock_proc.returncode = 0
    mock_proc.communicate.return_value = (commit_json, b"")

    with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
        info = await _fetch_latest_commit()
    assert info["sha"] == "abcdef12"
    assert info["message"] == "fix: something important"
    assert info["date"] == "2025-01-15T10:30:00Z"


@pytest.mark.asyncio()
async def test_fetch_latest_commit_failure():
    """Mocked failure → empty dict."""
    mock_proc = AsyncMock()
    mock_proc.returncode = 1
    mock_proc.communicate.return_value = (b"", b"error")

    with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
        info = await _fetch_latest_commit()
    assert info == {}


# --- Cache ---


def test_cache_round_trip(cache_dir):
    """Write + read preserves data."""
    result = UpdateResult(
        available=True,
        current_version="1.0.0",
        remote_version="2.0.0",
        commit_sha="abc123",
        checked_at=time.time(),
    )
    _write_cache(result)
    loaded = _read_cache()
    assert loaded is not None
    assert loaded.available is True
    assert loaded.current_version == "1.0.0"
    assert loaded.remote_version == "2.0.0"


def test_cache_expired(cache_dir):
    """Old checked_at → returns None."""
    result = UpdateResult(
        available=True,
        current_version="1.0.0",
        remote_version="2.0.0",
        checked_at=time.time() - 100000,  # way past TTL
    )
    _write_cache(result)
    assert _read_cache() is None


def test_cache_missing(cache_dir):
    """No cache file → returns None."""
    assert _read_cache() is None


# --- check_for_update ---


@pytest.mark.asyncio()
async def test_check_uses_cache(cache_dir):
    """Fresh cache → no network call."""
    result = UpdateResult(
        available=True,
        current_version="1.0.0",
        remote_version="2.0.0",
        checked_at=time.time(),
    )
    _write_cache(result)

    with patch("swarm.update._fetch_remote_version") as mock_fetch:
        got = await check_for_update()
    mock_fetch.assert_not_called()
    assert got.available is True


@pytest.mark.asyncio()
async def test_check_force_bypasses_cache(cache_dir):
    """force=True → network call made even with fresh cache."""
    result = UpdateResult(
        available=False,
        current_version="1.0.0",
        remote_version="1.0.0",
        checked_at=time.time(),
    )
    _write_cache(result)

    with (
        patch("swarm.update._fetch_remote_version", return_value=("2.0.0", "")),
        patch("swarm.update._fetch_latest_commit", return_value={}),
        patch("swarm.update._get_installed_version", return_value="1.0.0"),
    ):
        got = await check_for_update(force=True)
    assert got.available is True
    assert got.remote_version == "2.0.0"


@pytest.mark.asyncio()
async def test_check_available(cache_dir):
    """Remote != installed → available=True."""
    with (
        patch("swarm.update._fetch_remote_version", return_value=("2.0.0", "")),
        patch("swarm.update._fetch_latest_commit", return_value={"sha": "abc"}),
        patch("swarm.update._get_installed_version", return_value="1.0.0"),
    ):
        got = await check_for_update(force=True)
    assert got.available is True
    assert got.remote_version == "2.0.0"
    assert got.current_version == "1.0.0"


@pytest.mark.asyncio()
async def test_check_up_to_date(cache_dir):
    """Same versions → available=False."""
    with (
        patch("swarm.update._fetch_remote_version", return_value=("1.0.0", "")),
        patch("swarm.update._fetch_latest_commit", return_value={}),
        patch("swarm.update._get_installed_version", return_value="1.0.0"),
    ):
        got = await check_for_update(force=True)
    assert got.available is False


@pytest.mark.asyncio()
async def test_check_error(cache_dir):
    """Network error → error captured, available=False."""
    with (
        patch("swarm.update._fetch_remote_version", return_value=("", "timeout")),
        patch("swarm.update._get_installed_version", return_value="1.0.0"),
    ):
        got = await check_for_update(force=True)
    assert got.available is False
    assert got.error == "timeout"


# --- check_for_update_sync ---


def test_sync_returns_cache(cache_dir):
    """Cache exists and fresh → returns it."""
    result = UpdateResult(
        available=True,
        current_version="1.0.0",
        remote_version="2.0.0",
        checked_at=time.time(),
    )
    _write_cache(result)
    got = check_for_update_sync()
    assert got is not None
    assert got.available is True


def test_sync_returns_none(cache_dir):
    """No cache → returns None."""
    assert check_for_update_sync() is None


# --- perform_update ---


@pytest.mark.asyncio()
async def test_perform_update_success(cache_dir):
    """All subprocess calls succeed → (True, output)."""
    mock_proc = AsyncMock()
    mock_proc.returncode = 0
    mock_proc.communicate.return_value = (b"ok\n", b"")

    with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
        success, output = await perform_update()
    assert success is True
    assert "ok" in output


@pytest.mark.asyncio()
async def test_perform_update_failure(cache_dir):
    """Subprocess returns non-zero → (False, output)."""
    mock_proc = AsyncMock()
    mock_proc.returncode = 1
    mock_proc.communicate.return_value = (b"error: not found\n", b"")

    with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
        success, output = await perform_update()
    assert success is False
    assert "error" in output


# --- update_result_to_dict ---


def test_update_result_to_dict():
    result = UpdateResult(
        available=True,
        current_version="1.0.0",
        remote_version="2.0.0",
        commit_sha="abc",
        checked_at=1000.0,
    )
    d = update_result_to_dict(result)
    assert d["available"] is True
    assert d["current_version"] == "1.0.0"
    assert d["remote_version"] == "2.0.0"
    assert d["commit_sha"] == "abc"
    assert d["checked_at"] == 1000.0
