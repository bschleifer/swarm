"""Tests for config.py — parse, validate, and defaults."""

from pathlib import Path

import yaml

from swarm.config import (
    DEFAULT_ACTION_BUTTONS,
    DEFAULT_TASK_BUTTONS,
    ActionButtonConfig,
    DroneApprovalRule,
    DroneConfig,
    GroupConfig,
    HiveConfig,
    NotifyConfig,
    QueenConfig,
    TaskButtonConfig,
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
        assert cfg.drones.escalation_threshold == 120.0
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

    def test_serialize_always_includes_test_section(self):
        """serialize_config must always include 'test' even when all defaults.

        The config.html template unconditionally accesses config.test.port,
        so omitting the test section causes a 500 error on /config.
        """
        cfg = HiveConfig()  # all defaults
        data = serialize_config(cfg)
        assert "test" in data
        assert data["test"]["port"] == 9091
        assert data["test"]["auto_resolve_delay"] == 4.0
        assert data["test"]["report_dir"] == "~/.swarm/reports"
        assert data["test"]["auto_complete_min_idle"] == 10.0

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


class TestWriteConfig:
    """Tests for write_config (used by swarm init)."""

    def test_write_config_includes_api_password(self, tmp_path):
        """write_config should include api_password when provided."""
        out = tmp_path / "swarm.yaml"
        from swarm.config import write_config

        write_config(
            str(out),
            workers=[("api", "/tmp/api")],
            groups={"all": ["api"]},
            projects_dir="/tmp",
            api_password="mySecret",
        )
        data = yaml.safe_load(out.read_text())
        assert data["api_password"] == "mySecret"

    def test_write_config_omits_api_password_when_none(self, tmp_path):
        """write_config should not include api_password when not provided."""
        out = tmp_path / "swarm.yaml"
        from swarm.config import write_config

        write_config(
            str(out),
            workers=[("api", "/tmp/api")],
            groups={"all": ["api"]},
            projects_dir="/tmp",
        )
        data = yaml.safe_load(out.read_text())
        assert "api_password" not in data


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


class TestActionButtons:
    def test_defaults(self):
        """ActionButtonConfig has sensible defaults."""
        btn = ActionButtonConfig(label="Test")
        assert btn.action == ""
        assert btn.command == ""
        assert btn.style == "secondary"
        assert btn.show_mobile is True
        assert btn.show_desktop is True

    def test_default_action_buttons_constant(self):
        """DEFAULT_ACTION_BUTTONS has the 4 built-in buttons."""
        assert len(DEFAULT_ACTION_BUTTONS) == 4
        labels = [b.label for b in DEFAULT_ACTION_BUTTONS]
        assert labels == ["Revive", "Refresh", "Ask Queen", "Kill"]

    def test_no_config_gets_defaults(self, tmp_path):
        """When no action_buttons or tool_buttons in YAML, defaults are used."""
        path = _write_yaml(tmp_path, {})
        cfg = _parse_config(path)
        assert len(cfg.action_buttons) == 4
        assert cfg.action_buttons[0].label == "Revive"
        assert cfg.action_buttons[0].action == "revive"

    def test_backward_compat_tool_buttons_merge(self, tmp_path):
        """Old tool_buttons config merges with defaults when no action_buttons."""
        data = {
            "tool_buttons": [
                {"label": "Deploy", "command": "/deploy"},
                {"label": "Continue"},
            ]
        }
        path = _write_yaml(tmp_path, data)
        cfg = _parse_config(path)
        # 4 defaults + 2 tool_buttons
        assert len(cfg.action_buttons) == 6
        assert cfg.action_buttons[0].label == "Revive"
        assert cfg.action_buttons[4].label == "Deploy"
        assert cfg.action_buttons[4].command == "/deploy"
        assert cfg.action_buttons[4].action == ""
        assert cfg.action_buttons[5].label == "Continue"
        assert cfg.action_buttons[5].command == ""

    def test_explicit_action_buttons_ignores_tool_buttons(self, tmp_path):
        """When action_buttons key exists, tool_buttons are ignored for action_buttons."""
        data = {
            "tool_buttons": [{"label": "Old", "command": "/old"}],
            "action_buttons": [
                {"label": "Custom", "action": "", "command": "/custom", "style": "danger"},
            ],
        }
        path = _write_yaml(tmp_path, data)
        cfg = _parse_config(path)
        assert len(cfg.action_buttons) == 1
        assert cfg.action_buttons[0].label == "Custom"
        assert cfg.action_buttons[0].style == "danger"
        # tool_buttons still parsed separately
        assert len(cfg.tool_buttons) == 1

    def test_parse_all_fields(self, tmp_path):
        """All ActionButtonConfig fields are parsed from YAML."""
        data = {
            "action_buttons": [
                {
                    "label": "Test",
                    "action": "revive",
                    "command": "",
                    "style": "queen",
                    "show_mobile": False,
                    "show_desktop": True,
                },
            ]
        }
        path = _write_yaml(tmp_path, data)
        cfg = _parse_config(path)
        btn = cfg.action_buttons[0]
        assert btn.label == "Test"
        assert btn.action == "revive"
        assert btn.style == "queen"
        assert btn.show_mobile is False
        assert btn.show_desktop is True

    def test_serialize_action_buttons(self):
        """action_buttons are serialized with all fields."""
        cfg = HiveConfig(
            action_buttons=[
                ActionButtonConfig("Kill", action="kill", style="danger", show_mobile=False),
            ]
        )
        data = serialize_config(cfg)
        assert len(data["action_buttons"]) == 1
        ab = data["action_buttons"][0]
        assert ab["label"] == "Kill"
        assert ab["action"] == "kill"
        assert ab["style"] == "danger"
        assert ab["show_mobile"] is False
        assert ab["show_desktop"] is True

    def test_serialize_omits_empty_action_buttons(self):
        """Empty action_buttons list is not serialized."""
        cfg = HiveConfig()
        data = serialize_config(cfg)
        assert "action_buttons" not in data

    def test_roundtrip(self, tmp_path):
        """action_buttons survive serialize → save → load."""
        cfg = HiveConfig(
            action_buttons=[
                ActionButtonConfig("Revive", action="revive", style="secondary"),
                ActionButtonConfig("Deploy", command="/deploy", show_mobile=False),
            ]
        )
        out = tmp_path / "swarm.yaml"
        save_config(cfg, str(out))
        loaded = _parse_config(out)
        assert len(loaded.action_buttons) == 2
        assert loaded.action_buttons[0].label == "Revive"
        assert loaded.action_buttons[0].action == "revive"
        assert loaded.action_buttons[1].label == "Deploy"
        assert loaded.action_buttons[1].command == "/deploy"
        assert loaded.action_buttons[1].show_mobile is False


class TestTaskButtons:
    def test_defaults(self):
        """TaskButtonConfig has sensible defaults."""
        btn = TaskButtonConfig(label="Test", action="edit")
        assert btn.show_mobile is True
        assert btn.show_desktop is True

    def test_default_task_buttons_constant(self):
        """DEFAULT_TASK_BUTTONS has the 9 built-in buttons."""
        assert len(DEFAULT_TASK_BUTTONS) == 9
        actions = [b.action for b in DEFAULT_TASK_BUTTONS]
        assert actions == [
            "edit",
            "assign",
            "done",
            "unassign",
            "fail",
            "reopen",
            "log",
            "retry_draft",
            "remove",
        ]

    def test_no_config_gets_defaults(self, tmp_path):
        """When no task_buttons in YAML, defaults are used."""
        path = _write_yaml(tmp_path, {})
        cfg = _parse_config(path)
        assert len(cfg.task_buttons) == 9
        assert cfg.task_buttons[0].label == "Edit"
        assert cfg.task_buttons[0].action == "edit"
        assert cfg.task_buttons[-1].action == "remove"

    def test_parse_all_fields(self, tmp_path):
        """All TaskButtonConfig fields are parsed from YAML."""
        data = {
            "task_buttons": [
                {
                    "label": "Complete",
                    "action": "done",
                    "show_mobile": False,
                    "show_desktop": True,
                },
            ]
        }
        path = _write_yaml(tmp_path, data)
        cfg = _parse_config(path)
        assert len(cfg.task_buttons) == 1
        btn = cfg.task_buttons[0]
        assert btn.label == "Complete"
        assert btn.action == "done"
        assert btn.show_mobile is False
        assert btn.show_desktop is True

    def test_serialize_task_buttons(self):
        """task_buttons are serialized with all fields."""
        cfg = HiveConfig(
            task_buttons=[
                TaskButtonConfig("Edit", action="edit", show_mobile=False),
            ]
        )
        data = serialize_config(cfg)
        assert len(data["task_buttons"]) == 1
        tb = data["task_buttons"][0]
        assert tb["label"] == "Edit"
        assert tb["action"] == "edit"
        assert tb["show_mobile"] is False
        assert tb["show_desktop"] is True

    def test_serialize_omits_empty_task_buttons(self):
        """Empty task_buttons list is not serialized."""
        cfg = HiveConfig()
        data = serialize_config(cfg)
        assert "task_buttons" not in data

    def test_roundtrip(self, tmp_path):
        """task_buttons survive serialize -> save -> load."""
        cfg = HiveConfig(
            task_buttons=[
                TaskButtonConfig("Edit", action="edit"),
                TaskButtonConfig("Log", action="log", show_mobile=False),
                TaskButtonConfig("X", action="remove", show_desktop=False),
            ]
        )
        out = tmp_path / "swarm.yaml"
        save_config(cfg, str(out))
        loaded = _parse_config(out)
        assert len(loaded.task_buttons) == 3
        assert loaded.task_buttons[0].label == "Edit"
        assert loaded.task_buttons[0].action == "edit"
        assert loaded.task_buttons[1].label == "Log"
        assert loaded.task_buttons[1].show_mobile is False
        assert loaded.task_buttons[2].label == "X"
        assert loaded.task_buttons[2].action == "remove"
        assert loaded.task_buttons[2].show_desktop is False

    def test_parse_skips_invalid_entries(self, tmp_path):
        """Entries missing label or action are skipped."""
        data = {
            "task_buttons": [
                {"label": "Valid", "action": "edit"},
                {"label": "", "action": "log"},
                {"label": "NoAction"},
                "not a dict",
            ]
        }
        path = _write_yaml(tmp_path, data)
        cfg = _parse_config(path)
        assert len(cfg.task_buttons) == 1
        assert cfg.task_buttons[0].label == "Valid"


def test_invalid_port_falls_back_to_default(tmp_path):
    """Non-numeric port in test config should fall back to 9091."""
    data = {"workers": [{"name": "w", "path": "/tmp"}], "test": {"port": "not-a-number"}}
    path = _write_yaml(tmp_path, data)
    cfg = _parse_config(path)
    assert cfg.test.port == 9091


class TestEnvOverrides:
    def test_session_name(self, monkeypatch):
        monkeypatch.setenv("SWARM_SESSION_NAME", "my-session")
        cfg = HiveConfig()
        cfg.apply_env_overrides()
        assert cfg.session_name == "my-session"

    def test_watch_interval_valid(self, monkeypatch):
        monkeypatch.setenv("SWARM_WATCH_INTERVAL", "10")
        cfg = HiveConfig()
        cfg.apply_env_overrides()
        assert cfg.watch_interval == 10

    def test_watch_interval_invalid_ignored(self, monkeypatch):
        monkeypatch.setenv("SWARM_WATCH_INTERVAL", "not-a-number")
        cfg = HiveConfig()
        original = cfg.watch_interval
        cfg.apply_env_overrides()
        assert cfg.watch_interval == original

    def test_daemon_url(self, monkeypatch):
        monkeypatch.setenv("SWARM_DAEMON_URL", "http://custom:8080")
        cfg = HiveConfig()
        cfg.apply_env_overrides()
        assert cfg.daemon_url == "http://custom:8080"

    def test_api_password(self, monkeypatch):
        monkeypatch.setenv("SWARM_API_PASSWORD", "secret123")
        cfg = HiveConfig()
        cfg.apply_env_overrides()
        assert cfg.api_password == "secret123"

    def test_port_valid(self, monkeypatch):
        monkeypatch.setenv("SWARM_PORT", "8080")
        cfg = HiveConfig()
        cfg.apply_env_overrides()
        assert cfg.port == 8080

    def test_port_invalid_ignored(self, monkeypatch):
        monkeypatch.setenv("SWARM_PORT", "nope")
        cfg = HiveConfig()
        original = cfg.port
        cfg.apply_env_overrides()
        assert cfg.port == original


class TestQueenRangeValidation:
    def test_max_session_calls_zero_invalid(self):
        cfg = HiveConfig(queen=QueenConfig(max_session_calls=0))
        errors = cfg.validate()
        assert any("max_session_calls" in e for e in errors)

    def test_max_session_calls_one_valid(self):
        cfg = HiveConfig(queen=QueenConfig(max_session_calls=1))
        errors = cfg.validate()
        assert not any("max_session_calls" in e for e in errors)

    def test_max_session_age_negative_invalid(self):
        cfg = HiveConfig(queen=QueenConfig(max_session_age=-1))
        errors = cfg.validate()
        assert any("max_session_age" in e for e in errors)

    def test_max_session_age_zero_invalid(self):
        cfg = HiveConfig(queen=QueenConfig(max_session_age=0))
        errors = cfg.validate()
        assert any("max_session_age" in e for e in errors)


class TestDroneRangeValidation:
    def test_max_revive_attempts_negative_invalid(self):
        cfg = HiveConfig(drones=DroneConfig(max_revive_attempts=-1))
        errors = cfg.validate()
        assert any("max_revive_attempts" in e for e in errors)

    def test_max_poll_failures_zero_invalid(self):
        cfg = HiveConfig(drones=DroneConfig(max_poll_failures=0))
        errors = cfg.validate()
        assert any("max_poll_failures" in e for e in errors)

    def test_sleeping_poll_interval_zero_invalid(self):
        cfg = HiveConfig(drones=DroneConfig(sleeping_poll_interval=0))
        errors = cfg.validate()
        assert any("sleeping_poll_interval" in e for e in errors)

    def test_sleeping_threshold_zero_invalid(self):
        cfg = HiveConfig(drones=DroneConfig(sleeping_threshold=0))
        errors = cfg.validate()
        assert any("sleeping_threshold" in e for e in errors)

    def test_stung_reap_timeout_zero_invalid(self):
        cfg = HiveConfig(drones=DroneConfig(stung_reap_timeout=0))
        errors = cfg.validate()
        assert any("stung_reap_timeout" in e for e in errors)

    def test_idle_assign_threshold_zero_invalid(self):
        cfg = HiveConfig(drones=DroneConfig(idle_assign_threshold=0))
        errors = cfg.validate()
        assert any("idle_assign_threshold" in e for e in errors)

    def test_valid_drone_defaults(self):
        cfg = HiveConfig()
        errors = cfg.validate()
        drone_errors = [e for e in errors if e.startswith("drones.")]
        assert not drone_errors


class TestPortValidation:
    def test_port_out_of_range_high(self):
        cfg = HiveConfig(port=99999)
        errors = cfg.validate()
        assert any("port" in e for e in errors)

    def test_port_out_of_range_zero(self):
        cfg = HiveConfig(port=0)
        errors = cfg.validate()
        assert any("port" in e for e in errors)
