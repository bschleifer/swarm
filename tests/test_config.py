"""Tests for config.py — parse, validate, and defaults."""

import textwrap
from pathlib import Path

import yaml

from swarm.config import (
    BuzzConfig,
    ConfigError,
    GroupConfig,
    HiveConfig,
    NotifyConfig,
    QueenConfig,
    WorkerConfig,
    _parse_config,
    load_config,
    save_config,
    serialize_config,
)


def _write_yaml(tmp_path: Path, data: dict) -> Path:
    p = tmp_path / "swarm.yaml"
    p.write_text(yaml.dump(data, default_flow_style=False))
    return p


class TestParseConfig:
    def test_basic_parse(self, tmp_path):
        data = {
            "session_name": "test-hive",
            "workers": [
                {"name": "api", "path": "/tmp/api"},
                {"name": "web", "path": "/tmp/web"},
            ],
            "groups": [
                {"name": "all", "workers": ["api", "web"]},
            ],
        }
        path = _write_yaml(tmp_path, data)
        cfg = _parse_config(path)
        assert cfg.session_name == "test-hive"
        assert len(cfg.workers) == 2
        assert cfg.workers[0].name == "api"
        assert len(cfg.groups) == 1

    def test_defaults(self, tmp_path):
        path = _write_yaml(tmp_path, {})
        cfg = _parse_config(path)
        assert cfg.session_name == "swarm"
        assert cfg.panes_per_window == 4
        assert cfg.watch_interval == 5
        assert cfg.workers == []

    def test_buzz_section_parsed(self, tmp_path):
        data = {
            "buzz": {
                "escalation_threshold": 60.0,
                "poll_interval": 10.0,
                "auto_approve_yn": True,
                "max_revive_attempts": 5,
            }
        }
        path = _write_yaml(tmp_path, data)
        cfg = _parse_config(path)
        assert cfg.buzz.escalation_threshold == 60.0
        assert cfg.buzz.poll_interval == 10.0
        assert cfg.buzz.auto_approve_yn is True
        assert cfg.buzz.max_revive_attempts == 5

    def test_queen_section_parsed(self, tmp_path):
        data = {
            "queen": {
                "cooldown": 120.0,
                "enabled": False,
            }
        }
        path = _write_yaml(tmp_path, data)
        cfg = _parse_config(path)
        assert cfg.queen.cooldown == 120.0
        assert cfg.queen.enabled is False

    def test_buzz_defaults_when_missing(self, tmp_path):
        path = _write_yaml(tmp_path, {})
        cfg = _parse_config(path)
        assert cfg.buzz.escalation_threshold == 15.0
        assert cfg.buzz.poll_interval == 5.0
        assert cfg.queen.cooldown == 30.0

    def test_log_level_parsed(self, tmp_path):
        data = {"log_level": "DEBUG", "log_file": "/tmp/swarm.log"}
        path = _write_yaml(tmp_path, data)
        cfg = _parse_config(path)
        assert cfg.log_level == "DEBUG"
        assert cfg.log_file == "/tmp/swarm.log"


class TestValidate:
    def test_valid_config(self, tmp_path):
        # Create real directories for paths
        (tmp_path / "api").mkdir()
        (tmp_path / "web").mkdir()
        cfg = HiveConfig(
            workers=[
                WorkerConfig("api", str(tmp_path / "api")),
                WorkerConfig("web", str(tmp_path / "web")),
            ],
            groups=[GroupConfig("all", ["api", "web"])],
        )
        assert cfg.validate() == []

    def test_duplicate_worker_names(self):
        cfg = HiveConfig(
            workers=[
                WorkerConfig("api", "/tmp"),
                WorkerConfig("api", "/tmp/other"),
            ],
        )
        errors = cfg.validate()
        assert any("Duplicate worker name" in e for e in errors)

    def test_missing_worker_path(self):
        cfg = HiveConfig(
            workers=[
                WorkerConfig("ghost", "/nonexistent/path/12345"),
            ],
        )
        errors = cfg.validate()
        assert any("does not exist" in e for e in errors)

    def test_group_references_unknown_worker(self, tmp_path):
        (tmp_path / "api").mkdir()
        cfg = HiveConfig(
            workers=[WorkerConfig("api", str(tmp_path / "api"))],
            groups=[GroupConfig("team", ["api", "phantom"])],
        )
        errors = cfg.validate()
        assert any("phantom" in e for e in errors)

    def test_duplicate_group_names(self, tmp_path):
        cfg = HiveConfig(
            groups=[
                GroupConfig("all", []),
                GroupConfig("all", []),
            ],
        )
        errors = cfg.validate()
        assert any("Duplicate group name" in e for e in errors)


