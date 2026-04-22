"""Tests for the Queen CLAUDE.md upstream-contribution flow (task #258).

Companion to ``tests/test_queen_claude_md_reconcile.py``.  Where that one
exercises the shipped → local forward sync, this one exercises the local →
shipped reverse flow.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from swarm.queen.contribute import (
    _locate_constant_span,
    _rewrite_runtime_source,
    compute_status,
    count_hunks,
    emit_patch,
    mark_synced,
)
from swarm.queen.runtime import (
    CLAUDE_MD_FILENAME,
    QUEEN_SYSTEM_PROMPT,
    SHIPPED_MARKER_FILENAME,
)


@pytest.fixture()
def workdir(tmp_path) -> Path:
    return tmp_path / "queen-workdir"


def _write_claude(workdir: Path, body: str) -> Path:
    workdir.mkdir(parents=True, exist_ok=True)
    target = workdir / CLAUDE_MD_FILENAME
    target.write_text(body)
    return target


# ---------------------------------------------------------------------------
# compute_status: the diff/summary surface the CLI's status mode consumes
# ---------------------------------------------------------------------------


class TestComputeStatus:
    def test_reports_in_sync_when_local_matches_shipped(self, workdir):
        _write_claude(workdir, "shipped body\n")
        status = compute_status(workdir=workdir, shipped="shipped body\n")
        assert status.in_sync is True
        assert status.hunk_count == 0
        assert status.diff == ""

    def test_reports_diff_and_hunk_count_when_diverged(self, workdir):
        shipped = textwrap.dedent(
            """\
            # Header
            shipped paragraph
            more shipped content
            """
        )
        local = textwrap.dedent(
            """\
            # Header
            local paragraph
            more shipped content
            """
        )
        _write_claude(workdir, local)
        status = compute_status(workdir=workdir, shipped=shipped)

        assert status.in_sync is False
        assert status.hunk_count == 1
        # Diff is oriented shipped -> local.
        assert "-shipped paragraph" in status.diff
        assert "+local paragraph" in status.diff

    def test_missing_local_file_treats_local_as_empty(self, workdir):
        """Defensive: a missing CLAUDE.md reports a full-removal diff
        rather than raising.  Safe to call on a fresh install."""
        status = compute_status(workdir=workdir, shipped="something\n")
        assert status.in_sync is False
        # The diff shows the shipped content being removed.
        assert "-something" in status.diff


# ---------------------------------------------------------------------------
# _locate_constant_span / _rewrite_runtime_source: the emit-patch mechanics
# ---------------------------------------------------------------------------


class TestConstantSpan:
    def test_locates_open_and_close_lines(self):
        source = textwrap.dedent(
            '''\
            # preamble
            import x

            QUEEN_SYSTEM_PROMPT = """\\
            body line one
            body line two
            """

            # trailing stuff
            '''
        )
        start, end = _locate_constant_span(source)
        lines = source.splitlines(keepends=True)
        assert lines[start].startswith("QUEEN_SYSTEM_PROMPT")
        assert lines[end].strip() == '"""'

    def test_raises_when_header_missing(self):
        with pytest.raises(ValueError, match="could not locate"):
            _locate_constant_span("no constant here\n")


class TestRewriteRuntimeSource:
    def test_replaces_body_preserves_surroundings(self):
        source = textwrap.dedent(
            '''\
            import x

            QUEEN_SYSTEM_PROMPT = """\\
            old line one
            old line two
            """

            after_constant = 1
            '''
        )
        new_body = "new line one\nnew line two\nnew line three\n"
        rewritten = _rewrite_runtime_source(source, new_body)

        assert 'QUEEN_SYSTEM_PROMPT = """\\\n' in rewritten
        assert "new line one" in rewritten
        assert "new line three" in rewritten
        assert "old line" not in rewritten
        # Surrounding lines are intact.
        assert rewritten.startswith("import x\n")
        assert rewritten.rstrip().endswith("after_constant = 1")


