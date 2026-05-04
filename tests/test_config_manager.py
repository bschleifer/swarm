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
from swarm.drones.log import DroneLog
from swarm.server.config_manager import ConfigManager, _body_touches_approval_rules
from swarm.testing.config import TestConfig


def _make_mgr(
    tmp_path: Path | None = None,
    *,
    source_path: str = "",
    config: HiveConfig | None = None,
) -> ConfigManager:
    """Build a ConfigManager with explicit deps (no daemon mock needed)."""
    if config is None:
        config = HiveConfig(source_path=source_path)
    broadcast_ws = MagicMock()
    apply_config = MagicMock()
    drone_log = DroneLog()
    rebuild_graph = MagicMock()
    mgr = ConfigManager(
        config=config,
        broadcast_ws=broadcast_ws,
        drone_log=drone_log,
        apply_config=apply_config,
        get_pilot=lambda: None,
        rebuild_graph=rebuild_graph,
    )
    # Stash deps as test-accessible attributes
    mgr._test_broadcast_ws = broadcast_ws  # type: ignore[attr-defined]
    mgr._test_apply_config = apply_config  # type: ignore[attr-defined]
    mgr._test_rebuild_graph = rebuild_graph  # type: ignore[attr-defined]
    return mgr


def _write_yaml(path: Path, data: dict) -> Path:
    path.write_text(yaml.dump(data, default_flow_style=False))
    return path


# ---------------------------------------------------------------------------
# Initialization
# ---------------------------------------------------------------------------


class TestInit:
    def test_stores_config_reference(self) -> None:
        config = HiveConfig()
        mgr = _make_mgr(config=config)
        assert mgr._config is config

    def test_hot_apply_delegates_to_apply_config(self) -> None:
        mgr = _make_mgr()
        mgr.hot_apply()
        mgr._test_apply_config.assert_called_once()  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# check_file — on-disk mtime detection
# ---------------------------------------------------------------------------


