"""Reverse-sync for the interactive Queen's CLAUDE.md (task #258).

Companion to :mod:`swarm.queen.runtime`'s forward reconcile (task #254):
where ``reconcile_queen_claude_md`` pushes the shipped
``QUEEN_SYSTEM_PROMPT`` into the local workdir file, the functions here
go the other direction — taking local edits and producing a patch the
operator can land upstream in the swarm repository.

Design note (operator-clarified, 2026-04-22): **every diff between local
and shipped is a candidate for upstream contribution.**  The Queen is a
global role; there is no "deployment-specific Queen content."  Anything
the operator or Queen added locally should flow back to the shipped
``QUEEN_SYSTEM_PROMPT`` so fresh installs inherit the same improvements.
No per-hunk classification / local-only markers needed.  Operator can
defer a contribution by not running the CLI yet, or strip a hunk from
the emitted patch by hand, but there's no persistent "keep this local
forever" state.
"""

from __future__ import annotations

import difflib
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from swarm.logging import get_logger
from swarm.queen.runtime import (
    CLAUDE_MD_FILENAME,
    QUEEN_SYSTEM_PROMPT,
    QUEEN_WORK_DIR,
    SHIPPED_MARKER_FILENAME,
)

_log = get_logger("queen.contribute")

# Path inside the swarm repo where the shipped constant lives.  The
# patch we emit targets this file so ``git apply`` (or a PR-create flow)
# can land the contribution cleanly.
_RUNTIME_MODULE_REL_PATH = "src/swarm/queen/runtime.py"

# The string-literal header we locate in ``runtime.py`` to identify the
# boundary of the ``QUEEN_SYSTEM_PROMPT`` assignment.  The value is
# written as ``QUEEN_SYSTEM_PROMPT = """\`` on a single line; the
# closing is a lone triple-quote on its own line.  Both are searched
# line-wise so we don't accidentally match inside a docstring.
_CONSTANT_OPEN_LINE = 'QUEEN_SYSTEM_PROMPT = """\\\n'
_CONSTANT_CLOSE_LINE = '"""\n'


@dataclass
class ContributeStatus:
    """Outcome of inspecting local vs shipped CLAUDE.md content."""

    shipped: str
    local: str
    diff: str  # unified diff between the two (may be empty)
    hunk_count: int

    @property
    def in_sync(self) -> bool:
        return self.local == self.shipped


@dataclass
class PatchEmitResult:
    """Outcome of ``emit_patch``."""

    path: Path
    target_rel_path: str  # always ``_RUNTIME_MODULE_REL_PATH``
    hunk_count: int
    bytes_written: int


def compute_status(
    workdir: Path | None = None,
    shipped: str = QUEEN_SYSTEM_PROMPT,
) -> ContributeStatus:
    """Compare the on-disk CLAUDE.md against the shipped constant.

    Returns a :class:`ContributeStatus` with the unified diff (oriented
    shipped → local) and the count of diff hunks (each ``@@`` header
    counts as one hunk).  Safe to call with a missing CLAUDE.md — the
    status will show the full constant as the "local" side removal.
    """
    workdir = workdir or QUEEN_WORK_DIR
    target = workdir / CLAUDE_MD_FILENAME
    local = target.read_text() if target.exists() else ""

    if local == shipped:
        return ContributeStatus(shipped=shipped, local=local, diff="", hunk_count=0)

    diff = "".join(
        difflib.unified_diff(
            shipped.splitlines(keepends=True),
            local.splitlines(keepends=True),
            fromfile="QUEEN_SYSTEM_PROMPT (shipped)",
            tofile="CLAUDE.md (local)",
            n=3,
        )
    )
    hunk_count = sum(1 for line in diff.splitlines() if line.startswith("@@"))
    return ContributeStatus(shipped=shipped, local=local, diff=diff, hunk_count=hunk_count)


