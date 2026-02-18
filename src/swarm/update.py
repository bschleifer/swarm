"""GitHub-based update detection for Swarm.

Compares the installed version against the latest ``__version__`` on GitHub
main.  Results are cached to ``~/.swarm/update_cache.json`` with a 24-hour
TTL so that startup stays fast (the CLI banner reads cache only).
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

_CACHE_DIR = Path.home() / ".swarm"
_CACHE_FILE = _CACHE_DIR / "update_cache.json"
_CACHE_TTL = 86400  # 24 hours

_GITHUB_RAW_URL = "https://raw.githubusercontent.com/bschleifer/swarm/main/src/swarm/__init__.py"
_GITHUB_API_COMMITS_URL = "https://api.github.com/repos/bschleifer/swarm/commits?per_page=1"
_VERSION_RE = re.compile(r'__version__\s*=\s*["\']([^"\']+)["\']')

_INSTALL_SOURCE = "git+https://github.com/bschleifer/swarm.git"


@dataclass
class UpdateResult:
    """Result of an update check."""

    available: bool
    current_version: str
    remote_version: str
    commit_sha: str = ""
    commit_message: str = ""
    commit_date: str = ""
    checked_at: float = field(default_factory=time.time)
    error: str = ""


def _get_installed_version() -> str:
    """Return the installed version of swarm-ai."""
    import importlib.metadata

    try:
        return importlib.metadata.version("swarm-ai")
    except importlib.metadata.PackageNotFoundError:
        from swarm import __version__

        return __version__


async def _fetch_remote_version() -> tuple[str, str]:
    """Fetch ``__version__`` from the raw GitHub ``__init__.py``.

    Returns ``(version_string, error_string)``.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "curl",
            "-sS",
            "--max-time",
            "10",
            _GITHUB_RAW_URL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            return "", f"curl failed: {stderr.decode(errors='replace').strip()}"
        text = stdout.decode(errors="replace")
        m = _VERSION_RE.search(text)
        if not m:
            return "", "could not parse __version__ from remote"
        return m.group(1), ""
    except Exception as exc:
        return "", str(exc)


async def _fetch_latest_commit() -> dict[str, str]:
    """Fetch the latest commit sha/message/date from the GitHub API.

    Returns an empty dict on any failure.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "curl",
            "-sS",
            "--max-time",
            "10",
            "-H",
            "Accept: application/vnd.github+json",
            _GITHUB_API_COMMITS_URL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        if proc.returncode != 0:
            return {}
        data = json.loads(stdout.decode(errors="replace"))
        if not isinstance(data, list) or not data:
            return {}
        commit = data[0]
        return {
            "sha": commit.get("sha", "")[:8],
            "message": commit.get("commit", {}).get("message", "").split("\n")[0],
            "date": commit.get("commit", {}).get("committer", {}).get("date", ""),
        }
    except Exception:
        return {}


def _read_cache() -> UpdateResult | None:
    """Read the cached update result if it exists and is fresh."""
    try:
        data = json.loads(_CACHE_FILE.read_text())
        result = UpdateResult(**data)
        if time.time() - result.checked_at < _CACHE_TTL:
            return result
    except Exception:
        pass
    return None


def _write_cache(result: UpdateResult) -> None:
    """Persist an update result to the cache file."""
    try:
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        _CACHE_FILE.write_text(json.dumps(asdict(result)))
    except Exception:
        pass


async def check_for_update(*, force: bool = False) -> UpdateResult:
    """Check for updates, using the cache unless *force* or expired.

    Never raises — errors are captured in ``UpdateResult.error``.
    """
    if not force:
        cached = _read_cache()
        if cached is not None:
            return cached

    current = _get_installed_version()
    remote, error = await _fetch_remote_version()
    if error:
        return UpdateResult(
            available=False,
            current_version=current,
            remote_version="",
            error=error,
        )

    commit_info = await _fetch_latest_commit()
    result = UpdateResult(
        available=remote != current,
        current_version=current,
        remote_version=remote,
        commit_sha=commit_info.get("sha", ""),
        commit_message=commit_info.get("message", ""),
        commit_date=commit_info.get("date", ""),
    )
    _write_cache(result)
    return result


def check_for_update_sync() -> UpdateResult | None:
    """Synchronous cache-only read for the CLI banner.

    Returns ``None`` if no cache exists or it is expired.
    """
    return _read_cache()


async def perform_update() -> tuple[bool, str]:
    """Run the uv tool reinstall sequence.

    Returns ``(success, combined_output)``.
    """
    commands = [
        ["uv", "tool", "uninstall", "swarm-ai"],
        ["uv", "cache", "clean", "swarm-ai"],
        ["uv", "tool", "install", "--no-cache", _INSTALL_SOURCE],
    ]
    output_parts: list[str] = []
    for cmd in commands:
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            stdout, _ = await proc.communicate()
            text = stdout.decode(errors="replace").strip()
            if text:
                output_parts.append(text)
            if proc.returncode != 0:
                return False, "\n".join(output_parts)
        except Exception as exc:
            output_parts.append(str(exc))
            return False, "\n".join(output_parts)

    # Clear cache so next check reflects the new version
    try:
        _CACHE_FILE.unlink(missing_ok=True)
    except Exception:
        pass

    return True, "\n".join(output_parts)


def update_result_to_dict(result: UpdateResult) -> dict[str, Any]:
    """Serialize an UpdateResult for JSON API/WebSocket responses."""
    return asdict(result)