class TestGetGroup:
    def test_get_group_by_name(self):
        cfg = HiveConfig(
            workers=[
                WorkerConfig("api", "/tmp"),
                WorkerConfig("web", "/tmp"),
            ],
            groups=[GroupConfig("team", ["api", "web"])],
        )
        members = cfg.get_group("team")
        assert len(members) == 2
        assert members[0].name == "api"

    def test_get_group_case_insensitive(self):
        cfg = HiveConfig(
            workers=[WorkerConfig("api", "/tmp")],
            groups=[GroupConfig("Team", ["api"])],
        )
        members = cfg.get_group("team")
        assert len(members) == 1

    def test_get_group_unknown_raises(self):
        cfg = HiveConfig()
        try:
            cfg.get_group("nope")
            assert False, "Should have raised ValueError"
        except ValueError:
            pass


class TestGetWorker:
    def test_get_worker_by_name(self):
        cfg = HiveConfig(workers=[WorkerConfig("api", "/tmp")])
        w = cfg.get_worker("api")
        assert w is not None
        assert w.name == "api"

    def test_get_worker_case_insensitive(self):
        cfg = HiveConfig(workers=[WorkerConfig("API", "/tmp")])
        w = cfg.get_worker("api")
        assert w is not None

    def test_get_worker_unknown_returns_none(self):
        cfg = HiveConfig()
        assert cfg.get_worker("nope") is None


class TestSerializeConfig:
    def test_serialize_config_roundtrip(self, tmp_path):
        """Serialize → write → load → compare all fields."""
        cfg = HiveConfig(
            session_name="test-hive",
            projects_dir="/tmp/projects",
            workers=[WorkerConfig("api", "/tmp/api"), WorkerConfig("web", "/tmp/web")],
            groups=[GroupConfig("all", ["api", "web"])],
            panes_per_window=6,
            watch_interval=10,
            buzz=BuzzConfig(
                escalation_threshold=60.0,
                poll_interval=10.0,
                auto_approve_yn=True,
                max_revive_attempts=5,
                max_poll_failures=8,
                max_idle_interval=45.0,
                auto_stop_on_complete=False,
            ),
            queen=QueenConfig(cooldown=120.0, enabled=False),
            notifications=NotifyConfig(terminal_bell=False, desktop=True, debounce_seconds=10.0),
            log_level="DEBUG",
            log_file="/tmp/swarm.log",
            api_password="secret123",
        )

        out = tmp_path / "swarm.yaml"
        save_config(cfg, str(out))

        loaded = _parse_config(out)
        assert loaded.session_name == "test-hive"
        assert loaded.projects_dir == "/tmp/projects"
        assert len(loaded.workers) == 2
        assert loaded.workers[0].name == "api"
        assert loaded.workers[1].name == "web"
        assert len(loaded.groups) == 1
        assert loaded.groups[0].name == "all"
        assert loaded.panes_per_window == 6
        assert loaded.watch_interval == 10
        assert loaded.buzz.escalation_threshold == 60.0
        assert loaded.buzz.poll_interval == 10.0
        assert loaded.buzz.auto_approve_yn is True
        assert loaded.buzz.max_revive_attempts == 5
        assert loaded.buzz.max_poll_failures == 8
        assert loaded.buzz.max_idle_interval == 45.0
        assert loaded.buzz.auto_stop_on_complete is False
        assert loaded.queen.cooldown == 120.0
        assert loaded.queen.enabled is False
        assert loaded.notifications.terminal_bell is False
        assert loaded.notifications.desktop is True
        assert loaded.notifications.debounce_seconds == 10.0
        assert loaded.log_level == "DEBUG"
        assert loaded.log_file == "/tmp/swarm.log"
        assert loaded.api_password == "secret123"

    def test_serialize_omits_none(self):
        cfg = HiveConfig()
        data = serialize_config(cfg)
        assert "log_file" not in data
        assert "daemon_url" not in data
        assert "api_password" not in data

    def test_save_config_creates_file(self, tmp_path):
        cfg = HiveConfig(session_name="save-test")
        out = tmp_path / "output.yaml"
        save_config(cfg, str(out))
        assert out.exists()
        loaded = yaml.safe_load(out.read_text())
        assert loaded["session_name"] == "save-test"

    def test_save_config_defaults_to_source_path(self, tmp_path):
        out = tmp_path / "swarm.yaml"
        cfg = HiveConfig(session_name="path-test", source_path=str(out))
        save_config(cfg)
        assert out.exists()
        loaded = yaml.safe_load(out.read_text())
        assert loaded["session_name"] == "path-test"