def _locate_constant_span(runtime_source: str) -> tuple[int, int]:
    """Return ``(start_line_idx, end_line_idx)`` bounding ``QUEEN_SYSTEM_PROMPT``.

    The span includes both boundary lines — the opening assignment
    header (matching ``_CONSTANT_OPEN_LINE``) and the lone closing
    triple-quote line (matching ``_CONSTANT_CLOSE_LINE``).  Raises
    :class:`ValueError` if either boundary is missing or if the closing
    isn't found after the opening.
    """
    lines = runtime_source.splitlines(keepends=True)
    try:
        start = lines.index(_CONSTANT_OPEN_LINE)
    except ValueError as e:
        raise ValueError(
            f"could not locate {_CONSTANT_OPEN_LINE!r} header in runtime.py; "
            "the constant layout may have changed."
        ) from e
    # Search for the closing triple-quote AFTER the opening.  The
    # string body itself must not contain a lone triple-quote line for
    # this to work — an invariant the runtime module maintains.
    for offset, line in enumerate(lines[start + 1 :], start=1):
        if line == _CONSTANT_CLOSE_LINE:
            return start, start + offset
    raise ValueError(
        "reached EOF without finding the closing triple-quote for QUEEN_SYSTEM_PROMPT in runtime.py"
    )


def _rewrite_runtime_source(
    runtime_source: str,
    new_body: str,
) -> str:
    """Replace the ``QUEEN_SYSTEM_PROMPT`` body in ``runtime_source``.

    ``new_body`` is the full replacement string — everything that
    should appear between the opening and closing triple-quotes,
    including trailing newline.  The surrounding boundary lines
    (the ``QUEEN_SYSTEM_PROMPT =`` header and the lone closing
    triple-quote) are preserved.
    """
    lines = runtime_source.splitlines(keepends=True)
    start, end = _locate_constant_span(runtime_source)
    # Replace the body (lines between start+1 and end, exclusive of the
    # closing triple-quote line).
    new_body_lines = new_body.splitlines(keepends=True)
    rebuilt = lines[: start + 1] + new_body_lines + lines[end:]
    return "".join(rebuilt)


def emit_patch(
    out_path: Path,
    *,
    repo_root: Path,
    workdir: Path | None = None,
) -> PatchEmitResult:
    """Write a ``git apply``-able patch promoting local → shipped.

    The patch is generated from the current ``runtime.py`` on disk +
    the rewritten version containing the local CLAUDE.md content as the
    new ``QUEEN_SYSTEM_PROMPT``.  Diff headers use the relative path
    ``src/swarm/queen/runtime.py`` so the operator can:

        cd <swarm-repo> && git apply <out_path>

    Raises :class:`FileNotFoundError` if ``runtime.py`` can't be found
    at the expected repo-relative path, or :class:`ValueError` if the
    constant layout has drifted from what :func:`_locate_constant_span`
    expects.
    """
    workdir = workdir or QUEEN_WORK_DIR
    runtime_abs = repo_root / _RUNTIME_MODULE_REL_PATH
    if not runtime_abs.exists():
        raise FileNotFoundError(
            f"runtime.py not found at {runtime_abs} — pass a different "
            "--repo-root that points to a swarm checkout"
        )
    runtime_source = runtime_abs.read_text()

    local_md = (workdir / CLAUDE_MD_FILENAME).read_text()
    new_runtime = _rewrite_runtime_source(runtime_source, local_md)

    if new_runtime == runtime_source:
        # No-op diff — write an empty file to make that obvious rather
        # than a header-only stub that git apply would accept silently.
        out_path.write_text("")
        return PatchEmitResult(
            path=out_path,
            target_rel_path=_RUNTIME_MODULE_REL_PATH,
            hunk_count=0,
            bytes_written=0,
        )

    diff = "".join(
        difflib.unified_diff(
            runtime_source.splitlines(keepends=True),
            new_runtime.splitlines(keepends=True),
            fromfile=f"a/{_RUNTIME_MODULE_REL_PATH}",
            tofile=f"b/{_RUNTIME_MODULE_REL_PATH}",
            n=3,
        )
    )
    # git-apply expects a diff header line of the form:
    #   diff --git a/<path> b/<path>
    # followed by ``---`` / ``+++`` file markers and ``@@`` hunk headers.
    # ``unified_diff`` doesn't emit the ``diff --git`` header; prepend it.
    diff_with_git_header = (
        f"diff --git a/{_RUNTIME_MODULE_REL_PATH} b/{_RUNTIME_MODULE_REL_PATH}\n{diff}"
    )
    out_path.write_text(diff_with_git_header)
    hunk_count = sum(1 for line in diff.splitlines() if line.startswith("@@"))
    return PatchEmitResult(
        path=out_path,
        target_rel_path=_RUNTIME_MODULE_REL_PATH,
        hunk_count=hunk_count,
        bytes_written=len(diff_with_git_header),
    )


