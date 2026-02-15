"""Tests for config.py — parse, validate, and defaults."""

from pathlib import Path

import yaml

from swarm.config import (
    DroneApprovalRule,
    DroneConfig,
    GroupConfig,
    HiveConfig,
    NotifyConfig,
    QueenConfig,
    ToolButtonConfig,
    WorkerConfig,
    _parse_config,
    save_config,
    serialize_config,
)
from swarm.testing.config import TestConfig


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
        assert cfg.panes_per_window == 9
        assert cfg.watch_interval == 5
        assert cfg.workers == []

    def test_drones_section_parsed(self, tmp_path):
        data = {
            "drones": {
                "escalation_threshold": 60.0,
                "poll_interval": 10.0,
                "auto_approve_yn": True,
                "max_revive_attempts": 5,
            }
        }
        path = _write_yaml(tmp_path, data)
        cfg = _parse_config(path)
        assert cfg.drones.escalation_threshold == 60.0
        assert cfg.drones.poll_interval == 10.0
        assert cfg.drones.auto_approve_yn is True
        assert cfg.drones.max_revive_attempts == 5

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

    def test_drones_defaults_when_missing(self, tmp_path):
        path = _write_yaml(tmp_path, {})
        cfg = _parse_config(path)
        assert cfg.drones.escalation_threshold == 15.0
        assert cfg.drones.poll_interval == 5.0
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
            drones=DroneConfig(
                escalation_threshold=60.0,
                poll_interval=10.0,
                auto_approve_yn=True,
                max_revive_attempts=5,
                max_poll_failures=8,
                max_idle_interval=45.0,
                auto_stop_on_complete=False,
                allowed_read_paths=["~/.swarm/uploads/", "/tmp/shared/"],
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
        assert loaded.drones.escalation_threshold == 60.0
        assert loaded.drones.poll_interval == 10.0
        assert loaded.drones.auto_approve_yn is True
        assert loaded.drones.max_revive_attempts == 5
        assert loaded.drones.max_poll_failures == 8
        assert loaded.drones.max_idle_interval == 45.0
        assert loaded.drones.auto_stop_on_complete is False
        assert loaded.drones.allowed_read_paths == ["~/.swarm/uploads/", "/tmp/shared/"]
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

    def test_parse_workflows_section(self, tmp_path):
        """Workflows section maps task types to skill commands."""
        cfg_file = tmp_path / "swarm.yaml"
        cfg_file.write_text(
            "workers:\n"
            "  - name: api\n"
            "    path: /tmp/api\n"
            "workflows:\n"
            "  bug: /my-fix\n"
            "  feature: /my-feature\n"
            "  chore: /my-chore\n"
        )
        cfg = _parse_config(cfg_file)
        assert cfg.workflows == {"bug": "/my-fix", "feature": "/my-feature", "chore": "/my-chore"}

    def test_workflows_roundtrip(self, tmp_path):
        """Workflows survive serialize → save → load."""
        cfg = HiveConfig(
            session_name="wf-test",
            workers=[WorkerConfig("a", "/tmp/a")],
            workflows={"bug": "/custom-fix", "feature": "/custom-feat"},
        )
        out = tmp_path / "swarm.yaml"
        save_config(cfg, str(out))
        loaded = _parse_config(out)
        assert loaded.workflows == {"bug": "/custom-fix", "feature": "/custom-feat"}

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


class TestWorkerDescription:
    def test_parse_description(self, tmp_path):
        data = {
            "workers": [
                {"name": "api", "path": "/tmp/api", "description": "Main API worker"},
                {"name": "web", "path": "/tmp/web"},
            ]
        }
        path = _write_yaml(tmp_path, data)
        cfg = _parse_config(path)
        assert cfg.workers[0].description == "Main API worker"
        assert cfg.workers[1].description == ""

    def test_description_default(self):
        w = WorkerConfig("api", "/tmp")
        assert w.description == ""

    def test_serialize_omits_empty_description(self):
        cfg = HiveConfig(workers=[WorkerConfig("api", "/tmp")])
        data = serialize_config(cfg)
        assert "description" not in data["workers"][0]

    def test_serialize_includes_description(self):
        cfg = HiveConfig(workers=[WorkerConfig("api", "/tmp", description="Main worker")])
        data = serialize_config(cfg)
        assert data["workers"][0]["description"] == "Main worker"

    def test_roundtrip_description(self, tmp_path):
        cfg = HiveConfig(
            workers=[
                WorkerConfig("api", "/tmp/api", description="Main API worker"),
                WorkerConfig("web", "/tmp/web"),
            ],
        )
        out = tmp_path / "swarm.yaml"
        save_config(cfg, str(out))
        loaded = _parse_config(out)
        assert loaded.workers[0].description == "Main API worker"
        assert loaded.workers[1].description == ""


class TestQueenSystemPrompt:
    def test_parse_system_prompt(self, tmp_path):
        data = {
            "queen": {
                "cooldown": 30,
                "enabled": True,
                "system_prompt": "Always prefer nexus workers.",
            }
        }
        path = _write_yaml(tmp_path, data)
        cfg = _parse_config(path)
        assert cfg.queen.system_prompt == "Always prefer nexus workers."

    def test_system_prompt_default(self):
        q = QueenConfig()
        assert q.system_prompt == ""

    def test_serialize_omits_empty_system_prompt(self):
        cfg = HiveConfig()
        data = serialize_config(cfg)
        assert "system_prompt" not in data["queen"]

    def test_serialize_includes_system_prompt(self):
        cfg = HiveConfig(queen=QueenConfig(system_prompt="Prefer nexus workers."))
        data = serialize_config(cfg)
        assert data["queen"]["system_prompt"] == "Prefer nexus workers."

    def test_roundtrip_system_prompt(self, tmp_path):
        cfg = HiveConfig(
            queen=QueenConfig(system_prompt="All workers share the same repo."),
        )
        out = tmp_path / "swarm.yaml"
        save_config(cfg, str(out))
        loaded = _parse_config(out)
        assert loaded.queen.system_prompt == "All workers share the same repo."


class TestApprovalRules:
    def test_parse_approval_rules(self, tmp_path):
        data = {
            "drones": {
                "approval_rules": [
                    {"pattern": "^(Yes|Allow)", "action": "approve"},
                    {"pattern": "delete|remove", "action": "escalate"},
                ]
            }
        }
        path = _write_yaml(tmp_path, data)
        cfg = _parse_config(path)
        assert len(cfg.drones.approval_rules) == 2
        assert cfg.drones.approval_rules[0].pattern == "^(Yes|Allow)"
        assert cfg.drones.approval_rules[0].action == "approve"
        assert cfg.drones.approval_rules[1].action == "escalate"

    def test_approval_rules_default_empty(self):
        cfg = DroneConfig()
        assert cfg.approval_rules == []

    def test_serialize_approval_rules(self):
        cfg = HiveConfig(
            drones=DroneConfig(
                approval_rules=[
                    DroneApprovalRule("^Allow", "approve"),
                    DroneApprovalRule("drop|delete", "escalate"),
                ]
            )
        )
        data = serialize_config(cfg)
        rules = data["drones"]["approval_rules"]
        assert len(rules) == 2
        assert rules[0]["pattern"] == "^Allow"
        assert rules[1]["action"] == "escalate"

    def test_roundtrip_approval_rules(self, tmp_path):
        cfg = HiveConfig(
            drones=DroneConfig(
                approval_rules=[
                    DroneApprovalRule("^Yes", "approve"),
                    DroneApprovalRule("delete", "escalate"),
                ]
            )
        )
        out = tmp_path / "swarm.yaml"
        save_config(cfg, str(out))
        loaded = _parse_config(out)
        assert len(loaded.drones.approval_rules) == 2
        assert loaded.drones.approval_rules[0].pattern == "^Yes"
        assert loaded.drones.approval_rules[1].action == "escalate"

    def test_invalid_regex_validation(self):
        cfg = HiveConfig(
            drones=DroneConfig(approval_rules=[DroneApprovalRule("[invalid", "approve")])
        )
        errors = cfg.validate()
        assert any("invalid regex" in e for e in errors)

    def test_invalid_action_validation(self):
        cfg = HiveConfig(drones=DroneConfig(approval_rules=[DroneApprovalRule(".*", "deny")]))
        errors = cfg.validate()
        assert any("action must be" in e for e in errors)


class TestDefaultGroup:
    def test_parse_default_group(self, tmp_path):
        data = {
            "workers": [{"name": "api", "path": "/tmp/api"}],
            "groups": [{"name": "team", "workers": ["api"]}],
            "default_group": "team",
        }
        path = _write_yaml(tmp_path, data)
        cfg = _parse_config(path)
        assert cfg.default_group == "team"

    def test_default_group_default_empty(self, tmp_path):
        path = _write_yaml(tmp_path, {})
        cfg = _parse_config(path)
        assert cfg.default_group == ""

    def test_serialize_includes_default_group(self):
        cfg = HiveConfig(
            groups=[GroupConfig("team", ["api"])],
            default_group="team",
        )
        data = serialize_config(cfg)
        assert data["default_group"] == "team"

    def test_serialize_omits_empty_default_group(self):
        cfg = HiveConfig()
        data = serialize_config(cfg)
        assert "default_group" not in data

    def test_validate_default_group_exists(self, tmp_path):
        (tmp_path / "api").mkdir()
        cfg = HiveConfig(
            workers=[WorkerConfig("api", str(tmp_path / "api"))],
            groups=[GroupConfig("team", ["api"])],
            default_group="team",
        )
        errors = cfg.validate()
        assert not any("default_group" in e for e in errors)

    def test_validate_default_group_missing(self):
        cfg = HiveConfig(
            groups=[GroupConfig("team", [])],
            default_group="nonexistent",
        )
        errors = cfg.validate()
        assert any("default_group" in e for e in errors)

    def test_roundtrip_default_group(self, tmp_path):
        cfg = HiveConfig(
            groups=[GroupConfig("team", [])],
            default_group="team",
        )
        out = tmp_path / "swarm.yaml"
        save_config(cfg, str(out))
        loaded = _parse_config(out)
        assert loaded.default_group == "team"


class TestMinConfidence:
    def test_parse_min_confidence(self, tmp_path):
        data = {"queen": {"min_confidence": 0.5}}
        path = _write_yaml(tmp_path, data)
        cfg = _parse_config(path)
        assert cfg.queen.min_confidence == 0.5

    def test_min_confidence_default(self):
        cfg = QueenConfig()
        assert cfg.min_confidence == 0.7

    def test_serialize_min_confidence(self):
        cfg = HiveConfig(queen=QueenConfig(min_confidence=0.9))
        data = serialize_config(cfg)
        assert data["queen"]["min_confidence"] == 0.9

    def test_roundtrip_min_confidence(self, tmp_path):
        cfg = HiveConfig(queen=QueenConfig(min_confidence=0.3))
        out = tmp_path / "swarm.yaml"
        save_config(cfg, str(out))
        loaded = _parse_config(out)
        assert loaded.queen.min_confidence == 0.3

    def test_invalid_min_confidence_validation(self):
        cfg = HiveConfig(queen=QueenConfig(min_confidence=1.5))
        errors = cfg.validate()
        assert any("min_confidence" in e for e in errors)

    def test_min_confidence_boundary_valid(self):
        cfg = HiveConfig(queen=QueenConfig(min_confidence=0.0))
        errors = cfg.validate()
        assert not any("min_confidence" in e for e in errors)

        cfg = HiveConfig(queen=QueenConfig(min_confidence=1.0))
        errors = cfg.validate()
        assert not any("min_confidence" in e for e in errors)


class TestToolButtons:
    def test_parse_tool_buttons(self, tmp_path):
        data = {
            "tool_buttons": [
                {"label": "Check", "command": "/check"},
                {"label": "Tests", "command": "run tests"},
            ]
        }
        path = _write_yaml(tmp_path, data)
        cfg = _parse_config(path)
        assert len(cfg.tool_buttons) == 2
        assert cfg.tool_buttons[0].label == "Check"
        assert cfg.tool_buttons[0].command == "/check"
        assert cfg.tool_buttons[1].label == "Tests"

    def test_tool_buttons_default_empty(self):
        cfg = HiveConfig()
        assert cfg.tool_buttons == []

    def test_parse_skips_invalid_entries(self, tmp_path):
        data = {
            "tool_buttons": [
                {"label": "Valid", "command": "/ok"},
                {"label": "", "command": "/no-label"},
                {"label": "Continue"},
                "not a dict",
            ]
        }
        path = _write_yaml(tmp_path, data)
        cfg = _parse_config(path)
        assert len(cfg.tool_buttons) == 2
        assert cfg.tool_buttons[0].label == "Valid"
        assert cfg.tool_buttons[0].command == "/ok"
        assert cfg.tool_buttons[1].label == "Continue"
        assert cfg.tool_buttons[1].command == ""

    def test_serialize_tool_buttons(self):
        cfg = HiveConfig(
            tool_buttons=[
                ToolButtonConfig("Check", "/check"),
                ToolButtonConfig("Deploy", "/deploy"),
            ]
        )
        data = serialize_config(cfg)
        assert len(data["tool_buttons"]) == 2
        assert data["tool_buttons"][0] == {"label": "Check", "command": "/check"}

    def test_serialize_omits_empty_tool_buttons(self):
        cfg = HiveConfig()
        data = serialize_config(cfg)
        assert "tool_buttons" not in data

    def test_roundtrip_tool_buttons(self, tmp_path):
        cfg = HiveConfig(
            tool_buttons=[
                ToolButtonConfig("Check", "/check"),
                ToolButtonConfig("Tests", "run tests"),
            ]
        )
        out = tmp_path / "swarm.yaml"
        save_config(cfg, str(out))
        loaded = _parse_config(out)
        assert len(loaded.tool_buttons) == 2
        assert loaded.tool_buttons[0].label == "Check"
        assert loaded.tool_buttons[0].command == "/check"
        assert loaded.tool_buttons[1].label == "Tests"
        assert loaded.tool_buttons[1].command == "run tests"


class TestAutoCompleteMinIdleConfig:
    """auto_complete_min_idle in DroneConfig and TestConfig."""

    def test_drone_config_default(self):
        assert DroneConfig().auto_complete_min_idle == 45.0

    def test_drone_config_custom(self):
        cfg = DroneConfig(auto_complete_min_idle=20.0)
        assert cfg.auto_complete_min_idle == 20.0

    def test_test_config_default(self):
        assert TestConfig().auto_complete_min_idle == 10.0

    def test_parse_drone_auto_complete_min_idle(self, tmp_path):
        data = {
            "workers": [{"name": "api", "path": str(tmp_path)}],
            "drones": {"auto_complete_min_idle": 30.0},
        }
        cfg = _parse_config(_write_yaml(tmp_path, data))
        assert cfg.drones.auto_complete_min_idle == 30.0

    def test_parse_test_auto_complete_min_idle(self, tmp_path):
        data = {
            "workers": [{"name": "api", "path": str(tmp_path)}],
            "test": {"auto_complete_min_idle": 5.0},
        }
        cfg = _parse_config(_write_yaml(tmp_path, data))
        assert cfg.test.auto_complete_min_idle == 5.0

    def test_parse_defaults_when_missing(self, tmp_path):
        data = {"workers": [{"name": "api", "path": str(tmp_path)}]}
        cfg = _parse_config(_write_yaml(tmp_path, data))
        assert cfg.drones.auto_complete_min_idle == 45.0
        assert cfg.test.auto_complete_min_idle == 10.0