class TestCheckFile:
    def test_returns_false_when_no_source_path(self) -> None:
        mgr = _make_mgr(source_path="")
        assert mgr.check_file() is False

    def test_returns_false_when_file_missing(self) -> None:
        mgr = _make_mgr(source_path="/nonexistent/swarm.yaml")
        assert mgr.check_file() is False

    def test_returns_false_when_mtime_unchanged(self, tmp_path: Path) -> None:
        cfg_file = tmp_path / "swarm.yaml"
        _write_yaml(cfg_file, {"session_name": "test"})
        mtime = cfg_file.stat().st_mtime

        mgr = _make_mgr(source_path=str(cfg_file))
        mgr._config_mtime = mtime  # already up-to-date
        assert mgr.check_file() is False

    def test_detects_changed_mtime_and_reloads(self, tmp_path: Path) -> None:
        cfg_file = tmp_path / "swarm.yaml"
        _write_yaml(cfg_file, {"drones": {"poll_interval": 42}})

        mgr = _make_mgr(source_path=str(cfg_file))
        mgr._config_mtime = 0.0  # stale

        result = mgr.check_file()
        assert result is True
        # mtime should be updated
        assert mgr._config_mtime == cfg_file.stat().st_mtime
        # hot_apply should have been called
        mgr._test_apply_config.assert_called_once()  # type: ignore[attr-defined]
        # Config fields should be updated from the reloaded file
        assert mgr._config.drones.poll_interval == 42

    def test_returns_false_on_invalid_yaml(self, tmp_path: Path) -> None:
        cfg_file = tmp_path / "swarm.yaml"
        cfg_file.write_text("workers: [[[invalid yaml that will fail parsing")

        mgr = _make_mgr(source_path=str(cfg_file))
        mgr._config_mtime = 0.0

        # Should return False (reload failed) but update mtime to avoid retry loop
        result = mgr.check_file()
        assert result is False

    def test_yaml_reload_preserves_db_sourced_approval_rules(self, tmp_path: Path) -> None:
        """Regression: when the YAML changes on disk, the hot-reload path
        used to wholesale replace ``self._config.drones`` with the
        new_config.drones loaded from YAML — wiping every in-memory
        approval rule.  Since rules live in the DB (not the YAML), an
        unrelated scalar edit to swarm.yaml would silently empty the
        dashboard rules list.  The fix preserves in-memory rules
        across the reload.
        """
        from swarm.config.models import DroneApprovalRule, DroneConfig

        cfg_file = tmp_path / "swarm.yaml"
        # YAML has a scalar drone field but NO approval_rules section.
        _write_yaml(cfg_file, {"drones": {"poll_interval": 42}})

        # Seed the in-memory config with rules (as if they were loaded
        # from the DB).
        config = HiveConfig(
            source_path=str(cfg_file),
            drones=DroneConfig(
                approval_rules=[
                    DroneApprovalRule(pattern="Bash.*", action="approve"),
                    DroneApprovalRule(pattern="Read.*", action="approve"),
                ],
            ),
        )
        mgr = _make_mgr(config=config)
        mgr._config_mtime = 0.0  # force a reload

        assert mgr.check_file() is True
        # The YAML-edited scalar was applied...
        assert mgr._config.drones.poll_interval == 42
        # ...but the DB-sourced rules must survive intact.
        assert len(mgr._config.drones.approval_rules) == 2
        patterns = [r.pattern for r in mgr._config.drones.approval_rules]
        assert patterns == ["Bash.*", "Read.*"]

    def test_yaml_reload_preserves_per_worker_rules(self, tmp_path: Path) -> None:
        """Same preservation applies to worker-scoped rules."""
        from swarm.config.models import DroneApprovalRule

        cfg_file = tmp_path / "swarm.yaml"
        # YAML lists the same worker but with no rules.
        _write_yaml(
            cfg_file,
            {
                "workers": [{"name": "api", "path": str(tmp_path)}],
                "groups": [{"name": "all", "workers": ["api"]}],
            },
        )

        config = HiveConfig(
            source_path=str(cfg_file),
            workers=[
                WorkerConfig(
                    name="api",
                    path=str(tmp_path),
                    approval_rules=[DroneApprovalRule(pattern="Read.*", action="approve")],
                )
            ],
        )
        mgr = _make_mgr(config=config)
        mgr._config_mtime = 0.0

        assert mgr.check_file() is True
        api_worker = next(w for w in mgr._config.workers if w.name == "api")
        assert len(api_worker.approval_rules) == 1
        assert api_worker.approval_rules[0].pattern == "Read.*"


# ---------------------------------------------------------------------------
# reload — async hot-reload
# ---------------------------------------------------------------------------


class TestReload:
    @pytest.mark.asyncio
    async def test_reload_updates_config_and_broadcasts(self, tmp_path: Path) -> None:
        cfg_file = tmp_path / "swarm.yaml"
        _write_yaml(cfg_file, {"session_name": "new"})

        new_config = HiveConfig(session_name="new", source_path=str(cfg_file))
        mgr = _make_mgr(source_path=str(cfg_file))

        await mgr.reload(new_config)

        assert mgr._config.session_name == "new"
        mgr._test_apply_config.assert_called_once()  # type: ignore[attr-defined]
        mgr._test_broadcast_ws.assert_called_once_with({"type": "config_changed"})  # type: ignore[attr-defined]

    @pytest.mark.asyncio
    async def test_reload_updates_mtime_from_source_path(self, tmp_path: Path) -> None:
        cfg_file = tmp_path / "swarm.yaml"
        _write_yaml(cfg_file, {"session_name": "mtime-test"})
        expected_mtime = cfg_file.stat().st_mtime

        new_config = HiveConfig(source_path=str(cfg_file))
        mgr = _make_mgr(source_path=str(cfg_file))
        mgr._config_mtime = 0.0

        await mgr.reload(new_config)

        assert mgr._config_mtime == expected_mtime

    @pytest.mark.asyncio
    async def test_reload_without_source_path_skips_mtime(self) -> None:
        new_config = HiveConfig(session_name="no-path")
        mgr = _make_mgr(source_path="")
        mgr._config_mtime = 0.0

        await mgr.reload(new_config)

        # mtime unchanged because no source_path
        assert mgr._config_mtime == 0.0


# ---------------------------------------------------------------------------
# apply_update — partial config mutations from the API
# ---------------------------------------------------------------------------


