"""Tests for the Queen CLAUDE.md reconcile + sync logic (task #254)."""

from __future__ import annotations

import pytest

from swarm.queen.runtime import (
    CLAUDE_MD_FILENAME,
    DRIFT_SHIPPED_LAST_SUFFIX,
    DRIFT_SHIPPED_LATEST_SUFFIX,
    QUEEN_SYSTEM_PROMPT,
    SHIPPED_MARKER_FILENAME,
    ReconcileAction,
    reconcile_queen_claude_md,
    sync_queen_claude_md,
)


@pytest.fixture()
def workdir(tmp_path):
    """Ephemeral Queen workdir — fresh for every test."""
    return tmp_path / "queen-workdir"


def _files(workdir):
    """Return the key paths for assertions."""
    return {
        "target": workdir / CLAUDE_MD_FILENAME,
        "marker": workdir / SHIPPED_MARKER_FILENAME,
        "latest": workdir / f"{CLAUDE_MD_FILENAME}{DRIFT_SHIPPED_LATEST_SUFFIX}",
        "last": workdir / f"{CLAUDE_MD_FILENAME}{DRIFT_SHIPPED_LAST_SUFFIX}",
    }


class TestReconcileFreshInstall:
    def test_seeds_file_and_marker_when_nothing_exists(self, workdir):
        """Fresh workdir → reconcile writes both the live file and marker."""
        result = reconcile_queen_claude_md(workdir, shipped_latest="shipped v1")
        f = _files(workdir)
        assert result.action == ReconcileAction.SEEDED
        assert f["target"].read_text() == "shipped v1"
        assert f["marker"].read_text() == "shipped v1"
        assert not f["latest"].exists()
        assert not f["last"].exists()


class TestReconcileFirstUpgrade:
    def test_seeds_marker_from_on_disk_when_only_target_exists(self, workdir):
        """Pre-existing CLAUDE.md with no marker → treat current on-disk
        as the baseline (operator upgraded from pre-reconcile swarm)."""
        workdir.mkdir()
        f = _files(workdir)
        f["target"].write_text("operator had this before upgrade")

        result = reconcile_queen_claude_md(workdir, shipped_latest="shipped v2")
        assert result.action == ReconcileAction.MARKER_SEEDED
        # On-disk must not be overwritten on first contact.
        assert f["target"].read_text() == "operator had this before upgrade"
        # Marker captures the baseline.
        assert f["marker"].read_text() == "operator had this before upgrade"
        assert not f["latest"].exists()


class TestReconcileNoOp:
    def test_no_action_when_shipped_unchanged_and_on_disk_clean(self, workdir):
        workdir.mkdir()
        f = _files(workdir)
        f["target"].write_text("same content")
        f["marker"].write_text("same content")

        result = reconcile_queen_claude_md(workdir, shipped_latest="same content")
        assert result.action == ReconcileAction.NO_OP
        assert f["target"].read_text() == "same content"

    def test_no_action_when_shipped_unchanged_even_with_local_edits(self, workdir):
        """Operator edits live file; shipped hasn't moved → nothing to
        merge, stay out of the way."""
        workdir.mkdir()
        f = _files(workdir)
        f["target"].write_text("operator edits here")
        f["marker"].write_text("original shipped v1")

        result = reconcile_queen_claude_md(workdir, shipped_latest="original shipped v1")
        assert result.action == ReconcileAction.NO_OP
        # Operator edits preserved verbatim.
        assert f["target"].read_text() == "operator edits here"


class TestReconcileAutoUpdate:
    def test_auto_updates_when_shipped_changed_and_on_disk_clean(self, workdir):
        """Shipped moved, on-disk == marker (no local edits) → replace."""
        workdir.mkdir()
        f = _files(workdir)
        f["target"].write_text("shipped v1")
        f["marker"].write_text("shipped v1")

        result = reconcile_queen_claude_md(workdir, shipped_latest="shipped v2 with new section")
        assert result.action == ReconcileAction.AUTO_UPDATED
        assert f["target"].read_text() == "shipped v2 with new section"
        assert f["marker"].read_text() == "shipped v2 with new section"
        # No drift artifacts for the clean-update path.
        assert not f["latest"].exists()
        assert not f["last"].exists()


class TestReconcileDrift:
    def test_drift_flagged_when_both_shipped_and_on_disk_diverged(self, workdir):
        """Both sides moved independently → write side-by-side refs,
        do NOT overwrite on-disk."""
        workdir.mkdir()
        f = _files(workdir)
        f["target"].write_text("operator edits on top of v1")
        f["marker"].write_text("shipped v1")

        result = reconcile_queen_claude_md(workdir, shipped_latest="shipped v2 with new section")
        assert result.action == ReconcileAction.DRIFT_FLAGGED
        # On-disk MUST survive untouched.
        assert f["target"].read_text() == "operator edits on top of v1"
        # Reference files written so operator has a diff to work from.
        assert f["latest"].read_text() == "shipped v2 with new section"
        assert f["last"].read_text() == "shipped v1"

    def test_drift_result_details_names_both_reference_files(self, workdir):
        workdir.mkdir()
        f = _files(workdir)
        f["target"].write_text("operator changes")
        f["marker"].write_text("baseline")

        result = reconcile_queen_claude_md(workdir, shipped_latest="new ship")
        assert f["latest"].name in result.details
        assert f["last"].name in result.details


