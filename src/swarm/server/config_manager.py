"""ConfigManager — config validation, hot-reload, and persistence."""

from __future__ import annotations

import asyncio
import re as _re
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from yaml import YAMLError

from swarm.config import DroneApprovalRule, HiveConfig, WorkerConfig, load_config, save_config
from swarm.drones.log import DroneLog, LogCategory, SystemAction
from swarm.logging import get_logger

if TYPE_CHECKING:
    from swarm.db.core import SwarmDB
    from swarm.drones.pilot import DronePilot
    from swarm.server.worker_service import WorkerService

_log = get_logger("server.config_manager")


def _body_touches_approval_rules(body: dict[str, Any]) -> bool:
    """Return True if an apply_update body contains an approval_rules edit.

    Checks both the global path (``body["drones"]["approval_rules"]``)
    and any per-worker path
    (``body["workers"][i]["approval_rules"]``).  Used to decide whether
    the subsequent save should propagate ``sync_rules=True`` — keeping
    routine non-rules edits from overwriting the approval_rules table.
    """
    drones = body.get("drones")
    if isinstance(drones, dict) and "approval_rules" in drones:
        return True
    workers = body.get("workers")
    if isinstance(workers, list):
        for w in workers:
            if isinstance(w, dict) and "approval_rules" in w:
                return True
    return False