class TestApplyUpdate:
    @pytest.mark.asyncio
    async def test_apply_drones_update(self) -> None:
        config = HiveConfig()
        config.drones = DroneConfig()
        mgr = _make_mgr(config=config)

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

        assert config.drones.enabled is False
        assert config.drones.poll_interval == 15.0
        assert config.drones.escalation_threshold == 60.0

    @pytest.mark.asyncio
    async def test_apply_queen_update(self) -> None:
        config = HiveConfig()
        config.queen = QueenConfig()
        mgr = _make_mgr(config=config)

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

        assert config.queen.cooldown == 120.0
        assert config.queen.enabled is False
        assert config.queen.system_prompt == "Custom prompt"
        assert config.queen.min_confidence == 0.5

    @pytest.mark.asyncio
    async def test_apply_queen_auto_assign_tasks(self) -> None:
        """auto_assign_tasks must persist through config save/reload."""
        config = HiveConfig()
        config.queen = QueenConfig()
        assert config.queen.auto_assign_tasks is True  # default

        mgr = _make_mgr(config=config)
        mgr.reload = AsyncMock()  # type: ignore[assignment]
        mgr.save = MagicMock()  # type: ignore[assignment]

        await mgr.apply_update({"queen": {"auto_assign_tasks": False}})
        assert config.queen.auto_assign_tasks is False

    @pytest.mark.asyncio
    async def test_apply_notifications_update(self) -> None:
        config = HiveConfig()
        config.notifications = NotifyConfig()
        mgr = _make_mgr(config=config)

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

        assert config.notifications.terminal_bell is False
        assert config.notifications.desktop is False
        assert config.notifications.debounce_seconds == 15.0

    @pytest.mark.asyncio
    async def test_apply_update_calls_reload_and_save(self) -> None:
        mgr = _make_mgr()
        mgr.reload = AsyncMock()  # type: ignore[assignment]
        mgr.save = MagicMock()  # type: ignore[assignment]

        await mgr.apply_update({"drones": {"enabled": True}})

        mgr.reload.assert_awaited_once()
        # A non-rules update must NOT propagate sync_rules=True,
        # otherwise any unrelated setting change would wipe the
        # approval_rules table.  Regression for the data-loss bug.
        mgr.save.assert_called_once_with(sync_rules=False)

    @pytest.mark.asyncio
    async def test_apply_update_forwards_sync_rules_when_rules_present(self) -> None:
        """Explicit approval_rules in the body → sync_rules=True on save."""
        mgr = _make_mgr()
        mgr.reload = AsyncMock()  # type: ignore[assignment]
        mgr.save = MagicMock()  # type: ignore[assignment]

        await mgr.apply_update(
            {
                "drones": {
                    "approval_rules": [{"pattern": "Bash.*", "action": "approve"}],
                },
            }
        )

        mgr.save.assert_called_once_with(sync_rules=True)

    def test_body_touches_approval_rules_detects_global(self) -> None:
        assert _body_touches_approval_rules(
            {"drones": {"approval_rules": [{"pattern": "Bash.*", "action": "approve"}]}}
        )

    def test_body_touches_approval_rules_detects_per_worker(self) -> None:
        assert _body_touches_approval_rules({"workers": [{"name": "api", "approval_rules": []}]})

    def test_body_touches_approval_rules_ignores_unrelated_fields(self) -> None:
        # The hotly-reloaded scalar path must NOT trip the flag — that's
        # exactly the scenario where the old save_config_to_db wiped
        # rules on every unrelated setting change.
        assert not _body_touches_approval_rules({"drones": {"enabled": True}})
        assert not _body_touches_approval_rules({"queen": {"cooldown": 30}})
        assert not _body_touches_approval_rules({"workers": [{"name": "api"}]})
        assert not _body_touches_approval_rules({})

    @pytest.mark.asyncio
    async def test_apply_update_invalid_drone_type_raises(self) -> None:
        config = HiveConfig()
        config.drones = DroneConfig()
        mgr = _make_mgr(config=config)
        mgr.reload = AsyncMock()  # type: ignore[assignment]
        mgr.save = MagicMock()  # type: ignore[assignment]

        with pytest.raises(ValueError, match=r"drones\.enabled must be boolean"):
            await mgr.apply_update({"drones": {"enabled": "yes"}})

    @pytest.mark.asyncio
    async def test_apply_update_negative_number_raises(self) -> None:
        config = HiveConfig()
        config.drones = DroneConfig()
        mgr = _make_mgr(config=config)
        mgr.reload = AsyncMock()  # type: ignore[assignment]
        mgr.save = MagicMock()  # type: ignore[assignment]

        with pytest.raises(ValueError, match=r"drones\.poll_interval must be >= 0"):
            await mgr.apply_update({"drones": {"poll_interval": -5}})

    @pytest.mark.asyncio
    async def test_apply_queen_invalid_cooldown_raises(self) -> None:
        config = HiveConfig()
        config.queen = QueenConfig()
        mgr = _make_mgr(config=config)
        mgr.reload = AsyncMock()  # type: ignore[assignment]
        mgr.save = MagicMock()  # type: ignore[assignment]

        with pytest.raises(ValueError, match=r"queen\.cooldown must be a non-negative number"):
            await mgr.apply_update({"queen": {"cooldown": -1}})

    @pytest.mark.asyncio
    async def test_apply_queen_min_confidence_out_of_range_raises(self) -> None:
        config = HiveConfig()
        config.queen = QueenConfig()
        mgr = _make_mgr(config=config)
        mgr.reload = AsyncMock()  # type: ignore[assignment]
        mgr.save = MagicMock()  # type: ignore[assignment]

        with pytest.raises(
            ValueError, match=r"queen\.min_confidence must be a number between 0\.0 and 1\.0"
        ):
            await mgr.apply_update({"queen": {"min_confidence": 1.5}})

    @pytest.mark.asyncio
    async def test_apply_notifications_invalid_debounce_raises(self) -> None:
        config = HiveConfig()
        config.notifications = NotifyConfig()
        mgr = _make_mgr(config=config)
        mgr.reload = AsyncMock()  # type: ignore[assignment]
        mgr.save = MagicMock()  # type: ignore[assignment]

        with pytest.raises(ValueError, match=r"notifications\.debounce_seconds must be >= 0"):
            await mgr.apply_update({"notifications": {"debounce_seconds": -1}})

    @pytest.mark.asyncio
    async def test_apply_test_section(self) -> None:
        config = HiveConfig()
        config.test = TestConfig()
        mgr = _make_mgr(config=config)
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

        assert config.test.port == 8080
        assert config.test.auto_resolve_delay == 10.0
        assert config.test.auto_complete_min_idle == 5.0
        assert config.test.report_dir == "/tmp/reports"

    @pytest.mark.asyncio
    async def test_apply_test_invalid_port_raises(self) -> None:
        config = HiveConfig()
        config.test = TestConfig()
        mgr = _make_mgr(config=config)
        mgr.reload = AsyncMock()  # type: ignore[assignment]
        mgr.save = MagicMock()  # type: ignore[assignment]

        with pytest.raises(
            ValueError, match=r"test\.port must be an integer between 1024 and 65535"
        ):
            await mgr.apply_update({"test": {"port": 80}})

    @pytest.mark.asyncio
    async def test_apply_workflows(self) -> None:
        config = HiveConfig()
        config.workflows = {}
        mgr = _make_mgr(config=config)
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
        config = HiveConfig()
        config.drones = DroneConfig()
        mgr = _make_mgr(config=config)

        rules_raw = [
            {"pattern": "^Allow", "action": "approve"},
            {"pattern": "danger", "action": "escalate"},
        ]
        mgr._apply_drones({"approval_rules": rules_raw})

        assert len(config.drones.approval_rules) == 2
        assert config.drones.approval_rules[0].pattern == "^Allow"
        assert config.drones.approval_rules[1].action == "escalate"


