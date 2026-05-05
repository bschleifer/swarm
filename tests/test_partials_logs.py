"""Tests for /partials/logs severity-filter logic.

Regression for the dashboard's Logs tab: the level filter used to do a
naive substring match (``level_filter in ln``), which made selecting
"INFO" hide every WARNING/ERROR line.  The fix is an inclusive
hierarchy — picking a level shows that level and above, mirroring how
Python's logging module treats threshold severities.
"""

from __future__ import annotations

import pytest

from swarm.web.log_filter import LOG_LEVEL_INCLUSIVE, line_matches_level


@pytest.fixture(scope="module")
def helpers():
    return {"levels": LOG_LEVEL_INCLUSIVE, "matches": line_matches_level}


def _line(level: str, msg: str = "x") -> str:
    """Mimic the swarm.log line shape: ``YYYY-MM-DD HH:MM:SS [LEVEL] logger: msg``."""
    return f"2026-05-04 12:00:00 [{level}] swarm.example: {msg}"


class TestSeverityHierarchy:
    def test_info_includes_warning_and_error(self, helpers) -> None:
        allowed = helpers["levels"]["INFO"]
        assert helpers["matches"](_line("INFO"), allowed)
        assert helpers["matches"](_line("WARNING"), allowed)
        assert helpers["matches"](_line("ERROR"), allowed)
        assert not helpers["matches"](_line("DEBUG"), allowed)

    def test_warning_excludes_info_and_debug(self, helpers) -> None:
        allowed = helpers["levels"]["WARNING"]
        assert helpers["matches"](_line("WARNING"), allowed)
        assert helpers["matches"](_line("ERROR"), allowed)
        assert not helpers["matches"](_line("INFO"), allowed)
        assert not helpers["matches"](_line("DEBUG"), allowed)

    def test_debug_includes_everything(self, helpers) -> None:
        allowed = helpers["levels"]["DEBUG"]
        for lvl in ("DEBUG", "INFO", "WARNING", "ERROR"):
            assert helpers["matches"](_line(lvl), allowed), f"{lvl} should match DEBUG-and-up"

    def test_error_only(self, helpers) -> None:
        allowed = helpers["levels"]["ERROR"]
        assert helpers["matches"](_line("ERROR"), allowed)
        for lvl in ("DEBUG", "INFO", "WARNING"):
            assert not helpers["matches"](_line(lvl), allowed)

    def test_payload_string_does_not_false_match(self, helpers) -> None:
        """The bracketed token check prevents a payload like 'Got INFO from
        upstream' from matching when the line's own level is DEBUG."""
        allowed = helpers["levels"]["INFO"]
        line = _line("DEBUG", "Got INFO from upstream")
        assert not helpers["matches"](line, allowed), (
            "Substring match would falsely include this DEBUG line — pre-fix"
            " bug we explicitly guard against with the [LEVEL] bracket check."
        )
