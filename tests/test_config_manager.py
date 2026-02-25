"""Tests for ConfigManager — hot-reload, validation, persistence, and approval rules."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml

from swarm.config import (
    DroneConfig,
    GroupConfig,
    HiveConfig,
    NotifyConfig,
    QueenConfig,
    WorkerConfig,
)
from swarm.server.config_manager import ConfigManager
from swarm.testing.config import TestConfig


def _make_daemon(
    tmp_path: Path | None = None,
    *,
    source_path: str = "",
    config: HiveConfig | None = None,
) -> MagicMock:
    """Build a minimal mock daemon with a real HiveConfig."""
    d = MagicMock()
    if config is None:
        config = HiveConfig(source_path=source_path)
    d.config = config
    d._config_mtime = 0.0
    d.broadcast_ws = MagicMock()
    d.apply_config = MagicMock()
    d.pilot = None
    d.graph_mgr = None
    d.email = MagicMock()
    d._build_graph_manager = MagicMock(return_value=None)
    return d


def _write_yaml(path: Path, data: dict) -> Path:
    path.write_text(yaml.dump(data, default_flow_style=False))
    return path


# ---------------------------------------------------------------------------
# Initialization
# ---------------------------------------------------------------------------


class TestInit:
    def test_stores_daemon_reference(self) -> None:
        daemon = _make_daemon()
        mgr = ConfigManager(daemon)
        assert mgr._daemon is daemon

    def test_hot_apply_delegates_to_daemon(self) -> None:
        daemon = _make_daemon()
        mgr = ConfigManager(daemon)
        mgr.hot_apply()
        daemon.apply_config.assert_called_once()


# ---------------------------------------------------------------------------
# check_file — on-disk mtime detection
# ---------------------------------------------------------------------------


class TestCheckFile:
    def test_returns_false_when_no_source_path(self) -> None:
        daemon = _make_daemon(source_path="")
        mgr = ConfigManager(daemon)
        assert mgr.check_file() is False

    def test_returns_false_when_file_missing(self) -> None:
        daemon = _make_daemon(source_path="/nonexistent/swarm.yaml")
        mgr = ConfigManager(daemon)
        assert mgr.check_file() is False

    def test_returns_false_when_mtime_unchanged(self, tmp_path: Path) -> None:
        cfg_file = tmp_path / "swarm.yaml"
        _write_yaml(cfg_file, {"session_name": "test"})
        mtime = cfg_file.stat().st_mtime

        daemon = _make_daemon(source_path=str(cfg_file))
        daemon._config_mtime = mtime  # already up-to-date
        mgr = ConfigManager(daemon)
        assert mgr.check_file() is False

    def test_detects_changed_mtime_and_reloads(self, tmp_path: Path) -> None:
        cfg_file = tmp_path / "swarm.yaml"
        _write_yaml(cfg_file, {"drones": {"poll_interval": 42}})

        daemon = _make_daemon(source_path=str(cfg_file))
        daemon._config_mtime = 0.0  # stale
        mgr = ConfigManager(daemon)

        result = mgr.check_file()
        assert result is True
        # mtime should be updated
        assert daemon._config_mtime == cfg_file.stat().st_mtime
        # hot_apply should have been called
        daemon.apply_config.assert_called_once()
        # Config fields should be updated from the reloaded file
        assert daemon.config.drones.poll_interval == 42

    def test_returns_false_on_invalid_yaml(self, tmp_path: Path) -> None:
        cfg_file = tmp_path / "swarm.yaml"
        cfg_file.write_text("workers: [[[invalid yaml that will fail parsing")

        daemon = _make_daemon(source_path=str(cfg_file))
        daemon._config_mtime = 0.0
        mgr = ConfigManager(daemon)

        # Should return False (reload failed) but update mtime to avoid retry loop
        result = mgr.check_file()
        assert result is False


# ---------------------------------------------------------------------------
# reload — async hot-reload
# ---------------------------------------------------------------------------


class TestReload:
    @pytest.mark.asyncio
    async def test_reload_updates_config_and_broadcasts(self, tmp_path: Path) -> None:
        cfg_file = tmp_path / "swarm.yaml"
        _write_yaml(cfg_file, {"session_name": "new"})

        new_config = HiveConfig(session_name="new", source_path=str(cfg_file))

        daemon = _make_daemon(source_path=str(cfg_file))
        mgr = ConfigManager(daemon)

        await mgr.reload(new_config)

        assert daemon.config is new_config
        daemon.apply_config.assert_called_once()
        daemon.broadcast_ws.assert_called_once_with({"type": "config_changed"})

    @pytest.mark.asyncio
    async def test_reload_updates_mtime_from_source_path(self, tmp_path: Path) -> None:
        cfg_file = tmp_path / "swarm.yaml"
        _write_yaml(cfg_file, {"session_name": "mtime-test"})
        expected_mtime = cfg_file.stat().st_mtime

        new_config = HiveConfig(source_path=str(cfg_file))
        daemon = _make_daemon(source_path=str(cfg_file))
        daemon._config_mtime = 0.0
        mgr = ConfigManager(daemon)

        await mgr.reload(new_config)

        assert daemon._config_mtime == expected_mtime

    @pytest.mark.asyncio
    async def test_reload_without_source_path_skips_mtime(self) -> None:
        new_config = HiveConfig(session_name="no-path")
        daemon = _make_daemon(source_path="")
        daemon._config_mtime = 0.0
        mgr = ConfigManager(daemon)

        await mgr.reload(new_config)

        # mtime unchanged because no source_path
        assert daemon._config_mtime == 0.0


# ---------------------------------------------------------------------------
# apply_update — partial config mutations from the API
# ---------------------------------------------------------------------------


class TestApplyUpdate:
    @pytest.mark.asyncio
    async def test_apply_drones_update(self) -> None:
        daemon = _make_daemon()
        daemon.config.drones = DroneConfig()
        mgr = ConfigManager(daemon)

        body: dict[str, Any] = {
            "drones": {
                "enabled": False,
                "poll_interval": 15.0,
                "escalation_threshold": 60.0,
            }
        }
        # Mock reload and save to avoid side effects
        mgr.reload = AsyncMock()  # type: ignore[assignment]
        mgr.save = MagicMock()  # type: ignore[assignment]

        await mgr.apply_update(body)

        assert daemon.config.drones.enabled is False
        assert daemon.config.drones.poll_interval == 15.0
        assert daemon.config.drones.escalation_threshold == 60.0

    @pytest.mark.asyncio
    async def test_apply_queen_update(self) -> None:
        daemon = _make_daemon()
        daemon.config.queen = QueenConfig()
        mgr = ConfigManager(daemon)

        body: dict[str, Any] = {
            "queen": {
                "cooldown": 120.0,
                "enabled": False,
                "system_prompt": "Custom prompt",
                "min_confidence": 0.5,
            }
        }
        mgr.reload = AsyncMock()  # type: ignore[assignment]
        mgr.save = MagicMock()  # type: ignore[assignment]

        await mgr.apply_update(body)

        assert daemon.config.queen.cooldown == 120.0
        assert daemon.config.queen.enabled is False
        assert daemon.config.queen.system_prompt == "Custom prompt"
        assert daemon.config.queen.min_confidence == 0.5

    @pytest.mark.asyncio
    async def test_apply_notifications_update(self) -> None:
        daemon = _make_daemon()
        daemon.config.notifications = NotifyConfig()
        mgr = ConfigManager(daemon)

        body: dict[str, Any] = {
            "notifications": {
                "terminal_bell": False,
                "desktop": False,
                "debounce_seconds": 15.0,
            }
        }
        mgr.reload = AsyncMock()  # type: ignore[assignment]
        mgr.save = MagicMock()  # type: ignore[assignment]

        await mgr.apply_update(body)

        assert daemon.config.notifications.terminal_bell is False
        assert daemon.config.notifications.desktop is False
        assert daemon.config.notifications.debounce_seconds == 15.0

    @pytest.mark.asyncio
    async def test_apply_update_calls_reload_and_save(self) -> None:
        daemon = _make_daemon()
        mgr = ConfigManager(daemon)
        mgr.reload = AsyncMock()  # type: ignore[assignment]
        mgr.save = MagicMock()  # type: ignore[assignment]

        await mgr.apply_update({"drones": {"enabled": True}})

        mgr.reload.assert_awaited_once()
        mgr.save.assert_called_once()

    @pytest.mark.asyncio
    async def test_apply_update_invalid_drone_type_raises(self) -> None:
        daemon = _make_daemon()
        daemon.config.drones = DroneConfig()
        mgr = ConfigManager(daemon)
        mgr.reload = AsyncMock()  # type: ignore[assignment]
        mgr.save = MagicMock()  # type: ignore[assignment]

        with pytest.raises(ValueError, match="drones.enabled must be boolean"):
            await mgr.apply_update({"drones": {"enabled": "yes"}})

    @pytest.mark.asyncio
    async def test_apply_update_negative_number_raises(self) -> None:
        daemon = _make_daemon()
        daemon.config.drones = DroneConfig()
        mgr = ConfigManager(daemon)
        mgr.reload = AsyncMock()  # type: ignore[assignment]
        mgr.save = MagicMock()  # type: ignore[assignment]

        with pytest.raises(ValueError, match="drones.poll_interval must be >= 0"):
            await mgr.apply_update({"drones": {"poll_interval": -5}})

    @pytest.mark.asyncio
    async def test_apply_queen_invalid_cooldown_raises(self) -> None:
        daemon = _make_daemon()
        daemon.config.queen = QueenConfig()
        mgr = ConfigManager(daemon)
        mgr.reload = AsyncMock()  # type: ignore[assignment]
        mgr.save = MagicMock()  # type: ignore[assignment]

        with pytest.raises(ValueError, match="queen.cooldown must be a non-negative number"):
            await mgr.apply_update({"queen": {"cooldown": -1}})

    @pytest.mark.asyncio
    async def test_apply_queen_min_confidence_out_of_range_raises(self) -> None:
        daemon = _make_daemon()
        daemon.config.queen = QueenConfig()
        mgr = ConfigManager(daemon)
        mgr.reload = AsyncMock()  # type: ignore[assignment]
        mgr.save = MagicMock()  # type: ignore[assignment]

        with pytest.raises(
            ValueError, match="queen.min_confidence must be a number between 0.0 and 1.0"
        ):
            await mgr.apply_update({"queen": {"min_confidence": 1.5}})

    @pytest.mark.asyncio
    async def test_apply_notifications_invalid_debounce_raises(self) -> None:
        daemon = _make_daemon()
        daemon.config.notifications = NotifyConfig()
        mgr = ConfigManager(daemon)
        mgr.reload = AsyncMock()  # type: ignore[assignment]
        mgr.save = MagicMock()  # type: ignore[assignment]

        with pytest.raises(ValueError, match="notifications.debounce_seconds must be >= 0"):
            await mgr.apply_update({"notifications": {"debounce_seconds": -1}})

    @pytest.mark.asyncio
    async def test_apply_test_section(self) -> None:
        daemon = _make_daemon()
        daemon.config.test = TestConfig()
        mgr = ConfigManager(daemon)
        mgr.reload = AsyncMock()  # type: ignore[assignment]
        mgr.save = MagicMock()  # type: ignore[assignment]

        body: dict[str, Any] = {
            "test": {
                "port": 8080,
                "auto_resolve_delay": 10.0,
                "auto_complete_min_idle": 5.0,
                "report_dir": "/tmp/reports",
            }
        }
        await mgr.apply_update(body)

        assert daemon.config.test.port == 8080
        assert daemon.config.test.auto_resolve_delay == 10.0
        assert daemon.config.test.auto_complete_min_idle == 5.0
        assert daemon.config.test.report_dir == "/tmp/reports"

    @pytest.mark.asyncio
    async def test_apply_test_invalid_port_raises(self) -> None:
        daemon = _make_daemon()
        daemon.config.test = TestConfig()
        mgr = ConfigManager(daemon)
        mgr.reload = AsyncMock()  # type: ignore[assignment]
        mgr.save = MagicMock()  # type: ignore[assignment]

        with pytest.raises(ValueError, match="test.port must be an integer between 1024 and 65535"):
            await mgr.apply_update({"test": {"port": 80}})

    @pytest.mark.asyncio
    async def test_apply_workflows(self) -> None:
        daemon = _make_daemon()
        daemon.config.workflows = {}
        mgr = ConfigManager(daemon)
        mgr.reload = AsyncMock()  # type: ignore[assignment]
        mgr.save = MagicMock()  # type: ignore[assignment]

        with patch("swarm.server.config_manager.ConfigManager._apply_workflows") as mock_wf:
            await mgr.apply_update({"workflows": {"bug": "/fix"}})
            mock_wf.assert_called_once_with({"bug": "/fix"})


# ---------------------------------------------------------------------------
# parse_approval_rules — static validation
# ---------------------------------------------------------------------------


class TestParseApprovalRules:
    def test_valid_rules(self) -> None:
        raw = [
            {"pattern": "^(Yes|Allow)", "action": "approve"},
            {"pattern": "delete|remove", "action": "escalate"},
        ]
        rules = ConfigManager.parse_approval_rules(raw)
        assert len(rules) == 2
        assert rules[0].pattern == "^(Yes|Allow)"
        assert rules[0].action == "approve"
        assert rules[1].pattern == "delete|remove"
        assert rules[1].action == "escalate"

    def test_default_action_is_approve(self) -> None:
        raw = [{"pattern": ".*"}]
        rules = ConfigManager.parse_approval_rules(raw)
        assert rules[0].action == "approve"

    def test_invalid_action_raises(self) -> None:
        raw = [{"pattern": ".*", "action": "deny"}]
        with pytest.raises(ValueError, match="action must be 'approve' or 'escalate'"):
            ConfigManager.parse_approval_rules(raw)

    def test_invalid_regex_raises(self) -> None:
        raw = [{"pattern": "[invalid", "action": "approve"}]
        with pytest.raises(ValueError, match="invalid regex"):
            ConfigManager.parse_approval_rules(raw)

    def test_not_a_list_raises(self) -> None:
        with pytest.raises(ValueError, match="must be a list"):
            ConfigManager.parse_approval_rules("not a list")

    def test_non_dict_entry_raises(self) -> None:
        raw = ["not a dict"]
        with pytest.raises(ValueError, match="must be an object"):
            ConfigManager.parse_approval_rules(raw)

    def test_empty_list_returns_empty(self) -> None:
        rules = ConfigManager.parse_approval_rules([])
        assert rules == []

    def test_approval_rules_applied_via_update(self) -> None:
        daemon = _make_daemon()
        daemon.config.drones = DroneConfig()
        mgr = ConfigManager(daemon)

        rules_raw = [
            {"pattern": "^Allow", "action": "approve"},
            {"pattern": "danger", "action": "escalate"},
        ]
        mgr._apply_drones({"approval_rules": rules_raw})

        assert len(daemon.config.drones.approval_rules) == 2
        assert daemon.config.drones.approval_rules[0].pattern == "^Allow"
        assert daemon.config.drones.approval_rules[1].action == "escalate"


# ---------------------------------------------------------------------------
# toggle_drones
# ---------------------------------------------------------------------------


class TestToggleDrones:
    def test_toggle_with_no_pilot_returns_false(self) -> None:
        daemon = _make_daemon()
        daemon.pilot = None
        mgr = ConfigManager(daemon)
        assert mgr.toggle_drones() is False

    def test_toggle_calls_pilot_and_saves(self) -> None:
        daemon = _make_daemon()
        daemon.pilot = MagicMock()
        daemon.pilot.toggle.return_value = False
        daemon.config.drones = DroneConfig(enabled=True)
        mgr = ConfigManager(daemon)
        # Mock save to prevent actual disk writes
        mgr.save = MagicMock()  # type: ignore[assignment]

        result = mgr.toggle_drones()

        assert result is False
        daemon.pilot.toggle.assert_called_once()
        assert daemon.config.drones.enabled is False
        mgr.save.assert_called_once()
        daemon.broadcast_ws.assert_called_once_with({"type": "drones_toggled", "enabled": False})


# ---------------------------------------------------------------------------
# save — persist config and update mtime
# ---------------------------------------------------------------------------


class TestSave:
    def test_save_delegates_to_save_config(self, tmp_path: Path) -> None:
        cfg_file = tmp_path / "swarm.yaml"
        daemon = _make_daemon(source_path=str(cfg_file))
        mgr = ConfigManager(daemon)

        with patch("swarm.server.config_manager.save_config") as mock_save:
            mgr.save()
            mock_save.assert_called_once_with(daemon.config)

    def test_save_updates_mtime(self, tmp_path: Path) -> None:
        cfg_file = tmp_path / "swarm.yaml"
        _write_yaml(cfg_file, {"session_name": "save-test"})

        daemon = _make_daemon(source_path=str(cfg_file))
        daemon._config_mtime = 0.0
        mgr = ConfigManager(daemon)

        with patch("swarm.server.config_manager.save_config"):
            mgr.save()

        assert daemon._config_mtime == cfg_file.stat().st_mtime

    def test_save_without_source_path_skips_mtime(self) -> None:
        daemon = _make_daemon(source_path="")
        daemon._config_mtime = 0.0
        mgr = ConfigManager(daemon)

        with patch("swarm.server.config_manager.save_config"):
            mgr.save()

        # mtime stays at 0 — no source_path to stat
        assert daemon._config_mtime == 0.0


# ---------------------------------------------------------------------------
# _apply_scalars — workers, provider, graph settings
# ---------------------------------------------------------------------------


class TestApplyScalars:
    def test_apply_session_name(self) -> None:
        daemon = _make_daemon()
        mgr = ConfigManager(daemon)
        mgr._apply_scalars({"session_name": "new-session"})
        assert daemon.config.session_name == "new-session"

    def test_apply_log_level(self) -> None:
        daemon = _make_daemon()
        mgr = ConfigManager(daemon)
        mgr._apply_scalars({"log_level": "DEBUG"})
        assert daemon.config.log_level == "DEBUG"

    def test_apply_valid_provider(self) -> None:
        daemon = _make_daemon()
        mgr = ConfigManager(daemon)
        mgr._apply_scalars({"provider": "gemini"})
        assert daemon.config.provider == "gemini"

    def test_apply_invalid_provider_raises(self) -> None:
        daemon = _make_daemon()
        mgr = ConfigManager(daemon)
        with pytest.raises(ValueError, match="Invalid global provider"):
            mgr._apply_scalars({"provider": "openai"})

    def test_apply_worker_description(self) -> None:
        daemon = _make_daemon()
        daemon.config.workers = [WorkerConfig("api", "/tmp/api")]
        mgr = ConfigManager(daemon)

        mgr._apply_scalars({"workers": {"api": {"description": "API worker"}}})
        assert daemon.config.workers[0].description == "API worker"

    def test_apply_worker_description_string_compat(self) -> None:
        """Old format: worker value is just a description string."""
        daemon = _make_daemon()
        daemon.config.workers = [WorkerConfig("api", "/tmp/api")]
        mgr = ConfigManager(daemon)

        mgr._apply_scalars({"workers": {"api": "Legacy description"}})
        assert daemon.config.workers[0].description == "Legacy description"

    def test_apply_worker_provider(self) -> None:
        daemon = _make_daemon()
        daemon.config.workers = [WorkerConfig("api", "/tmp/api")]
        mgr = ConfigManager(daemon)

        mgr._apply_scalars({"workers": {"api": {"provider": "gemini"}}})
        assert daemon.config.workers[0].provider == "gemini"

    def test_apply_worker_invalid_provider_raises(self) -> None:
        daemon = _make_daemon()
        daemon.config.workers = [WorkerConfig("api", "/tmp/api")]
        mgr = ConfigManager(daemon)

        with pytest.raises(ValueError, match="invalid provider"):
            mgr._apply_scalars({"workers": {"api": {"provider": "openai"}}})

    def test_apply_unknown_worker_ignored(self) -> None:
        daemon = _make_daemon()
        daemon.config.workers = [WorkerConfig("api", "/tmp/api")]
        mgr = ConfigManager(daemon)

        # Should not raise for unknown worker names
        mgr._apply_scalars({"workers": {"nonexistent": {"description": "ghost"}}})
        assert daemon.config.workers[0].description == ""

    def test_apply_default_group(self) -> None:
        daemon = _make_daemon()
        daemon.config.groups = [GroupConfig("team", ["api"])]
        mgr = ConfigManager(daemon)

        mgr._apply_scalars({"default_group": "team"})
        assert daemon.config.default_group == "team"

    def test_apply_default_group_invalid_raises(self) -> None:
        daemon = _make_daemon()
        daemon.config.groups = [GroupConfig("team", ["api"])]
        mgr = ConfigManager(daemon)

        with pytest.raises(ValueError, match="does not match any defined group"):
            mgr._apply_scalars({"default_group": "nonexistent"})


# ---------------------------------------------------------------------------
# watch_mtime — async polling loop
# ---------------------------------------------------------------------------


class TestWatchMtime:
    @pytest.mark.asyncio
    async def test_watch_mtime_detects_change(self, tmp_path: Path) -> None:
        """watch_mtime broadcasts when config file mtime increases."""
        cfg_file = tmp_path / "swarm.yaml"
        _write_yaml(cfg_file, {"session_name": "watch"})

        daemon = _make_daemon(source_path=str(cfg_file))
        daemon._config_mtime = 0.0  # stale
        mgr = ConfigManager(daemon)

        # Patch sleep to return immediately, then cancel on second call
        call_count = 0

        async def _fake_sleep(seconds: float) -> None:
            nonlocal call_count
            call_count += 1
            if call_count > 1:
                raise asyncio.CancelledError

        import asyncio

        with patch("asyncio.sleep", side_effect=_fake_sleep):
            await mgr.watch_mtime()

        # Should have detected the change and broadcast
        daemon.broadcast_ws.assert_called_with({"type": "config_file_changed"})
        assert daemon._config_mtime == cfg_file.stat().st_mtime

    @pytest.mark.asyncio
    async def test_watch_mtime_skips_when_no_source_path(self) -> None:
        """watch_mtime does nothing when source_path is empty."""
        daemon = _make_daemon(source_path="")
        mgr = ConfigManager(daemon)

        call_count = 0

        async def _fake_sleep(seconds: float) -> None:
            nonlocal call_count
            call_count += 1
            if call_count > 1:
                raise asyncio.CancelledError

        import asyncio

        with patch("asyncio.sleep", side_effect=_fake_sleep):
            await mgr.watch_mtime()

        daemon.broadcast_ws.assert_not_called()
