"""End-to-end smoke of the Queen setup on a fresh install (task #255).

Simulates what happens on a brand-new user's first `swarm start`: no
prior DB, no workdir, default config.  Traces the Queen setup artifacts
the daemon constructs (schema, config seed, CLAUDE.md reconcile, workdir
layout) and asserts each lands correctly.

These are smoke tests, not full daemon boots — they exercise the
individual install seams in the order the daemon hits them, catching
regressions if any one step becomes dependent on prior-install state
that a fresh user wouldn't have.
"""

from __future__ import annotations

import sqlite3

from swarm.config.models import HiveConfig, QueenConfig
from swarm.db import SwarmDB
from swarm.db.migrate import auto_migrate
from swarm.queen.queen import HEADLESS_DECISION_PROMPT
from swarm.queen.runtime import (
    CLAUDE_MD_FILENAME,
    QUEEN_SYSTEM_PROMPT,
    SHIPPED_MARKER_FILENAME,
    ReconcileAction,
    reconcile_queen_claude_md,
)


class TestFreshInstallQueenWorkdir:
    """Queen workdir + CLAUDE.md setup on a clean filesystem."""

    def test_reconcile_creates_workdir_when_parent_missing(self, tmp_path):
        """First daemon boot: neither ~/.swarm nor ~/.swarm/queen exists.
        Reconcile must create the full path without failing."""
        workdir = tmp_path / "brand-new" / ".swarm" / "queen" / "workdir"
        assert not workdir.parent.exists()

        result = reconcile_queen_claude_md(workdir)
        assert result.action == ReconcileAction.SEEDED
        assert workdir.exists()
        assert (workdir / CLAUDE_MD_FILENAME).exists()
        assert (workdir / SHIPPED_MARKER_FILENAME).exists()

    def test_fresh_claude_md_contains_expected_role_markers(self, tmp_path):
        workdir = tmp_path / "workdir"
        reconcile_queen_claude_md(workdir)
        body = (workdir / CLAUDE_MD_FILENAME).read_text()
        # Non-empty + carries the identity header.
        assert body.startswith("# You are the Queen")
        # The two major sections a fresh operator should see.
        assert "## Hierarchy" in body
        assert "## Two Queens" in body

    def test_shipped_marker_matches_shipped_constant_on_fresh_seed(self, tmp_path):
        workdir = tmp_path / "workdir"
        reconcile_queen_claude_md(workdir)
        marker = (workdir / SHIPPED_MARKER_FILENAME).read_text()
        # Marker is the immutable reference point — must equal the
        # constant at seed time so future reconciles correctly detect
        # drift.
        assert marker == QUEEN_SYSTEM_PROMPT


class TestFreshInstallDatabase:
    """auto_migrate on a non-existent DB must produce every Queen-critical table."""

    QUEEN_CRITICAL_TABLES = (
        "tasks",
        "messages",
        "buzz_log",
        "queen_learnings",
        "queen_threads",
        "queen_messages",
        "queen_sessions",
        "worker_blockers",
        "workers",
        "proposals",
        "schema_version",
    )

    def test_auto_migrate_creates_all_queen_critical_tables(self, tmp_path):
        db_path = tmp_path / "swarm.db"
        assert not db_path.exists()

        db = SwarmDB(db_path)
        auto_migrate(db)
        assert db_path.exists()

        with sqlite3.connect(str(db_path)) as conn:
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
        missing = [t for t in self.QUEEN_CRITICAL_TABLES if t not in tables]
        assert not missing, f"missing queen-critical tables after auto_migrate: {missing}"

    def test_auto_migrate_is_idempotent(self, tmp_path):
        """Running auto_migrate twice on an empty DB must not fail or
        rewrite schema.  Covers the reload path where the daemon execv's
        and runs migrate again."""
        db_path = tmp_path / "swarm.db"
        db = SwarmDB(db_path)
        auto_migrate(db)
        first_version_rows = _read_schema_version_rows(db_path)

        auto_migrate(db)
        second_version_rows = _read_schema_version_rows(db_path)

        assert first_version_rows == second_version_rows


class TestFreshInstallConfigDefaults:
    """QueenConfig + HiveConfig defaults must carry everything the daemon
    expects out of the box so a missing swarm.yaml doesn't break install."""

    def test_queen_config_defaults(self):
        qc = QueenConfig()
        assert qc.enabled is True  # daemon will spawn Queen on start
        assert qc.cooldown > 0  # headless path needs a non-zero throttle
        assert qc.system_prompt == ""  # empty → daemon seeds it
        assert 0 < qc.min_confidence <= 1.0
        assert qc.auto_assign_tasks is True
        assert qc.oversight.enabled is True

    def test_hive_config_has_queen_section(self):
        """``HiveConfig()`` with no args must populate ``.queen``."""
        hc = HiveConfig()
        assert hc.queen is not None
        assert isinstance(hc.queen, QueenConfig)

    def test_daemon_seed_populates_empty_system_prompt(self):
        """Simulate the daemon's ``if not config.queen.system_prompt``
        seed path (from task #253) on a default config."""
        cfg = HiveConfig()
        assert cfg.queen.system_prompt == ""  # fresh install starts empty
        if not cfg.queen.system_prompt:
            cfg.queen.system_prompt = HEADLESS_DECISION_PROMPT
        assert cfg.queen.system_prompt
        # Role markers the headless prompt must carry.
        assert "headless Queen" in cfg.queen.system_prompt
        assert "stateless decision" in cfg.queen.system_prompt


class TestFreshInstallFullBootSequence:
    """Simulate the daemon's Queen-setup seam in the order it executes
    on first boot: DB migrate → reconcile CLAUDE.md → seed headless
    prompt.  Verifies nothing silently depends on pre-existing state."""

    def test_first_boot_sequence_end_to_end(self, tmp_path):
        # Step 1: DB migrate (daemon.__init__ line ~128)
        db_path = tmp_path / "swarm.db"
        db = SwarmDB(db_path)
        auto_migrate(db)

        # Step 2: HiveConfig default construction (no swarm.yaml)
        cfg = HiveConfig()

        # Step 3: Daemon headless prompt seed (task #253)
        if not cfg.queen.system_prompt:
            cfg.queen.system_prompt = HEADLESS_DECISION_PROMPT

        # Step 4: Queen CLAUDE.md reconcile (task #254)
        workdir = tmp_path / "queen-workdir"
        result = reconcile_queen_claude_md(workdir)

        # Post-boot assertions:
        # - Schema is in place
        assert db_path.exists()
        # - Headless decision prompt is live in-memory
        assert "headless Queen" in cfg.queen.system_prompt
        # - Workdir materialized with both the live file + shipped marker
        assert (workdir / CLAUDE_MD_FILENAME).exists()
        assert (workdir / SHIPPED_MARKER_FILENAME).exists()
        # - Reconcile reported SEEDED (not anything else) on a true fresh install
        assert result.action == ReconcileAction.SEEDED


def _read_schema_version_rows(db_path) -> list[tuple]:
    with sqlite3.connect(str(db_path)) as conn:
        return conn.execute("SELECT * FROM schema_version ORDER BY version").fetchall()