# ---------------------------------------------------------------------------
# toggle_drones
# ---------------------------------------------------------------------------


class TestToggleDrones:
    def test_toggle_with_no_pilot_returns_false(self) -> None:
        mgr = _make_mgr()
        # get_pilot returns None by default
        assert mgr.toggle_drones() is False

    def test_toggle_calls_pilot_and_saves(self) -> None:
        config = HiveConfig()
        config.drones = DroneConfig(enabled=True)
        pilot = MagicMock()
        pilot.toggle.return_value = False
        broadcast_ws = MagicMock()
        mgr = ConfigManager(
            config=config,
            broadcast_ws=broadcast_ws,
            drone_log=DroneLog(),
            apply_config=MagicMock(),
            get_pilot=lambda: pilot,
            rebuild_graph=MagicMock(),
        )
        # Mock save to prevent actual disk writes
        mgr.save = MagicMock()  # type: ignore[assignment]

        result = mgr.toggle_drones()

        assert result is False
        pilot.toggle.assert_called_once()
        assert config.drones.enabled is False
        mgr.save.assert_called_once()
        broadcast_ws.assert_called_once_with({"type": "drones_toggled", "enabled": False})


# ---------------------------------------------------------------------------
# save — persist config and update mtime
# ---------------------------------------------------------------------------