class ConfigManager:
    """Manages config hot-reload, validation, and persistence."""

    def __init__(
        self,
        config: HiveConfig,
        broadcast_ws: Callable[[dict[str, Any]], None],
        drone_log: DroneLog,
        apply_config: Callable[[], None],
        get_pilot: Callable[[], DronePilot | None],
        rebuild_graph: Callable[[], None],
        rebuild_jira: Callable[[], None] | None = None,
        get_worker_svc: Callable[[], WorkerService | None] | None = None,
        swarm_db: SwarmDB | None = None,
    ) -> None:
        self._config = config
        self._broadcast_ws = broadcast_ws
        self._drone_log = drone_log
        self._apply_config = apply_config
        self._get_pilot = get_pilot
        self._rebuild_graph = rebuild_graph
        self._rebuild_jira = rebuild_jira or (lambda: None)
        self._get_worker_svc = get_worker_svc or (lambda: None)
        self._swarm_db = swarm_db  # SwarmDB instance (None = YAML-only)
        self._config_mtime: float = 0.0

    # --- Hot-reload ---

    def hot_apply(self) -> None:
        """Apply config changes to pilot, queen, and notification bus."""
        self._apply_config()

    def _invalidate_provider_cache(self) -> None:
        """Clear pilot's provider cache so tuning changes take effect."""
        pilot = self._get_pilot()
        if pilot:
            pilot.invalidate_provider_cache()

    async def reload(self, new_config: HiveConfig) -> None:
        """Hot-reload configuration. Updates pilot, queen, and notifies WS clients."""
        # Replace the shared config object's fields in-place so all holders
        # of the reference see the update.  The daemon's self.config binding
        # is updated by the caller (apply_update) when needed.
        self._config.__dict__.update(new_config.__dict__)
        self.hot_apply()

        # Update mtime tracker
        if new_config.source_path:
            sp = Path(new_config.source_path)
            if sp.exists():
                self._config_mtime = sp.stat().st_mtime

        self._broadcast_ws({"type": "config_changed"})
        self._drone_log.add(
            SystemAction.CONFIG_CHANGED,
            "system",
            "config reloaded",
            category=LogCategory.SYSTEM,
        )
        _log.info("config hot-reloaded")

    async def watch_mtime(self) -> None:
        """Poll config file mtime every 30s and notify WS clients if changed."""
        try:
            while True:
                await asyncio.sleep(30)
                if not self._config.source_path:
                    continue
                try:
                    sp = Path(self._config.source_path)
                    if sp.exists():
                        mtime = sp.stat().st_mtime
                        if mtime > self._config_mtime:
                            self._config_mtime = mtime
                            self._broadcast_ws({"type": "config_file_changed"})
                            _log.info("config file changed on disk")
                except OSError:
                    _log.debug("mtime check failed", exc_info=True)
        except asyncio.CancelledError:
            return

    def check_file(self) -> bool:
        """Check if config file changed on disk; reload if so. Returns True if reloaded."""
        if not self._config.source_path:
            return False
        try:
            current_mtime = Path(self._config.source_path).stat().st_mtime
        except OSError:
            return False
        if current_mtime <= self._config_mtime:
            return False
        self._config_mtime = current_mtime
        try:
            new_config = load_config(self._config.source_path)
        except (OSError, ValueError, KeyError, YAMLError):
            _log.warning("failed to reload config from disk", exc_info=True)
            return False

        # Hot-apply fields that don't require worker lifecycle changes.
        #
        # CAREFUL: approval_rules live in the DB, not the YAML.  The
        # YAML hot-reload must NOT overwrite them — if we blindly
        # assigned ``self._config.drones = new_config.drones`` the
        # in-memory rule list would be wiped on every external YAML
        # edit (user tweaks a scalar in swarm.yaml → rule list goes
        # empty → dashboard shows nothing).  Preserve the existing
        # rules and copy the YAML-editable drone fields in place.
        preserved_global_rules = list(self._config.drones.approval_rules)
        # Preserve per-worker rules too, keyed by worker name.
        preserved_worker_rules = {w.name: list(w.approval_rules) for w in self._config.workers}

        self._config.groups = new_config.groups
        new_config.drones.approval_rules = preserved_global_rules
        self._config.drones = new_config.drones
        self._config.queen = new_config.queen
        self._config.notifications = new_config.notifications
        # Reapply preserved per-worker rules onto the reloaded worker
        # list before we swap it in.
        for w in new_config.workers:
            if w.name in preserved_worker_rules:
                w.approval_rules = preserved_worker_rules[w.name]
        self._config.workers = new_config.workers
        self._config.api_password = new_config.api_password
        self._config.test = new_config.test
        self._config.custom_llms = new_config.custom_llms
        self._config.provider_overrides = new_config.provider_overrides
        # Refresh custom provider registry from disk-reloaded config
        if new_config.custom_llms:
            from swarm.providers import register_custom_providers

            register_custom_providers(new_config.custom_llms)
        from swarm.providers import register_provider_overrides

        register_provider_overrides(new_config.provider_overrides)
        self._invalidate_provider_cache()

        self.hot_apply()

        _log.info("config reloaded from disk (external change detected)")
        return True

    def toggle_drones(self) -> bool:
        """Toggle drone pilot and persist to config. Returns new enabled state."""
        pilot = self._get_pilot()
        if not pilot:
            return False
        new_state = pilot.toggle()
        self._config.drones.enabled = new_state
        self.save()
        self._broadcast_ws({"type": "drones_toggled", "enabled": new_state})
        return new_state

    def save(self, *, sync_rules: bool = False) -> None:
        """Save config to DB (primary) or YAML (fallback).

        Pass ``sync_rules=True`` **only** when the caller has just
        modified ``drones.approval_rules`` (global or per-worker) and
        wants the change persisted.  Default is False so routine saves
        — toggling drones, editing unrelated settings, hot-reload
        callbacks — cannot wipe the rules table.
        """
        if self._save_to_db(sync_rules=sync_rules):
            return
        from swarm.config import ConfigError

        try:
            save_config(self._config)
        except ConfigError:
            return
        if self._config.source_path:
            try:
                self._config_mtime = Path(self._config.source_path).stat().st_mtime
            except OSError:
                pass

    def _save_to_db(self, *, sync_rules: bool = False) -> bool:
        """Save config to swarm.db. Returns True on success.

        Failures are logged at WARNING level (not DEBUG) so a silently
        failing save surfaces in default-level operator logs.  Reported
        in #328: a user's Groups edits weren't persisting across reboots
        because the dashboard reported success while the underlying DB
        write was failing — with no forensic evidence at WARNING.
        """
        if self._swarm_db is None:
            return False
        try:
            from swarm.db.config_store import save_config_to_db

            save_config_to_db(self._swarm_db, self._config, sync_approval_rules=sync_rules)
            return True
        except Exception:
            _log.warning("DB config save failed", exc_info=True)
            return False

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

    _DRONE_BOOL_KEYS = frozenset(
        {
            "enabled",
            "auto_approve_yn",
            "auto_stop_on_complete",
            "auto_approve_assignments",
        }
    )
    _DRONE_SCALAR_KEYS = (
        "enabled",
        "escalation_threshold",
        "poll_interval",
        "auto_approve_yn",
        "max_revive_attempts",
        "max_poll_failures",
        "max_idle_interval",
        "auto_stop_on_complete",
        "auto_approve_assignments",
        "idle_assign_threshold",
        "auto_complete_min_idle",
        "sleeping_threshold",
        "sleeping_poll_interval",
        "stung_reap_timeout",
        "poll_interval_buzzing",
        "poll_interval_waiting",
        "poll_interval_resting",
    )

    def _apply_drone_state_thresholds(self, st: dict[str, Any]) -> None:
        """Validate and apply drones.state_thresholds sub-section."""
        cfg = self._config.drones.state_thresholds
        for k in ("buzzing_confirm_count", "stung_confirm_count"):
            if k in st:
                v = st[k]
                if not isinstance(v, int) or v < 1:
                    raise ValueError(f"drones.state_thresholds.{k} must be >= 1")
                setattr(cfg, k, v)
        if "revive_grace" in st:
            v = st["revive_grace"]
            if not isinstance(v, (int, float)) or v < 0:
                raise ValueError("drones.state_thresholds.revive_grace must be >= 0")
            cfg.revive_grace = float(v)

    @staticmethod
    def _validate_drone_scalar(key: str, val: object, bool_keys: frozenset[str]) -> None:
        """Raise ValueError if a drone scalar value has the wrong type."""
        if key in bool_keys:
            if not isinstance(val, bool):
                raise ValueError(f"drones.{key} must be boolean")
        else:
            if not isinstance(val, (int, float)):
                raise ValueError(f"drones.{key} must be a number")
            if val < 0:
                raise ValueError(f"drones.{key} must be >= 0")

    def _apply_drones(self, bz: dict[str, Any]) -> None:
        """Validate and apply drones section of a config update."""
        cfg = self._config.drones
        for key in self._DRONE_SCALAR_KEYS:
            if key not in bz:
                continue
            self._validate_drone_scalar(key, bz[key], self._DRONE_BOOL_KEYS)
            setattr(cfg, key, bz[key])
        if "state_thresholds" in bz and isinstance(bz["state_thresholds"], dict):
            self._apply_drone_state_thresholds(bz["state_thresholds"])
        if "allowed_read_paths" in bz:
            val = bz["allowed_read_paths"]
            if isinstance(val, list) and all(isinstance(p, str) for p in val):
                cfg.allowed_read_paths = val
        if "approval_rules" in bz:
            self._config.drones.approval_rules = self.parse_approval_rules(bz["approval_rules"])

    def _apply_queen_oversight(self, ov: dict[str, Any]) -> None:
        """Validate and apply queen.oversight sub-section."""
        ocfg = self._config.queen.oversight
        if "enabled" in ov:
            if not isinstance(ov["enabled"], bool):
                raise ValueError("queen.oversight.enabled must be boolean")
            ocfg.enabled = ov["enabled"]
        for k in ("buzzing_threshold_minutes", "drift_check_interval_minutes"):
            if k in ov:
                v = ov[k]
                if not isinstance(v, (int, float)) or v <= 0:
                    raise ValueError(f"queen.oversight.{k} must be > 0")
                setattr(ocfg, k, float(v))
        if "max_calls_per_hour" in ov:
            v = ov["max_calls_per_hour"]
            if not isinstance(v, int) or v < 1:
                raise ValueError("queen.oversight.max_calls_per_hour must be >= 1")
            ocfg.max_calls_per_hour = v

    # (key, type_check, coerce_fn | None, error_message, constraint | None)
    _QUEEN_FIELDS = (
        ("cooldown", (int, float), float, "must be a non-negative number", lambda v: v >= 0),
        ("enabled", (bool,), None, "must be boolean", None),
        ("system_prompt", (str,), None, "must be a string", None),
        (
            "min_confidence",
            (int, float),
            float,
            "must be a number between 0.0 and 1.0",
            lambda v: 0.0 <= v <= 1.0,
        ),
        ("max_session_calls", (int,), None, "must be >= 1", lambda v: v >= 1),
        ("max_session_age", (int, float), float, "must be > 0", lambda v: v > 0),
        ("auto_assign_tasks", (bool,), None, "must be boolean", None),
    )

    def _apply_queen_scalars(self, qn: dict[str, Any]) -> None:
        """Validate and apply flat queen fields."""
        cfg = self._config.queen
        for key, types, coerce, msg, check in self._QUEEN_FIELDS:
            if key not in qn:
                continue
            val = qn[key]
            if not isinstance(val, types):
                raise ValueError(f"queen.{key} {msg}")
            if check and not check(val):
                raise ValueError(f"queen.{key} {msg}")
            setattr(cfg, key, coerce(val) if coerce else val)

    def _apply_queen(self, qn: dict[str, Any]) -> None:
        """Validate and apply queen section of a config update."""
        self._apply_queen_scalars(qn)
        if "oversight" in qn and isinstance(qn["oversight"], dict):
            self._apply_queen_oversight(qn["oversight"])

    @staticmethod
    def _validate_string_list(prefix: str, val: object) -> list[str]:
        """Validate that ``val`` is a ``list[str]`` and return it as such."""
        if not isinstance(val, list) or not all(isinstance(e, str) for e in val):
            raise ValueError(f"{prefix} must be a list of strings")
        return list(val)

    def _apply_notifications(self, nt: dict[str, Any]) -> None:
        """Validate and apply notifications section of a config update.

        Handles the full ``NotifyConfig`` schema the dashboard sends:
        ``terminal_bell``, ``desktop``, ``debounce_seconds``,
        ``desktop_events``, ``terminal_events``, ``templates``,
        ``webhook.{url,events}``, and the entire ``email`` block.

        Reported in #328 (Bug C): the previous implementation only
        consumed three top-level scalars and silently discarded
        everything else.  Operators editing SMTP settings in the
        dashboard saw the toast "saved" but the values never reached
        ``save_config_to_db`` — after a restart the page rendered the
        defaults again, looking like a load-time bug while the actual
        defect was here in the apply path.
        """
        cfg = self._config.notifications
        for key in ("terminal_bell", "desktop"):
            if key in nt:
                if not isinstance(nt[key], bool):
                    raise ValueError(f"notifications.{key} must be boolean")
                setattr(cfg, key, nt[key])
        if "debounce_seconds" in nt:
            if not isinstance(nt["debounce_seconds"], (int, float)) or nt["debounce_seconds"] < 0:
                raise ValueError("notifications.debounce_seconds must be >= 0")
            cfg.debounce_seconds = nt["debounce_seconds"]
        for key in ("desktop_events", "terminal_events"):
            if key in nt:
                setattr(cfg, key, self._validate_string_list(f"notifications.{key}", nt[key]))
        if "templates" in nt:
            val = nt["templates"]
            if not isinstance(val, dict):
                raise ValueError("notifications.templates must be an object")
            cfg.templates = {str(k): str(v) for k, v in val.items()}
        if "webhook" in nt:
            self._apply_notifications_webhook(nt["webhook"])
        if "email" in nt:
            self._apply_notifications_email(nt["email"])

    def _apply_notifications_webhook(self, wh: object) -> None:
        """Validate and apply notifications.webhook sub-section."""
        if not isinstance(wh, dict):
            raise ValueError("notifications.webhook must be an object")
        cfg = self._config.notifications.webhook
        if "url" in wh:
            if not isinstance(wh["url"], str):
                raise ValueError("notifications.webhook.url must be a string")
            cfg.url = wh["url"].strip()
        if "events" in wh:
            cfg.events = self._validate_string_list("notifications.webhook.events", wh["events"])

    _EMAIL_BOOL_KEYS = ("enabled", "use_tls")
    _EMAIL_STRING_KEYS = ("smtp_host", "smtp_user", "smtp_password", "from_address")

    def _apply_notifications_email_scalars(self, em: dict[str, Any]) -> None:
        """Apply email bool/string/int scalar fields."""
        cfg = self._config.notifications.email
        for key in self._EMAIL_BOOL_KEYS:
            if key in em:
                if not isinstance(em[key], bool):
                    raise ValueError(f"notifications.email.{key} must be boolean")
                setattr(cfg, key, em[key])
        for key in self._EMAIL_STRING_KEYS:
            if key in em:
                if not isinstance(em[key], str):
                    raise ValueError(f"notifications.email.{key} must be a string")
                setattr(cfg, key, em[key])
        if "smtp_port" in em:
            val = em["smtp_port"]
            if not isinstance(val, int) or not (1 <= val <= 65535):
                raise ValueError("notifications.email.smtp_port must be 1-65535")
            cfg.smtp_port = val

    def _apply_notifications_email(self, em: object) -> None:
        """Validate and apply notifications.email sub-section."""
        if not isinstance(em, dict):
            raise ValueError("notifications.email must be an object")
        self._apply_notifications_email_scalars(em)
        cfg = self._config.notifications.email
        if "to_addresses" in em:
            cfg.to_addresses = self._validate_string_list(
                "notifications.email.to_addresses", em["to_addresses"]
            )
        if "events" in em:
            cfg.events = self._validate_string_list("notifications.email.events", em["events"])

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
        self._config.workflows = cleaned
        from swarm.tasks.workflows import apply_config_overrides

        apply_config_overrides(cleaned)

    def _apply_test(self, ts: dict[str, Any]) -> None:
        """Validate and apply test section of a config update."""
        cfg = self._config.test
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
            group_names = {g.name.lower() for g in self._config.groups}
            if dg.lower() not in group_names:
                raise ValueError(f"default_group '{dg}' does not match any defined group")
        self._config.default_group = dg

    def _apply_worker_identity(
        self, wc: WorkerConfig, wdata: dict[str, Any], original_name: str
    ) -> None:
        """Apply name/path changes to a worker config and sync live worker."""
        from swarm.server.helpers import validate_worker_name

        live_name: str | None = None
        live_path: str | None = None

        if "name" in wdata and isinstance(wdata["name"], str):
            new_name = wdata["name"].strip()
            if new_name and new_name != wc.name:
                if err := validate_worker_name(new_name):
                    raise ValueError(err)
                if self._config.get_worker(new_name):
                    raise ValueError(f"Cannot rename '{wc.name}' to '{new_name}': already exists")
                live_name = new_name
                wc.name = new_name
        if "path" in wdata and isinstance(wdata["path"], str):
            new_path = wdata["path"].strip()
            if new_path and new_path != wc.path:
                live_path = new_path
                wc.path = new_path

        if live_name or live_path:
            svc = self._get_worker_svc()
            if svc and svc.get_worker(original_name):
                svc.update_worker(original_name, name=live_name, path=live_path)

    def _apply_worker_entry(
        self,
        wc: WorkerConfig,
        wdata: dict[str, Any],
        valid_providers: set[str],
        original_name: str,
    ) -> None:
        """Apply a single worker's config update (description, provider, name, path)."""
        if "description" in wdata and isinstance(wdata["description"], str):
            wc.description = wdata["description"]
        if "provider" in wdata:
            prov = wdata["provider"] if isinstance(wdata["provider"], str) else ""
            if prov and prov not in valid_providers:
                raise ValueError(f"Worker '{wc.name}' has invalid provider '{prov}'")
            wc.provider = prov
        self._apply_worker_identity(wc, wdata, original_name)

    def _apply_workers(self, workers: dict[str, Any]) -> None:
        """Validate and apply worker descriptions, providers, names, and paths."""
        from swarm.providers import get_valid_providers

        valid = get_valid_providers()
        for wname, wdata in workers.items():
            wc = self._config.get_worker(wname)
            if not wc:
                continue
            if isinstance(wdata, str):
                wc.description = wdata
            elif isinstance(wdata, dict):
                self._apply_worker_entry(wc, wdata, valid, wname)

    def _apply_scalars(self, body: dict[str, Any]) -> None:
        """Apply workers, default_group, scalars, and graph settings."""
        from swarm.providers import get_valid_providers

        valid = get_valid_providers()
        if "workers" in body and isinstance(body["workers"], dict):
            self._apply_workers(body["workers"])
        if "provider" in body:
            prov = body["provider"]
            if isinstance(prov, str) and prov in valid:
                self._config.provider = prov
            elif prov:
                raise ValueError(f"Invalid global provider '{prov}'")
        if "default_group" in body:
            self._apply_default_group(body["default_group"])
        for key in ("session_name", "projects_dir", "log_level"):
            if key in body:
                setattr(self._config, key, body[key])
        for key, attr, default in (
            ("graph_client_id", "graph_client_id", ""),
            ("graph_tenant_id", "graph_tenant_id", "common"),
            ("graph_client_secret", "graph_client_secret", ""),
        ):
            if key in body and isinstance(body[key], str):
                val = body[key].strip() or default
                setattr(self._config, attr, val)
        self._apply_buttons(body)

    def _apply_buttons(self, body: dict[str, Any]) -> None:
        """Apply tool_buttons and action_buttons from the request body."""
        if "tool_buttons" in body and isinstance(body["tool_buttons"], list):
            from swarm.config import ToolButtonConfig

            self._config.tool_buttons = [
                ToolButtonConfig(label=b["label"], command=b.get("command", ""))
                for b in body["tool_buttons"]
                if isinstance(b, dict) and b.get("label")
            ]
        if "action_buttons" in body and isinstance(body["action_buttons"], list):
            from swarm.config import ActionButtonConfig

            self._config.action_buttons = [
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

            self._config.task_buttons = [
                TaskButtonConfig(
                    label=b["label"],
                    action=b.get("action", ""),
                    show_mobile=b.get("show_mobile", True),
                    show_desktop=b.get("show_desktop", True),
                )
                for b in body["task_buttons"]
                if isinstance(b, dict) and b.get("label") and b.get("action")
            ]

    @staticmethod
    def _parse_tuning_from_entry(prefix: str, entry: dict[str, object]) -> object:
        """Parse and validate ProviderTuning fields from a dict."""
        from swarm.config import _TUNING_FIELDS, ProviderTuning, _parse_tuning

        if not any(entry.get(k) for k in _TUNING_FIELDS):
            return ProviderTuning()
        tuning = _parse_tuning(entry)
        # Validate regex patterns
        for field_name in (
            "idle_pattern",
            "busy_pattern",
            "choice_pattern",
            "user_question_pattern",
            "safe_patterns",
        ):
            val = getattr(tuning, field_name, "")
            if val:
                try:
                    _re.compile(val)
                except _re.error as exc:
                    raise ValueError(f"{prefix}.{field_name}: invalid regex: {exc}") from exc
        return tuning

    def _apply_llms(self, llms_raw: object) -> None:
        """Validate and apply custom LLM providers from a config update."""
        if not isinstance(llms_raw, list):
            raise ValueError("llms must be a list")
        from swarm.config import CustomLLMConfig
        from swarm.providers import ProviderType, register_custom_providers

        builtin = frozenset(p.value for p in ProviderType)
        parsed: list[CustomLLMConfig] = []
        seen: set[str] = set()
        for i, entry in enumerate(llms_raw):
            if not isinstance(entry, dict):
                raise ValueError(f"llms[{i}] must be an object")
            name = entry.get("name", "").strip()
            if not name:
                raise ValueError(f"llms[{i}]: name is required")
            if name in builtin:
                raise ValueError(f"llms[{i}]: name '{name}' collides with built-in provider")
            if name in seen:
                raise ValueError(f"llms[{i}]: duplicate name '{name}'")
            seen.add(name)
            command = entry.get("command", [])
            if isinstance(command, str):
                command = command.split()
            if not command:
                raise ValueError(f"llms[{i}]: command is required")
            display_name = entry.get("display_name", "").strip()
            tuning = self._parse_tuning_from_entry(f"llms[{i}]", entry)
            parsed.append(
                CustomLLMConfig(
                    name=name,
                    command=command,
                    display_name=display_name,
                    tuning=tuning,
                )
            )
        self._config.custom_llms = parsed
        register_custom_providers(parsed)
        self._invalidate_provider_cache()

    def _apply_provider_overrides(self, overrides_raw: object) -> None:
        """Validate and apply provider tuning overrides for built-in providers."""
        if not isinstance(overrides_raw, dict):
            raise ValueError("provider_overrides must be an object")
        from swarm.config import ProviderTuning
        from swarm.providers import get_valid_providers, register_provider_overrides

        valid = get_valid_providers()
        parsed: dict[str, ProviderTuning] = {}
        for pname, pdata in overrides_raw.items():
            if pname not in valid:
                raise ValueError(f"provider_overrides: unknown provider '{pname}'")
            if not isinstance(pdata, dict):
                raise ValueError(f"provider_overrides.{pname} must be an object")
            tuning = self._parse_tuning_from_entry(
                f"provider_overrides.{pname}",
                pdata,
            )
            if tuning.has_tuning():
                parsed[pname] = tuning
        self._config.provider_overrides = parsed
        register_provider_overrides(parsed)
        self._invalidate_provider_cache()

    def _apply_coordination(self, co: dict[str, Any]) -> None:
        """Validate and apply coordination section of a config update."""
        cfg = self._config.coordination
        if "mode" in co:
            if co["mode"] not in ("single-branch", "worktree"):
                raise ValueError("coordination.mode must be 'single-branch' or 'worktree'")
            cfg.mode = co["mode"]
        if "auto_pull" in co:
            if not isinstance(co["auto_pull"], bool):
                raise ValueError("coordination.auto_pull must be boolean")
            cfg.auto_pull = co["auto_pull"]
        if "file_ownership" in co:
            if co["file_ownership"] not in ("off", "warning", "hard-block"):
                raise ValueError(
                    "coordination.file_ownership must be 'off', 'warning', or 'hard-block'"
                )
            cfg.file_ownership = co["file_ownership"]

    _JIRA_STRING_KEYS = (
        "project",
        "import_filter",
        "import_label",
        "client_id",
        "client_secret",
        "cloud_id",
    )

    @staticmethod
    def _apply_jira_strings(cfg: object, jr: dict[str, Any], keys: tuple[str, ...]) -> None:
        """Validate and apply string fields on the Jira config."""
        for key in keys:
            if key in jr:
                if not isinstance(jr[key], str):
                    raise ValueError(f"jira.{key} must be a string")
                val = jr[key].strip()
                if not val and key in ("client_id", "client_secret"):
                    continue
                setattr(cfg, key, val)

    def _apply_jira(self, jr: dict[str, Any]) -> None:
        """Validate and apply jira section of a config update."""
        cfg = self._config.jira
        if "enabled" in jr:
            if not isinstance(jr["enabled"], bool):
                raise ValueError("jira.enabled must be boolean")
            cfg.enabled = jr["enabled"]
        self._apply_jira_strings(cfg, jr, self._JIRA_STRING_KEYS)
        if "sync_interval_minutes" in jr:
            val = jr["sync_interval_minutes"]
            if not isinstance(val, (int, float)) or val <= 0:
                raise ValueError("jira.sync_interval_minutes must be > 0")
            cfg.sync_interval_minutes = float(val)
        if "lookback_days" in jr:
            val = jr["lookback_days"]
            if not isinstance(val, (int, float)) or val < 0:
                raise ValueError("jira.lookback_days must be >= 0")
            cfg.lookback_days = int(val)
        if "status_map" in jr:
            val = jr["status_map"]
            if not isinstance(val, dict):
                raise ValueError("jira.status_map must be an object")
            # Merge with defaults so empty {} doesn't wipe all mappings
            default_map = {
                "pending": "To Do",
                "in_progress": "In Progress",
                "completed": "Done",
                "failed": "To Do",
            }
            cfg.status_map = {**default_map, **{str(k): str(v) for k, v in val.items()}}

    def _apply_advanced_bools(self, body: dict[str, Any]) -> None:
        """Apply boolean advanced fields from the config body."""
        for key in ("trust_proxy",):
            if key in body:
                if not isinstance(body[key], bool):
                    raise ValueError(f"{key} must be boolean")
                setattr(self._config, key, body[key])

    def _apply_advanced(self, body: dict[str, Any]) -> None:
        """Apply top-level advanced fields."""
        if "port" in body:
            val = body["port"]
            if not isinstance(val, int) or not (1024 <= val <= 65535):
                raise ValueError("port must be integer between 1024 and 65535")
            self._config.port = val
        self._apply_advanced_bools(body)
        if "tunnel_domain" in body:
            if not isinstance(body["tunnel_domain"], str):
                raise ValueError("tunnel_domain must be a string")
            self._config.tunnel_domain = body["tunnel_domain"].strip()
        if "domain" in body:
            if not isinstance(body["domain"], str):
                raise ValueError("domain must be a string")
            self._config.domain = body["domain"].strip()
        if "terminal" in body and isinstance(body["terminal"], dict):
            t = body["terminal"]
            if "replay_scrollback" in t:
                if not isinstance(t["replay_scrollback"], bool):
                    raise ValueError("terminal.replay_scrollback must be boolean")
                self._config.terminal.replay_scrollback = t["replay_scrollback"]

    async def apply_update(self, body: dict[str, Any]) -> None:
        """Apply a partial config update from the API. Raises ValueError on invalid input."""
        if "llms" in body:
            self._apply_llms(body["llms"])
        if "provider_overrides" in body:
            self._apply_provider_overrides(body["provider_overrides"])
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
        if "coordination" in body:
            self._apply_coordination(body["coordination"])
        if "jira" in body:
            self._apply_jira(body["jira"])
        self._apply_advanced(body)

        # Rebuild integration managers if credentials changed
        self._rebuild_graph()
        self._rebuild_jira()

        # Hot-reload and save.  Only forward sync_rules=True when the
        # caller's payload genuinely carried an approval_rules update
        # (global or per-worker), so an unrelated config edit can't
        # cascade into rewriting the approval_rules table.
        rules_touched = _body_touches_approval_rules(body)
        await self.reload(self._config)
        self.save(sync_rules=rules_touched)
