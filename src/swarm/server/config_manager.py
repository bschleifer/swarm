"""ConfigManager — config validation, hot-reload, and persistence."""

from __future__ import annotations

import asyncio
import re as _re
from pathlib import Path
from typing import TYPE_CHECKING, Any

from swarm.config import DroneApprovalRule, HiveConfig, load_config, save_config
from swarm.drones.log import LogCategory, SystemAction
from swarm.logging import get_logger

if TYPE_CHECKING:
    from swarm.server.daemon import SwarmDaemon

_log = get_logger("server.config_manager")


class ConfigManager:
    """Manages config hot-reload, validation, and persistence."""

    def __init__(self, daemon: SwarmDaemon) -> None:
        self._daemon = daemon

    # --- Hot-reload ---

    def hot_apply(self) -> None:
        """Apply config changes to pilot, queen, and notification bus."""
        self._daemon.apply_config()

    async def reload(self, new_config: HiveConfig) -> None:
        """Hot-reload configuration. Updates pilot, queen, and notifies WS clients."""
        d = self._daemon
        d.config = new_config
        self.hot_apply()

        # Update mtime tracker
        if new_config.source_path:
            sp = Path(new_config.source_path)
            if sp.exists():
                d._config_mtime = sp.stat().st_mtime

        d.broadcast_ws({"type": "config_changed"})
        d.drone_log.add(
            SystemAction.CONFIG_CHANGED,
            "system",
            "config reloaded",
            category=LogCategory.SYSTEM,
        )
        _log.info("config hot-reloaded")

    async def watch_mtime(self) -> None:
        """Poll config file mtime every 30s and notify WS clients if changed."""
        d = self._daemon
        try:
            while True:
                await asyncio.sleep(30)
                if not d.config.source_path:
                    continue
                try:
                    sp = Path(d.config.source_path)
                    if sp.exists():
                        mtime = sp.stat().st_mtime
                        if mtime > d._config_mtime:
                            d._config_mtime = mtime
                            d.broadcast_ws({"type": "config_file_changed"})
                            _log.info("config file changed on disk")
                except OSError:
                    _log.debug("mtime check failed", exc_info=True)
        except asyncio.CancelledError:
            return

    def check_file(self) -> bool:
        """Check if config file changed on disk; reload if so. Returns True if reloaded."""
        d = self._daemon
        if not d.config.source_path:
            return False
        try:
            current_mtime = Path(d.config.source_path).stat().st_mtime
        except OSError:
            return False
        if current_mtime <= d._config_mtime:
            return False
        d._config_mtime = current_mtime
        try:
            new_config = load_config(d.config.source_path)
        except (OSError, ValueError, KeyError):
            _log.warning("failed to reload config from disk", exc_info=True)
            return False

        # Hot-apply fields that don't require worker lifecycle changes
        d.config.groups = new_config.groups
        d.config.drones = new_config.drones
        d.config.queen = new_config.queen
        d.config.notifications = new_config.notifications
        d.config.workers = new_config.workers
        d.config.api_password = new_config.api_password
        d.config.test = new_config.test

        self.hot_apply()

        _log.info("config reloaded from disk (external change detected)")
        return True

    def toggle_drones(self) -> bool:
        """Toggle drone pilot and persist to config. Returns new enabled state."""
        d = self._daemon
        if not d.pilot:
            return False
        new_state = d.pilot.toggle()
        d.config.drones.enabled = new_state
        self.save()
        d.broadcast_ws({"type": "drones_toggled", "enabled": new_state})
        return new_state

    def save(self) -> None:
        """Save config to disk and update mtime to prevent self-triggered reload."""
        d = self._daemon
        save_config(d.config)
        if d.config.source_path:
            try:
                d._config_mtime = Path(d.config.source_path).stat().st_mtime
            except OSError:
                pass

    # --- Config update validation + apply ---

    @staticmethod
    def parse_approval_rules(rules_raw: object) -> list[DroneApprovalRule]:
        """Parse and validate approval rules from a config update."""
        if not isinstance(rules_raw, list):
            raise ValueError("drones.approval_rules must be a list")
        parsed = []
        for i, r in enumerate(rules_raw):
            if not isinstance(r, dict):
                raise ValueError(f"drones.approval_rules[{i}] must be an object")
            pattern = r.get("pattern", "")
            action = r.get("action", "approve")
            if action not in ("approve", "escalate"):
                raise ValueError(
                    f"drones.approval_rules[{i}].action must be 'approve' or 'escalate'"
                )
            try:
                _re.compile(pattern)
            except _re.error as exc:
                raise ValueError(
                    f"drones.approval_rules[{i}].pattern: invalid regex: {exc}"
                ) from exc
            parsed.append(DroneApprovalRule(pattern=pattern, action=action))
        return parsed

    def _apply_drones(self, bz: dict[str, Any]) -> None:
        """Validate and apply drones section of a config update."""
        cfg = self._daemon.config.drones
        for key in (
            "enabled",
            "escalation_threshold",
            "poll_interval",
            "auto_approve_yn",
            "max_revive_attempts",
            "max_poll_failures",
            "max_idle_interval",
            "auto_stop_on_complete",
        ):
            if key in bz:
                val = bz[key]
                if key in ("enabled", "auto_approve_yn", "auto_stop_on_complete"):
                    if not isinstance(val, bool):
                        raise ValueError(f"drones.{key} must be boolean")
                else:
                    if not isinstance(val, (int, float)):
                        raise ValueError(f"drones.{key} must be a number")
                    if val < 0:
                        raise ValueError(f"drones.{key} must be >= 0")
                setattr(cfg, key, val)
        if "approval_rules" in bz:
            self._daemon.config.drones.approval_rules = self.parse_approval_rules(
                bz["approval_rules"]
            )

    def _apply_queen(self, qn: dict[str, Any]) -> None:
        """Validate and apply queen section of a config update."""
        cfg = self._daemon.config.queen
        if "cooldown" in qn:
            if not isinstance(qn["cooldown"], (int, float)) or qn["cooldown"] < 0:
                raise ValueError("queen.cooldown must be a non-negative number")
            cfg.cooldown = qn["cooldown"]
        if "enabled" in qn:
            if not isinstance(qn["enabled"], bool):
                raise ValueError("queen.enabled must be boolean")
            cfg.enabled = qn["enabled"]
        if "system_prompt" in qn:
            if not isinstance(qn["system_prompt"], str):
                raise ValueError("queen.system_prompt must be a string")
            cfg.system_prompt = qn["system_prompt"]
        if "min_confidence" in qn:
            val = qn["min_confidence"]
            if not isinstance(val, (int, float)) or not (0.0 <= val <= 1.0):
                raise ValueError("queen.min_confidence must be a number between 0.0 and 1.0")
            cfg.min_confidence = float(val)

    def _apply_notifications(self, nt: dict[str, Any]) -> None:
        """Validate and apply notifications section of a config update."""
        cfg = self._daemon.config.notifications
        for key in ("terminal_bell", "desktop"):
            if key in nt:
                if not isinstance(nt[key], bool):
                    raise ValueError(f"notifications.{key} must be boolean")
                setattr(cfg, key, nt[key])
        if "debounce_seconds" in nt:
            if not isinstance(nt["debounce_seconds"], (int, float)) or nt["debounce_seconds"] < 0:
                raise ValueError("notifications.debounce_seconds must be >= 0")
            cfg.debounce_seconds = nt["debounce_seconds"]

    def _apply_workflows(self, wf: object) -> None:
        """Validate and apply workflows section of a config update."""
        if not isinstance(wf, dict):
            raise ValueError("workflows must be an object")
        valid_types = {"bug", "feature", "verify", "chore"}
        cleaned: dict[str, str] = {}
        for k, v in wf.items():
            if k not in valid_types:
                raise ValueError(f"workflows key '{k}' is not a valid task type")
            if not isinstance(v, str):
                raise ValueError(f"workflows.{k} must be a string")
            cleaned[k] = v.strip()
        self._daemon.config.workflows = cleaned
        from swarm.tasks.workflows import apply_config_overrides

        apply_config_overrides(cleaned)

    def _apply_test(self, ts: dict[str, Any]) -> None:
        """Validate and apply test section of a config update."""
        cfg = self._daemon.config.test
        if "port" in ts:
            val = ts["port"]
            if not isinstance(val, int) or not (1024 <= val <= 65535):
                raise ValueError("test.port must be an integer between 1024 and 65535")
            cfg.port = val
        if "auto_resolve_delay" in ts:
            val = ts["auto_resolve_delay"]
            if not isinstance(val, (int, float)) or val < 0:
                raise ValueError("test.auto_resolve_delay must be >= 0")
            cfg.auto_resolve_delay = float(val)
        if "auto_complete_min_idle" in ts:
            val = ts["auto_complete_min_idle"]
            if not isinstance(val, (int, float)) or val < 1:
                raise ValueError("test.auto_complete_min_idle must be >= 1")
            cfg.auto_complete_min_idle = float(val)
        if "report_dir" in ts:
            val = ts["report_dir"]
            if not isinstance(val, str) or not val.strip():
                raise ValueError("test.report_dir must be a non-empty string")
            cfg.report_dir = val.strip()

    def _apply_default_group(self, dg: object) -> None:
        """Validate and apply default_group setting."""
        if not isinstance(dg, str):
            raise ValueError("default_group must be a string")
        if dg:
            group_names = {g.name.lower() for g in self._daemon.config.groups}
            if dg.lower() not in group_names:
                raise ValueError(f"default_group '{dg}' does not match any defined group")
        self._daemon.config.default_group = dg

    def _apply_scalars(self, body: dict[str, Any]) -> None:
        """Apply workers, default_group, scalars, and graph settings."""
        d = self._daemon
        if "workers" in body and isinstance(body["workers"], dict):
            for wname, desc in body["workers"].items():
                wc = d.config.get_worker(wname)
                if wc and isinstance(desc, str):
                    wc.description = desc
        if "default_group" in body:
            self._apply_default_group(body["default_group"])
        for key in ("session_name", "projects_dir", "log_level"):
            if key in body:
                setattr(d.config, key, body[key])
        for key, attr in (
            ("graph_client_id", "graph_client_id"),
            ("graph_tenant_id", "graph_tenant_id"),
        ):
            if key in body and isinstance(body[key], str):
                val = body[key].strip() or ("common" if key == "graph_tenant_id" else "")
                setattr(d.config, attr, val)
        self._apply_buttons(body)

    def _apply_buttons(self, body: dict[str, Any]) -> None:
        """Apply tool_buttons and action_buttons from the request body."""
        d = self._daemon
        if "tool_buttons" in body and isinstance(body["tool_buttons"], list):
            from swarm.config import ToolButtonConfig

            d.config.tool_buttons = [
                ToolButtonConfig(label=b["label"], command=b.get("command", ""))
                for b in body["tool_buttons"]
                if isinstance(b, dict) and b.get("label")
            ]
        if "action_buttons" in body and isinstance(body["action_buttons"], list):
            from swarm.config import ActionButtonConfig

            d.config.action_buttons = [
                ActionButtonConfig(
                    label=b["label"],
                    action=b.get("action", ""),
                    command=b.get("command", ""),
                    style=b.get("style", "secondary"),
                    show_mobile=b.get("show_mobile", True),
                    show_desktop=b.get("show_desktop", True),
                )
                for b in body["action_buttons"]
                if isinstance(b, dict) and b.get("label")
            ]
        if "task_buttons" in body and isinstance(body["task_buttons"], list):
            from swarm.config import TaskButtonConfig

            d.config.task_buttons = [
                TaskButtonConfig(
                    label=b["label"],
                    action=b.get("action", ""),
                    show_mobile=b.get("show_mobile", True),
                    show_desktop=b.get("show_desktop", True),
                )
                for b in body["task_buttons"]
                if isinstance(b, dict) and b.get("label") and b.get("action")
            ]

    async def apply_update(self, body: dict[str, Any]) -> None:
        """Apply a partial config update from the API. Raises ValueError on invalid input."""
        d = self._daemon
        if "drones" in body:
            self._apply_drones(body["drones"])
        if "queen" in body:
            self._apply_queen(body["queen"])
        if "notifications" in body:
            self._apply_notifications(body["notifications"])
        self._apply_scalars(body)
        if "workflows" in body:
            self._apply_workflows(body["workflows"])
        if "test" in body:
            self._apply_test(body["test"])

        # Rebuild graph manager if client_id changed
        d.graph_mgr = d._build_graph_manager(d.config)
        d.email._graph_mgr = d.graph_mgr

        # Hot-reload and save
        await self.reload(d.config)
        self.save()
