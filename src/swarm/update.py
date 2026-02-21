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
from collections.abc import Callable
from typing import Any

_CACHE_DIR = Path.home() / ".swarm"
_CACHE_FILE = _CACHE_DIR / "update_cache.json"
_CACHE_TTL = 86400  # 24 hours

_GITHUB_RAW_URL = "https://raw.githubusercontent.com/bschleifer/swarm/main/src/swarm/__init__.py"
_GITHUB_API_COMMITS_URL = "https://api.github.com/repos/bschleifer/swarm/commits?per_page=1"
_VERSION_RE = re.compile(r'__version__\s*=\s*["\']([^"\']+)["\']')

_INSTALL_SOURCE = "git+https://github.com/bschleifer/swarm.git"


def _version_tuple(v: str) -> tuple[int, ...]:
    """Parse a dotted version string into a tuple of ints for comparison."""
    parts: list[int] = []
    for segment in v.split("."):
        try:
            parts.append(int(segment))
        except ValueError:
            break
    return tuple(parts)


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
    is_dev: bool = False


def _is_dev_install() -> bool:
    """Return True if swarm is running from a local editable/dev install."""
    import importlib.metadata

    try:
        dist = importlib.metadata.distribution("swarm-ai")
        # PEP 610: editable installs have a direct_url.json with dir_info.editable
        if dist.read_text("direct_url.json"):
            import json as _json

            info = _json.loads(dist.read_text("direct_url.json"))
            if info.get("dir_info", {}).get("editable", False):
                return True
            # Also flag file:// installs (local path installs via uv)
            if info.get("url", "").startswith("file://"):
                return True
    except (importlib.metadata.PackageNotFoundError, Exception):
        pass
    return False


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
    dev = _is_dev_install()
    result = UpdateResult(
        available=_version_tuple(remote) > _version_tuple(current),
        current_version=current,
        remote_version=remote,
        commit_sha=commit_info.get("sha", ""),
        commit_message=commit_info.get("message", ""),
        commit_date=commit_info.get("date", ""),
        is_dev=dev,
    )
    _write_cache(result)
    return result


def check_for_update_sync() -> UpdateResult | None:
    """Synchronous cache-only read for the CLI banner.

    Returns ``None`` if no cache exists or it is expired.
    """
    return _read_cache()


async def perform_update(
    on_output: Callable[[str], None] | None = None,
) -> tuple[bool, str]:
    """Install the latest version from GitHub via a single uv command.

    ``--force`` reinstalls even if present (no separate uninstall step).
    ``--no-cache`` bypasses the build cache (no separate cache-clean step).

    *on_output* is called with each line of stdout/stderr for live progress.

    Returns ``(success, combined_output)``.
    """
    cmd = ["uv", "tool", "install", "--force", "--no-cache", _INSTALL_SOURCE]

    def _emit(line: str) -> None:
        if on_output:
            on_output(line)

    _emit("Installing from GitHub...")
    print("  → Installing from GitHub...", flush=True)

    output_lines: list[str] = []
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        assert proc.stdout is not None
        try:
            async with asyncio.timeout(120):
                async for raw in proc.stdout:
                    line = raw.decode(errors="replace").rstrip()
                    output_lines.append(line)
                    _emit(line)
                await proc.wait()
        except TimeoutError:
            proc.kill()
            msg = "Command timed out after 120s"
            output_lines.append(msg)
            _emit(msg)
            return False, "\n".join(output_lines)

        if proc.returncode != 0:
            return False, "\n".join(output_lines)
    except Exception as exc:
        output_lines.append(str(exc))
        return False, "\n".join(output_lines)

    # Clear cache so next check reflects the new version
    try:
        _CACHE_FILE.unlink(missing_ok=True)
    except Exception:
        pass

    _emit("Update complete!")
    return True, "\n".join(output_lines)


def get_local_source_path() -> str | None:
    """Return the local filesystem path if swarm was installed from a local directory.

    Returns ``None`` for editable installs (changes already live), git installs,
    or PyPI installs.
    """
    import importlib.metadata

    try:
        dist = importlib.metadata.distribution("swarm-ai")
        raw = dist.read_text("direct_url.json")
        if not raw:
            return None
        info = json.loads(raw)
        # Editable installs don't need reinstalling — changes are live via symlinks
        if info.get("dir_info", {}).get("editable", False):
            return None
        url = info.get("url", "")
        if url.startswith("file://"):
            # Strip the file:// prefix to get the filesystem path
            return url[len("file://") :]
        return None
    except Exception:
        return None


async def reinstall_from_local_source(
    on_output: Callable[[str], None] | None = None,
) -> tuple[bool, str]:
    """Reinstall swarm from its local source path before a server restart.

    No-op (returns ``(True, "")``) when the package was not installed from a
    local directory (e.g. git, PyPI, or editable installs).

    Returns ``(success, combined_output)``.
    """
    source_path = get_local_source_path()
    if source_path is None:
        return True, ""

    cmd = ["uv", "tool", "install", "--force", "--no-cache", source_path]

    def _emit(line: str) -> None:
        if on_output:
            on_output(line)

    _emit(f"Reinstalling from local source: {source_path}")
    print(f"  → Reinstalling from local source: {source_path}", flush=True)

    output_lines: list[str] = []
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        assert proc.stdout is not None
        try:
            async with asyncio.timeout(120):
                async for raw in proc.stdout:
                    line = raw.decode(errors="replace").rstrip()
                    output_lines.append(line)
                    _emit(line)
                await proc.wait()
        except TimeoutError:
            proc.kill()
            msg = "Reinstall timed out after 120s"
            output_lines.append(msg)
            _emit(msg)
            return False, "\n".join(output_lines)

        if proc.returncode != 0:
            return False, "\n".join(output_lines)
    except Exception as exc:
        output_lines.append(str(exc))
        return False, "\n".join(output_lines)

    _emit("Local reinstall complete!")
    return True, "\n".join(output_lines)


def update_result_to_dict(result: UpdateResult) -> dict[str, Any]:
    """Serialize an UpdateResult for JSON API/WebSocket responses."""
    return asdict(result)
