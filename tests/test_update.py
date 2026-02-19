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
    _is_dev_install,
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


# --- _is_dev_install ---


def test_is_dev_install_editable():
    """Editable install detected via direct_url.json."""
    import json as _json

    url_data = _json.dumps(
        {"url": "file:///home/user/projects/swarm", "dir_info": {"editable": True}},
    )
    mock_dist = patch(
        "importlib.metadata.distribution",
        return_value=type("D", (), {"read_text": lambda self, name: url_data})(),
    )
    with mock_dist:
        assert _is_dev_install() is True


def test_is_dev_install_file_url():
    """Local file:// install (non-editable) detected as dev."""
    import json as _json

    url_data = _json.dumps({"url": "file:///home/user/projects/swarm", "dir_info": {}})
    mock_dist = patch(
        "importlib.metadata.distribution",
        return_value=type("D", (), {"read_text": lambda self, name: url_data})(),
    )
    with mock_dist:
        assert _is_dev_install() is True


def test_is_dev_install_not_dev():
    """Regular PyPI install → not dev."""
    mock_dist = patch(
        "importlib.metadata.distribution",
        return_value=type("D", (), {"read_text": lambda self, name: None})(),
    )
    with mock_dist:
        assert _is_dev_install() is False


def test_is_dev_install_not_found():
    """Package not found → not dev."""
    import importlib.metadata

    exc = importlib.metadata.PackageNotFoundError
    with patch("importlib.metadata.distribution", side_effect=exc):
        assert _is_dev_install() is False


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


@pytest.mark.asyncio()
async def test_check_includes_is_dev(cache_dir):
    """check_for_update populates is_dev from _is_dev_install."""
    with (
        patch("swarm.update._fetch_remote_version", return_value=("2.0.0", "")),
        patch("swarm.update._fetch_latest_commit", return_value={}),
        patch("swarm.update._get_installed_version", return_value="1.0.0"),
        patch("swarm.update._is_dev_install", return_value=True),
    ):
        got = await check_for_update(force=True)
    assert got.is_dev is True


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


async def _async_lines_gen(lines: list[bytes]):
    for line in lines:
        yield line


def _async_lines_iter(lines: list[bytes]):
    """Return an async iterator over *lines*, mimicking proc.stdout."""
    return _async_lines_gen(lines)


@pytest.mark.asyncio()
async def test_perform_update_success(cache_dir):
    """Single uv command succeeds → (True, output)."""
    mock_proc = AsyncMock()
    mock_proc.returncode = 0
    mock_proc.stdout = _async_lines_iter([b"Resolved 5 packages\n", b"Installed swarm\n"])
    mock_proc.wait = AsyncMock()

    with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
        success, output = await perform_update()
    assert success is True
    assert "Installed swarm" in output


@pytest.mark.asyncio()
async def test_perform_update_failure(cache_dir):
    """uv command returns non-zero → (False, output)."""
    mock_proc = AsyncMock()
    mock_proc.returncode = 1
    mock_proc.stdout = _async_lines_iter([b"error: not found\n"])
    mock_proc.wait = AsyncMock()

    with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
        success, output = await perform_update()
    assert success is False
    assert "error" in output


@pytest.mark.asyncio()
async def test_perform_update_on_output_callback(cache_dir):
    """on_output callback receives each line of output."""
    mock_proc = AsyncMock()
    mock_proc.returncode = 0
    mock_proc.stdout = _async_lines_iter([b"line one\n", b"line two\n"])
    mock_proc.wait = AsyncMock()

    collected: list[str] = []
    with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
        success, _ = await perform_update(on_output=collected.append)
    assert success is True
    # Should have the emit labels plus the two output lines plus "Update complete!"
    assert any("line one" in c for c in collected)
    assert any("line two" in c for c in collected)
    assert any("Update complete" in c for c in collected)


# --- update_result_to_dict ---


def test_update_result_to_dict():
    result = UpdateResult(
        available=True,
        current_version="1.0.0",
        remote_version="2.0.0",
        commit_sha="abc",
        checked_at=1000.0,
        is_dev=True,
    )
    d = update_result_to_dict(result)
    assert d["available"] is True
    assert d["current_version"] == "1.0.0"
    assert d["remote_version"] == "2.0.0"
    assert d["commit_sha"] == "abc"
    assert d["checked_at"] == 1000.0
    assert d["is_dev"] is True


# --- /action/update-and-restart endpoint ---


@pytest.fixture
def _web_app():
    """Create a minimal aiohttp app with the update-and-restart route."""
    import asyncio

    from unittest.mock import MagicMock

    from aiohttp import web

    from swarm.web.app import handle_action_update_and_restart

    app = web.Application()
    app.router.add_post("/action/update-and-restart", handle_action_update_and_restart)

    daemon = MagicMock()
    daemon.broadcast_ws = MagicMock()
    app["daemon"] = daemon
    app["shutdown_event"] = asyncio.Event()
    app["restart_flag"] = {"requested": False}

    return app


@pytest.mark.asyncio()
async def test_update_and_restart_success(_web_app):
    """Successful update sets restart_requested and shutdown_event."""
    from aiohttp.test_utils import TestClient, TestServer

    app = _web_app
    with patch("swarm.update.perform_update", return_value=(True, "ok")):
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/action/update-and-restart",
                headers={"X-Requested-With": "SwarmWeb"},
            )
            assert resp.status == 200
            data = await resp.json()
            assert data["success"] is True
            assert data["restarting"] is True
            assert app["restart_flag"]["requested"] is True
            assert app["shutdown_event"].is_set()
            app["daemon"].broadcast_ws.assert_called_with({"type": "update_restarting"})


@pytest.mark.asyncio()
async def test_update_and_restart_failure(_web_app):
    """Failed update returns error and does NOT set restart flag."""
    from aiohttp.test_utils import TestClient, TestServer

    app = _web_app
    with patch("swarm.update.perform_update", return_value=(False, "install error")):
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/action/update-and-restart",
                headers={"X-Requested-With": "SwarmWeb"},
            )
            assert resp.status == 200
            data = await resp.json()
            assert data["success"] is False
            assert "install error" in data["output"]
            assert app["restart_flag"]["requested"] is False
            assert not app["shutdown_event"].is_set()