class TestSyncAcceptShipped:
    def test_replaces_on_disk_and_updates_marker(self, workdir, monkeypatch):
        workdir.mkdir()
        f = _files(workdir)
        f["target"].write_text("operator edits")
        f["marker"].write_text("old shipped")
        f["latest"].write_text("latest ship ref")
        f["last"].write_text("last ship ref")

        result = sync_queen_claude_md("accept-shipped", workdir=workdir)
        assert result.action == ReconcileAction.AUTO_UPDATED
        assert f["target"].read_text() == QUEEN_SYSTEM_PROMPT
        assert f["marker"].read_text() == QUEEN_SYSTEM_PROMPT
        # Drift refs cleared after reconcile.
        assert not f["latest"].exists()
        assert not f["last"].exists()


class TestSyncKeepLocal:
    def test_updates_marker_only_and_preserves_local_edits(self, workdir):
        workdir.mkdir()
        f = _files(workdir)
        f["target"].write_text("operator edits stay")
        f["marker"].write_text("old shipped")
        f["latest"].write_text("latest ship ref")
        f["last"].write_text("last ship ref")

        result = sync_queen_claude_md("keep-local", workdir=workdir)
        assert result.action == ReconcileAction.NO_OP
        # On-disk survives verbatim.
        assert f["target"].read_text() == "operator edits stay"
        # Marker bumped to latest shipped so future reconciles don't
        # re-flag the same drift.
        assert f["marker"].read_text() == QUEEN_SYSTEM_PROMPT
        # Drift refs cleared.
        assert not f["latest"].exists()
        assert not f["last"].exists()


class TestSyncRejectsUnknownMode:
    def test_unknown_mode_raises_value_error(self, workdir):
        with pytest.raises(ValueError, match="unknown sync mode"):
            sync_queen_claude_md("rebase-please", workdir=workdir)


class TestDriftReconcileIsIdempotent:
    def test_rerunning_reconcile_doesnt_change_state(self, workdir):
        """Calling reconcile twice in the drift-flagged state shouldn't
        churn the on-disk live file or the marker — only the drift refs
        may be rewritten identically."""
        workdir.mkdir()
        f = _files(workdir)
        f["target"].write_text("operator changes")
        f["marker"].write_text("v1 baseline")

        reconcile_queen_claude_md(workdir, shipped_latest="v2 ship")
        first_target = f["target"].read_text()
        first_marker = f["marker"].read_text()

        reconcile_queen_claude_md(workdir, shipped_latest="v2 ship")
        assert f["target"].read_text() == first_target
        assert f["marker"].read_text() == first_marker


class TestFullUpgradeDriftThenAcceptCycle:
    def test_end_to_end_cycle(self, workdir):
        """Simulate the full lifecycle: fresh install → operator edits →
        new shipped version → drift detected → operator accepts shipped.
        """
        # 1. Fresh install — daemon boots, seeds file + marker from v1.
        result = reconcile_queen_claude_md(workdir, shipped_latest="v1 shipped")
        assert result.action == ReconcileAction.SEEDED
        f = _files(workdir)
        assert f["target"].read_text() == "v1 shipped"

        # 2. Operator / Queen edits the live file between releases.
        f["target"].write_text("v1 shipped + operator tweaks")

        # 3. User upgrades; daemon boots; shipped is now v2.
        result = reconcile_queen_claude_md(workdir, shipped_latest="v2 shipped")
        assert result.action == ReconcileAction.DRIFT_FLAGGED
        # Live file untouched; refs written.
        assert f["target"].read_text() == "v1 shipped + operator tweaks"
        assert f["latest"].read_text() == "v2 shipped"

        # 4. Operator runs `swarm queen sync-claude-md --accept-shipped`.
        #    (Uses the production QUEEN_SYSTEM_PROMPT — just assert the
        #    flow clears drift refs and aligns marker with shipped.)
        result = sync_queen_claude_md("accept-shipped", workdir=workdir)
        assert result.action == ReconcileAction.AUTO_UPDATED
        assert f["target"].read_text() == QUEEN_SYSTEM_PROMPT
        assert f["marker"].read_text() == QUEEN_SYSTEM_PROMPT
        assert not f["latest"].exists()

        # 5. Next daemon boot — should be a no-op.
        result = reconcile_queen_claude_md(workdir, shipped_latest=QUEEN_SYSTEM_PROMPT)
        assert result.action == ReconcileAction.NO_OP
