"""Tests for worker/usage.py — JSONL session reader and cost estimation."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from swarm.worker.usage import (
    estimate_cost,
    find_active_session,
    project_dir,
    read_session_usage,
)
from swarm.worker.worker import TokenUsage


class TestProjectDir:
    def test_encodes_slashes(self):
        result = project_dir("/home/user/projects/myapp")
        assert result.name == "-home-user-projects-myapp"
        assert result.parent.name == "projects"

    def test_root_path(self):
        result = project_dir("/")
        assert result.name == "-"


class TestEstimateCost:
    def test_zero_tokens(self):
        assert estimate_cost(TokenUsage()) == 0.0

    def test_nonzero(self):
        u = TokenUsage(input_tokens=1_000_000, output_tokens=1_000_000)
        cost = estimate_cost(u)
        # $3/M input + $15/M output = $18
        assert cost == pytest.approx(18.0)

    def test_cache_pricing(self):
        u = TokenUsage(cache_read_tokens=1_000_000, cache_creation_tokens=1_000_000)
        cost = estimate_cost(u)
        # $0.30/M cache read + $3.75/M cache create = $4.05
        assert cost == pytest.approx(4.05)


class TestFindActiveSession:
    def test_no_dir(self, tmp_path: Path):
        result = find_active_session(tmp_path / "nonexistent", 0.0)
        assert result is None

    def test_no_files(self, tmp_path: Path):
        result = find_active_session(tmp_path, 0.0)
        assert result is None

    def test_finds_recent(self, tmp_path: Path):
        old = tmp_path / "old.jsonl"
        old.write_text("{}")
        # Make old file look old
        import os

        os.utime(old, (0, 0))

        new = tmp_path / "new.jsonl"
        new.write_text("{}")

        result = find_active_session(tmp_path, time.time() - 10)
        assert result == new

    def test_skips_old(self, tmp_path: Path):
        old = tmp_path / "old.jsonl"
        old.write_text("{}")
        import os

        os.utime(old, (0, 0))

        result = find_active_session(tmp_path, time.time() - 10)
        assert result is None


class TestReadSessionUsage:
    def test_empty_file(self, tmp_path: Path):
        f = tmp_path / "session.jsonl"
        f.write_text("")
        result = read_session_usage(f)
        assert result.total_tokens == 0

    def test_sums_assistant_messages(self, tmp_path: Path):
        lines = [
            json.dumps(
                {
                    "type": "assistant",
                    "message": {
                        "usage": {
                            "input_tokens": 100,
                            "output_tokens": 50,
                            "cache_read_input_tokens": 200,
                            "cache_creation_input_tokens": 300,
                        }
                    },
                }
            ),
            json.dumps({"type": "user", "message": {"content": "hello"}}),
            json.dumps(
                {
                    "type": "assistant",
                    "message": {
                        "usage": {
                            "input_tokens": 80,
                            "output_tokens": 30,
                        }
                    },
                }
            ),
        ]
        f = tmp_path / "session.jsonl"
        f.write_text("\n".join(lines))

        result = read_session_usage(f)
        assert result.input_tokens == 180
        assert result.output_tokens == 80
        assert result.cache_read_tokens == 200
        assert result.cache_creation_tokens == 300
        assert result.cost_usd > 0  # estimated

    def test_skips_malformed_lines(self, tmp_path: Path):
        lines = [
            "not json at all",
            json.dumps({"type": "assistant", "message": "not a dict"}),
            json.dumps(
                {
                    "type": "assistant",
                    "message": {"usage": {"input_tokens": 10, "output_tokens": 5}},
                }
            ),
        ]
        f = tmp_path / "session.jsonl"
        f.write_text("\n".join(lines))

        result = read_session_usage(f)
        assert result.input_tokens == 10
        assert result.output_tokens == 5

    def test_nonexistent_file(self, tmp_path: Path):
        result = read_session_usage(tmp_path / "nope.jsonl")
        assert result.total_tokens == 0