class TestSave:
    def test_save_delegates_to_save_config(self, tmp_path: Path) -> None:
        config = HiveConfig(source_path=str(tmp_path / "swarm.yaml"))
        mgr = _make_mgr(config=config)

        with patch("swarm.server.config_manager.save_config") as mock_save:
            mgr.save()
            mock_save.assert_called_once_with(config)

    def test_save_updates_mtime(self, tmp_path: Path) -> None:
        cfg_file = tmp_path / "swarm.yaml"
        _write_yaml(cfg_file, {"session_name": "save-test"})

        config = HiveConfig(source_path=str(cfg_file))
        mgr = _make_mgr(config=config)
        mgr._config_mtime = 0.0

        with patch("swarm.server.config_manager.save_config"):
            mgr.save()

        assert mgr._config_mtime == cfg_file.stat().st_mtime

    def test_save_without_source_path_skips_mtime(self) -> None:
        mgr = _make_mgr(source_path="")
        mgr._config_mtime = 0.0

        with patch("swarm.server.config_manager.save_config"):
            mgr.save()

        # mtime stays at 0 — no source_path to stat
        assert mgr._config_mtime == 0.0

    def test_save_to_db_failure_logged_at_error_level(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Regression for #328: a failing DB config save must surface in
        default-level logs, not be swallowed at DEBUG.

        Without this, an operator whose DB save silently fails (lock,
        permissions, schema mismatch, etc.) sees the dashboard report
        success but the change vanishes on reboot — with zero forensic
        evidence in their warning log.  Reported by a user whose Groups
        edits weren't persisting across restarts; her warning log
        contained no save-related entries because the failure was
        emitted at DEBUG.
        """
        import logging

        config = HiveConfig()
        broadcast_ws = MagicMock()
        # Pass a non-None swarm_db so the DB save path is taken.  The
        # mock raises on any save, simulating a real failure.
        swarm_db = MagicMock()
        mgr = ConfigManager(
            config=config,
            broadcast_ws=broadcast_ws,
            drone_log=DroneLog(),
            apply_config=MagicMock(),
            get_pilot=lambda: None,
            rebuild_graph=MagicMock(),
            swarm_db=swarm_db,
        )

        with patch(
            "swarm.db.config_store.save_config_to_db",
            side_effect=RuntimeError("simulated DB write failure"),
        ):
            with caplog.at_level(logging.WARNING, logger="swarm.server.config_manager"):
                # save() should fall through to YAML when DB save fails.
                # No raise — the function is best-effort.  But the
                # failure MUST be visible at WARNING+ level.
                with patch("swarm.server.config_manager.save_config"):
                    mgr.save()

        # The DB save error must be visible at >= WARNING.  "DEBUG"
        # records are excluded by caplog.at_level(WARNING).
        save_failures = [
            r
            for r in caplog.records
            if r.name == "swarm.server.config_manager" and "DB config save failed" in r.getMessage()
        ]
        assert save_failures, (
            "DB save failure should be logged at WARNING+ so it appears in "
            "default-level operator logs.  Found records: "
            f"{[(r.levelname, r.getMessage()) for r in caplog.records]}"
        )
        # Must include the underlying exception for diagnosis.
        assert save_failures[0].exc_info is not None
        # Must be at WARNING or higher (not DEBUG/INFO).
        assert save_failures[0].levelno >= logging.WARNING


# ---------------------------------------------------------------------------
# _apply_scalars — workers, provider, graph settings
# ---------------------------------------------------------------------------


class TestApplyScalars:
    def test_apply_session_name(self) -> None:
        mgr = _make_mgr()
        mgr._apply_scalars({"session_name": "new-session"})
        assert mgr._config.session_name == "new-session"

    def test_apply_log_level(self) -> None:
        mgr = _make_mgr()
        mgr._apply_scalars({"log_level": "DEBUG"})
        assert mgr._config.log_level == "DEBUG"

    def test_apply_valid_provider(self) -> None:
        mgr = _make_mgr()
        mgr._apply_scalars({"provider": "gemini"})
        assert mgr._config.provider == "gemini"

    def test_apply_invalid_provider_raises(self) -> None:
        mgr = _make_mgr()
        with pytest.raises(ValueError, match="Invalid global provider"):
            mgr._apply_scalars({"provider": "openai"})

    def test_apply_worker_description(self) -> None:
        config = HiveConfig()
        config.workers = [WorkerConfig("api", "/tmp/api")]
        mgr = _make_mgr(config=config)

        mgr._apply_scalars({"workers": {"api": {"description": "API worker"}}})
        assert config.workers[0].description == "API worker"

    def test_apply_worker_description_string_compat(self) -> None:
        """Old format: worker value is just a description string."""
        config = HiveConfig()
        config.workers = [WorkerConfig("api", "/tmp/api")]
        mgr = _make_mgr(config=config)

        mgr._apply_scalars({"workers": {"api": "Legacy description"}})
        assert config.workers[0].description == "Legacy description"

    def test_apply_worker_provider(self) -> None:
        config = HiveConfig()
        config.workers = [WorkerConfig("api", "/tmp/api")]
        mgr = _make_mgr(config=config)

        mgr._apply_scalars({"workers": {"api": {"provider": "gemini"}}})
        assert config.workers[0].provider == "gemini"

    def test_apply_worker_invalid_provider_raises(self) -> None:
        config = HiveConfig()
        config.workers = [WorkerConfig("api", "/tmp/api")]
        mgr = _make_mgr(config=config)

        with pytest.raises(ValueError, match="invalid provider"):
            mgr._apply_scalars({"workers": {"api": {"provider": "openai"}}})

    def test_apply_unknown_worker_ignored(self) -> None:
        config = HiveConfig()
        config.workers = [WorkerConfig("api", "/tmp/api")]
        mgr = _make_mgr(config=config)

        # Should not raise for unknown worker names
        mgr._apply_scalars({"workers": {"nonexistent": {"description": "ghost"}}})
        assert config.workers[0].description == ""

    def test_apply_default_group(self) -> None:
        config = HiveConfig()
        config.groups = [GroupConfig("team", ["api"])]
        mgr = _make_mgr(config=config)

        mgr._apply_scalars({"default_group": "team"})
        assert config.default_group == "team"

    def test_apply_default_group_invalid_raises(self) -> None:
        config = HiveConfig()
        config.groups = [GroupConfig("team", ["api"])]
        mgr = _make_mgr(config=config)

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

        mgr = _make_mgr(source_path=str(cfg_file))
        mgr._config_mtime = 0.0  # stale

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
        mgr._test_broadcast_ws.assert_called_with({"type": "config_file_changed"})  # type: ignore[attr-defined]
        assert mgr._config_mtime == cfg_file.stat().st_mtime

    @pytest.mark.asyncio
    async def test_watch_mtime_skips_when_no_source_path(self) -> None:
        """watch_mtime does nothing when source_path is empty."""
        mgr = _make_mgr(source_path="")

        call_count = 0

        async def _fake_sleep(seconds: float) -> None:
            nonlocal call_count
            call_count += 1
            if call_count > 1:
                raise asyncio.CancelledError

        import asyncio

        with patch("asyncio.sleep", side_effect=_fake_sleep):
            await mgr.watch_mtime()

        mgr._test_broadcast_ws.assert_not_called()  # type: ignore[attr-defined]