# ---------------------------------------------------------------------------
# emit_patch: end-to-end patch shape + no-op handling
# ---------------------------------------------------------------------------


class TestEmitPatch:
    def _make_repo_root(self, tmp_path: Path, constant_body: str) -> Path:
        """Build a minimal fake swarm repo at tmp_path/repo-root.

        The constructed ``runtime.py`` has the exact line structure the
        contribute logic expects: ``QUEEN_SYSTEM_PROMPT = '''\\`` on its
        own line, the body, then a lone closing triple-quote.  Built
        with explicit newline assembly rather than ``textwrap.dedent``
        to keep the output exactly at column 0 regardless of where
        this helper sits in the source file.
        """
        repo = tmp_path / "repo-root"
        runtime_dir = repo / "src" / "swarm" / "queen"
        runtime_dir.mkdir(parents=True)
        body = constant_body if constant_body.endswith("\n") else constant_body + "\n"
        # Assemble the file contents: preamble + opening header +
        # body + closing + trailing line.  ``\\`` is the literal
        # backslash the runtime module uses as line-continuation so
        # the string doesn't start with a spurious leading newline.
        runtime_source = (
            f'# minimal runtime stand-in for tests\nQUEEN_SYSTEM_PROMPT = """\\\n{body}"""\n'
        )
        runtime_dir.joinpath("runtime.py").write_text(runtime_source)
        return repo

    def test_produces_git_applyable_diff(self, tmp_path, workdir):
        repo = self._make_repo_root(tmp_path, "shipped line one\nshipped line two\n")
        _write_claude(workdir, "local line one\nlocal line two\n")
        out = tmp_path / "contribution.patch"

        result = emit_patch(out, repo_root=repo, workdir=workdir)

        assert out.exists()
        assert result.hunk_count == 1
        assert result.bytes_written > 0
        patch = out.read_text()
        # Git header: consumers run `git apply` from repo root.
        assert "diff --git a/src/swarm/queen/runtime.py b/src/swarm/queen/runtime.py" in patch
        assert "--- a/src/swarm/queen/runtime.py" in patch
        assert "+++ b/src/swarm/queen/runtime.py" in patch
        # Hunk contains both the old and new body.
        assert "-shipped line one" in patch
        assert "+local line one" in patch

    def test_no_op_diff_writes_empty_file(self, tmp_path, workdir):
        """When local and shipped agree, the patch file is empty — callers
        see bytes_written=0 rather than a header-only stub that git apply
        would quietly succeed on."""
        body = "identical body\n"
        repo = self._make_repo_root(tmp_path, body)
        _write_claude(workdir, body)
        out = tmp_path / "empty.patch"

        result = emit_patch(out, repo_root=repo, workdir=workdir)

        assert result.hunk_count == 0
        assert result.bytes_written == 0
        assert out.read_text() == ""

    def test_missing_runtime_py_raises(self, tmp_path, workdir):
        _write_claude(workdir, "local\n")
        with pytest.raises(FileNotFoundError, match=r"runtime\.py not found"):
            emit_patch(tmp_path / "x.patch", repo_root=tmp_path, workdir=workdir)


# ---------------------------------------------------------------------------
# mark_synced: the drift-reset mechanism that closes the loop with #254
# ---------------------------------------------------------------------------


