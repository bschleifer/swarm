"""Tests for scripts/release.py — calver bump + CHANGELOG promotion.

The release script is what ``/ship`` runs before every commit to keep the
version number and the CHANGELOG in sync. These tests cover the three
pure-logic entry points so edits to the script's CLI glue can't silently
break the bump arithmetic or the CHANGELOG rewrite.
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pytest

# Make scripts/ importable without installing it.
_SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(_SCRIPTS))

from release import compute_next_version, promote_changelog, update_version_files  # noqa: E402


class TestComputeNextVersion:
    def test_past_date_current_bumps_to_today(self) -> None:
        assert compute_next_version("2026.4.16.4", date(2026, 4, 17)) == "2026.4.17"

    def test_same_date_no_patch_bumps_to_patch_2(self) -> None:
        assert compute_next_version("2026.4.17", date(2026, 4, 17)) == "2026.4.17.2"

    def test_same_date_with_patch_increments_patch(self) -> None:
        assert compute_next_version("2026.4.17.2", date(2026, 4, 17)) == "2026.4.17.3"
        assert compute_next_version("2026.4.17.9", date(2026, 4, 17)) == "2026.4.17.10"

    def test_no_leading_zeros_in_base(self) -> None:
        # April 7 → "2026.4.7" (not "2026.04.07"); that's the format every
        # existing CHANGELOG / pyproject entry uses.
        assert compute_next_version("2026.3.31", date(2026, 4, 7)) == "2026.4.7"

    def test_double_digit_month_and_day(self) -> None:
        assert compute_next_version("2025.11.30", date(2025, 12, 15)) == "2025.12.15"

    def test_year_rollover(self) -> None:
        assert compute_next_version("2025.12.31.4", date(2026, 1, 1)) == "2026.1.1"

    def test_current_ahead_of_today_raises(self) -> None:
        # Shouldn't happen in practice, but protect against clock skew
        # silently rolling the version backwards.
        with pytest.raises(ValueError, match="ahead of today"):
            compute_next_version("2027.1.1", date(2026, 4, 17))


class TestPromoteChangelog:
    def _changelog(self, unreleased_body: str) -> str:
        return f"""# Changelog

Swarm uses calendar versioning.

## Unreleased
{unreleased_body}
---

## v1.0.0

Initial release.
"""

    def test_promotes_populated_unreleased_and_resets(self) -> None:
        body = """
### Features
- Something useful
- Another thing

### Fixes
- A bug squashed
"""
        promoted = promote_changelog(self._changelog(body), "2026.4.17", "2026-04-17")

        # Old content lives under a dated heading now
        assert "## [2026.4.17] - 2026-04-17" in promoted
        assert "Something useful" in promoted
        assert "A bug squashed" in promoted

        # Unreleased still present but reset to the empty sub-header skeleton
        assert "## Unreleased\n\n### Features\n\n### Changes\n\n### Fixes\n" in promoted

        # Historical v1.0.0 is untouched
        assert "## v1.0.0\n\nInitial release." in promoted

        # Dated section appears BETWEEN Unreleased and v1.0.0
        idx_unreleased = promoted.index("## Unreleased")
        idx_dated = promoted.index("## [2026.4.17]")
        idx_v1 = promoted.index("## v1.0.0")
        assert idx_unreleased < idx_dated < idx_v1

    def test_empty_unreleased_produces_empty_dated_section(self) -> None:
        """When operator has nothing queued, still record the release —
        calver bumps are routine, and an empty section preserves the
        chronology. The section has the skeleton so editors can fill
        it in later if a belated note comes in."""
        empty_body = "\n\n### Features\n\n### Changes\n\n### Fixes\n\n"
        promoted = promote_changelog(self._changelog(empty_body), "2026.4.17.2", "2026-04-17")

        assert "## [2026.4.17.2] - 2026-04-17" in promoted
        # Unreleased is back to clean skeleton
        assert promoted.count("## Unreleased") == 1

    def test_whitespace_only_unreleased_is_treated_as_empty(self) -> None:
        promoted = promote_changelog(self._changelog("   \n   \n"), "2026.4.17", "2026-04-17")
        # Still promoted — empty dated section
        assert "## [2026.4.17] - 2026-04-17" in promoted

    def test_missing_unreleased_section_raises(self) -> None:
        """Loud failure beats silent data loss when the CHANGELOG has
        been hand-edited into a shape the script doesn't recognise."""
        bad = "# Changelog\n\n## v1.0.0\n\nInitial.\n"
        with pytest.raises(ValueError, match="Unreleased"):
            promote_changelog(bad, "2026.4.17", "2026-04-17")

    def test_preserves_horizontal_rule_separator(self) -> None:
        body = "\n### Features\n- a\n"
        promoted = promote_changelog(self._changelog(body), "2026.4.17", "2026-04-17")
        # The `---` separator between Unreleased / dated releases and
        # historical v1.0.0 block must survive.
        assert "\n---\n" in promoted

    def test_idempotent_on_empty_subsequent_run(self) -> None:
        """Running the script twice in a row (second run has already-
        empty Unreleased) should still produce a valid CHANGELOG without
        stacking duplicate dated headers."""
        empty_body = "\n\n### Features\n\n### Changes\n\n### Fixes\n\n"
        first = promote_changelog(self._changelog(empty_body), "2026.4.17", "2026-04-17")
        second = promote_changelog(first, "2026.4.17.2", "2026-04-17")
        assert second.count("## [2026.4.17] - 2026-04-17") == 1
        assert second.count("## [2026.4.17.2] - 2026-04-17") == 1


class TestUpdateVersionFiles:
    def test_updates_pyproject_and_init(self, tmp_path: Path) -> None:
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text('[project]\nname = "swarm-ai"\nversion = "2026.4.16.4"\nother = "x"\n')
        init_dir = tmp_path / "src" / "swarm"
        init_dir.mkdir(parents=True)
        init_file = init_dir / "__init__.py"
        init_file.write_text('"""Swarm."""\n\n__version__ = "2026.4.16.4"\n')

        update_version_files(tmp_path, "2026.4.17")

        assert 'version = "2026.4.17"' in pyproject.read_text()
        assert '__version__ = "2026.4.17"' in init_file.read_text()
        # Other lines unchanged
        assert 'other = "x"' in pyproject.read_text()

    def test_handles_single_quotes_in_pyproject(self, tmp_path: Path) -> None:
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text("[project]\nversion = '2026.4.16.4'\n")
        init_dir = tmp_path / "src" / "swarm"
        init_dir.mkdir(parents=True)
        (init_dir / "__init__.py").write_text("__version__ = '2026.4.16.4'\n")

        update_version_files(tmp_path, "2026.4.17")
        assert "2026.4.17" in pyproject.read_text()

    def test_only_replaces_top_level_version(self, tmp_path: Path) -> None:
        """Don't touch dependency version pins that happen to contain
        the word 'version'."""
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            "[project]\n"
            'version = "2026.4.16.4"\n'
            'requires-python = ">=3.12"\n'
            "[dependencies]\n"
            'croniter = ">=2.0,<3"  # croniter version policy\n'
        )
        init_dir = tmp_path / "src" / "swarm"
        init_dir.mkdir(parents=True)
        (init_dir / "__init__.py").write_text("__version__ = '2026.4.16.4'\n")

        update_version_files(tmp_path, "2026.4.17")

        text = pyproject.read_text()
        assert text.count('version = "2026.4.17"') == 1
        assert 'croniter = ">=2.0,<3"' in text  # dep pin preserved

    def test_errors_clearly_when_pyproject_missing(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            update_version_files(tmp_path, "2026.4.17")