@dataclass
class PRResult:
    """Outcome of ``open_pr``."""

    branch: str
    pr_url: str | None  # None when gh failed / wasn't available
    message: str


def _run(cmd: list[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd, cwd=str(cwd), check=False, capture_output=True, text=True, timeout=60
    )


def open_pr(
    *,
    repo_root: Path,
    workdir: Path | None = None,
    branch: str | None = None,
    title: str = "Queen CLAUDE.md: promote local edits upstream",
) -> PRResult:
    """Apply the contribution as a branch + PR against the swarm repo.

    Runs: new branch, apply the rewrite in-place, commit, push, ``gh pr
    create``.  Graceful failure when either ``gh`` isn't installed /
    authenticated or the repo doesn't have a remote configured.  On
    failure, returns a :class:`PRResult` with ``pr_url=None`` and an
    explanatory message; does not raise.
    """
    workdir = workdir or QUEEN_WORK_DIR
    branch = branch or _suggest_branch_name()
    runtime_abs = repo_root / _RUNTIME_MODULE_REL_PATH
    if not runtime_abs.exists():
        return PRResult(branch=branch, pr_url=None, message=f"{runtime_abs} not found")

    # Require a clean worktree before we start — mixing contribution
    # changes with unrelated edits is a footgun.
    status = _run(["git", "status", "--porcelain"], cwd=repo_root)
    if status.returncode != 0:
        return PRResult(
            branch=branch, pr_url=None, message="git status failed (is this a git repo?)"
        )
    if status.stdout.strip():
        return PRResult(
            branch=branch,
            pr_url=None,
            message=(
                "working tree has uncommitted changes — commit or stash them "
                "before running --open-pr"
            ),
        )

    # Check for gh upfront.
    gh_check = _run(["gh", "auth", "status"], cwd=repo_root)
    if gh_check.returncode != 0:
        return PRResult(
            branch=branch,
            pr_url=None,
            message="gh CLI not authenticated — run `gh auth login` first",
        )

    runtime_source = runtime_abs.read_text()
    local_md = (workdir / CLAUDE_MD_FILENAME).read_text()
    try:
        new_runtime = _rewrite_runtime_source(runtime_source, local_md)
    except ValueError as e:
        return PRResult(branch=branch, pr_url=None, message=f"rewrite failed: {e}")

    if new_runtime == runtime_source:
        return PRResult(branch=branch, pr_url=None, message="local and shipped are already in sync")

    # Create the branch + make the change + commit.
    checkout = _run(["git", "checkout", "-b", branch], cwd=repo_root)
    if checkout.returncode != 0:
        return PRResult(
            branch=branch, pr_url=None, message=f"git checkout -b failed: {checkout.stderr}"
        )
    runtime_abs.write_text(new_runtime)
    add = _run(["git", "add", _RUNTIME_MODULE_REL_PATH], cwd=repo_root)
    if add.returncode != 0:
        return PRResult(branch=branch, pr_url=None, message=f"git add failed: {add.stderr}")
    commit_msg = (
        "docs(queen): promote local CLAUDE.md edits to QUEEN_SYSTEM_PROMPT\n\n"
        "Emitted by `swarm queen contribute-claude-md --open-pr` (task #258).\n"
        "This brings the shipped constant into alignment with the authored "
        "CLAUDE.md at the swarm install that ran this command."
    )
    commit = _run(["git", "commit", "-m", commit_msg], cwd=repo_root)
    if commit.returncode != 0:
        return PRResult(branch=branch, pr_url=None, message=f"git commit failed: {commit.stderr}")
    push = _run(["git", "push", "-u", "origin", branch], cwd=repo_root)
    if push.returncode != 0:
        return PRResult(branch=branch, pr_url=None, message=f"git push failed: {push.stderr}")

    pr_body = (
        "Promotes local edits to `~/.swarm/queen/workdir/CLAUDE.md` back into "
        "`QUEEN_SYSTEM_PROMPT` (`src/swarm/queen/runtime.py`).\n\n"
        "Once merged, run `swarm queen contribute-claude-md --mark-synced` on "
        "the contributing install so the drift reconcile no longer flags this "
        "delta."
    )
    pr_create = _run(["gh", "pr", "create", "--title", title, "--body", pr_body], cwd=repo_root)
    if pr_create.returncode != 0:
        return PRResult(
            branch=branch,
            pr_url=None,
            message=f"gh pr create failed: {pr_create.stderr}",
        )
    pr_url = pr_create.stdout.strip().splitlines()[-1] if pr_create.stdout.strip() else ""
    return PRResult(branch=branch, pr_url=pr_url, message=f"PR opened: {pr_url}")


def _suggest_branch_name() -> str:
    """Generate a default branch name for ``open_pr``.

    Keeps it deterministic-ish per invocation via a short hash of the
    local CLAUDE.md so repeated failed attempts don't accumulate
    gibberish branches.
    """
    import hashlib

    try:
        local = (QUEEN_WORK_DIR / CLAUDE_MD_FILENAME).read_text()
    except OSError:
        local = ""
    h = hashlib.sha1(local.encode("utf-8"), usedforsecurity=False).hexdigest()[:8]
    return f"queen-claude-md-contrib-{h}"


def mark_synced(workdir: Path | None = None) -> Path:
    """Update the ``.claude_md_shipped`` marker to the current local file.

    Call this after a contribution PR has been merged upstream.  Once
    run, ``reconcile_queen_claude_md`` sees the marker matching on-disk
    and treats the state as in-sync (no more drift-flagging of the
    already-upstreamed hunks).  Idempotent — the marker is overwritten
    with the current live CLAUDE.md every call.
    """
    workdir = workdir or QUEEN_WORK_DIR
    target = workdir / CLAUDE_MD_FILENAME
    marker = workdir / SHIPPED_MARKER_FILENAME
    if not target.exists():
        raise FileNotFoundError(f"{target} does not exist; nothing to mark as synced")
    marker.write_text(target.read_text())
    return marker


def detect_repo_root() -> Path | None:
    """Try to locate the swarm repo checkout.

    Heuristics in order:

    1. The directory of the installed ``swarm`` package if it sits
       inside ``<repo>/src/swarm``.
    2. ``~/projects/swarm`` or ``~/projects/personal/swarm`` — common
       operator layouts.

    Returns ``None`` when no candidate contains the expected
    ``src/swarm/queen/runtime.py``.
    """
    candidates: list[Path] = []
    # 1. Walk up from the installed swarm package.  If the user ran
    #    ``pip install -e .`` the installed path is the repo.  For
    #    regular ``uv tool install`` it lives in a tools venv without
    #    a usable repo, and this path just fails to resolve.
    try:
        import swarm

        pkg_path = Path(swarm.__file__).resolve().parent  # .../src/swarm
        if pkg_path.parts[-2:] == ("src", "swarm"):
            candidates.append(pkg_path.parent.parent)  # .../<repo>
    except Exception:
        pass
    # 2. Common operator layouts.
    home = Path.home()
    candidates.extend(
        [
            home / "projects" / "swarm",
            home / "projects" / "personal" / "swarm",
            home / "code" / "swarm",
        ]
    )
    for c in candidates:
        if (c / _RUNTIME_MODULE_REL_PATH).exists():
            return c
    return None


# Utility used by status-mode rendering.  Exposed so the CLI and tests
# can share the hunk-counting logic without importing difflib.
def count_hunks(diff: str) -> int:
    return sum(1 for line in diff.splitlines() if re.match(r"^@@ ", line))