class TestMarkSynced:
    def test_updates_marker_to_current_live_file(self, workdir):
        _write_claude(workdir, "local content v2\n")
        # Simulate an older marker from the previous sync cycle.
        (workdir / SHIPPED_MARKER_FILENAME).write_text("old shipped v1\n")

        marker = mark_synced(workdir=workdir)

        assert marker == workdir / SHIPPED_MARKER_FILENAME
        assert marker.read_text() == "local content v2\n"

    def test_raises_when_live_file_missing(self, workdir):
        workdir.mkdir(parents=True, exist_ok=True)
        with pytest.raises(FileNotFoundError, match="nothing to mark"):
            mark_synced(workdir=workdir)

    def test_prevents_drift_flag_on_next_reconcile(self, workdir):
        """Post-mark-synced, a subsequent reconcile_queen_claude_md sees
        the marker matching on-disk and classifies the state as in-sync
        rather than drift-flagged."""
        from swarm.queen.runtime import ReconcileAction, reconcile_queen_claude_md

        # Set up a drift scenario: shipped moved, local edited.
        (workdir / CLAUDE_MD_FILENAME).parent.mkdir(parents=True, exist_ok=True)
        _write_claude(workdir, "local w/ contribution")
        # Marker points to old shipped, so next reconcile vs new shipped
        # would auto-flag drift (shipped changed, on-disk diverged).
        (workdir / SHIPPED_MARKER_FILENAME).write_text("old shipped")
        # Operator lands the contribution, new shipped now matches local.
        # They run --mark-synced.
        mark_synced(workdir=workdir)

        # Now reconcile against a shipped value matching the local file
        # (as it would after the merge of the contributed hunks).
        result = reconcile_queen_claude_md(workdir, shipped_latest="local w/ contribution")
        assert result.action == ReconcileAction.NO_OP


# ---------------------------------------------------------------------------
# count_hunks utility
# ---------------------------------------------------------------------------


class TestCountHunks:
    def test_counts_each_hunk_header(self):
        diff = textwrap.dedent(
            """\
            --- a
            +++ b
            @@ -1,3 +1,3 @@
             keep
            -old
            +new
             keep
            @@ -10,3 +10,3 @@
             keep
            -old2
            +new2
             keep
            """
        )
        assert count_hunks(diff) == 2

    def test_zero_hunks_when_no_diff(self):
        assert count_hunks("") == 0
        assert count_hunks("--- a\n+++ b\n") == 0


# ---------------------------------------------------------------------------
# CLI smoke — exercise the subcommand through Click so the flag topology
# and mutual-exclusion rules are pinned.
# ---------------------------------------------------------------------------


class TestCLI:
    def test_contribute_command_resolves_with_help(self):
        from click.testing import CliRunner

        from swarm.cli import main

        runner = CliRunner()
        result = runner.invoke(main, ["queen", "contribute-claude-md", "--help"])
        assert result.exit_code == 0
        assert "--emit-patch" in result.output
        assert "--open-pr" in result.output
        assert "--mark-synced" in result.output

    def test_mutually_exclusive_flags_rejected(self, tmp_path):
        from click.testing import CliRunner

        from swarm.cli import main

        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                "queen",
                "contribute-claude-md",
                "--emit-patch",
                str(tmp_path / "x.patch"),
                "--mark-synced",
            ],
        )
        assert result.exit_code != 0
        assert "mutually exclusive" in result.output


# ---------------------------------------------------------------------------
# Sanity check: the on-disk CLAUDE.md and the shipped constant are in
# sync at commit time.  Keeps the repo honest — if this fails, either
# ``QUEEN_SYSTEM_PROMPT`` drifted from the live file or vice versa, and
# future releases will drift-flag incorrectly.
# ---------------------------------------------------------------------------


def test_shipped_constant_matches_live_claude_md_at_commit_time():
    """Skipped gracefully if the workdir file isn't present (e.g. CI)."""
    workdir = Path.home() / ".swarm" / "queen" / "workdir"
    live = workdir / CLAUDE_MD_FILENAME
    if not live.exists():
        pytest.skip("no local CLAUDE.md — running in CI or a fresh env")
    local = live.read_text()
    assert local == QUEEN_SYSTEM_PROMPT, (
        "local CLAUDE.md diverged from shipped QUEEN_SYSTEM_PROMPT — run "
        "`swarm queen contribute-claude-md` to surface the diff before shipping"
    )
