"""SQLite-backed config store — replaces YAML config file.

Workers and groups are stored in normalized tables.
Complex nested configs (drones, queen, notifications, etc.) are
stored as JSON values in the config key-value table.
"""

from __future__ import annotations

import json
import time
import uuid
from typing import TYPE_CHECKING, Any

from swarm.config.models import (
    ActionButtonConfig,
    CoordinationConfig,
    CustomLLMConfig,
    DroneApprovalRule,
    DroneConfig,
    EmailConfig,
    GroupConfig,
    HiveConfig,
    JiraConfig,
    NotifyConfig,
    OversightConfig,
    ProviderTuning,
    QueenConfig,
    ResourceConfig,
    StateThresholds,
    TaskButtonConfig,
    TerminalConfig,
    TestConfig,
    ToolButtonConfig,
    WebhookConfig,
    WorkerConfig,
)
from swarm.logging import get_logger

if TYPE_CHECKING:
    from swarm.db.core import SwarmDB

_log = get_logger("db.config_store")

# Config keys stored as JSON blobs in the config table
_JSON_KEYS = {
    "drones",
    "queen",
    "notifications",
    "coordination",
    "jira",
    "test",
    "terminal",
    "resources",
    "workflows",
    "tool_buttons",
    "action_buttons",
    "task_buttons",
    "custom_llms",
    "provider_overrides",
}

# Scalar config keys
_SCALAR_KEYS = {
    "session_name",
    "projects_dir",
    "provider",
    "default_group",
    "watch_interval",
    "log_level",
    "log_file",
    "port",
    "daemon_url",
    "api_password",
    "graph_client_id",
    "graph_tenant_id",
    "graph_client_secret",
    "auto_mode",
    "trust_proxy",
    "tunnel_domain",
    "domain",
}


def load_config_from_db(db: SwarmDB) -> HiveConfig | None:
    """Load HiveConfig from the database. Returns None if not migrated yet."""
    row = db.fetchone("SELECT COUNT(*) FROM workers")
    if not row or row[0] == 0:
        scalar = db.fetchone("SELECT COUNT(*) FROM config WHERE key != 'update_cache'")
        if not scalar or scalar[0] == 0:
            return None

    config = HiveConfig()
    scalars, json_blobs = _load_config_rows(db)
    _apply_scalars(config, scalars)
    config.workers = _load_workers(db)
    config.groups = _load_groups(db)

    global_rules = db.fetchall(
        "SELECT pattern, action FROM approval_rules WHERE owner_type = 'global' ORDER BY sort_order"
    )
    _apply_json_blobs(config, json_blobs, global_rules)

    config.apply_env_overrides()
    _log.info("loaded config from swarm.db (%d workers)", len(config.workers))
    return config


def _load_config_rows(db: SwarmDB) -> tuple[dict[str, str], dict[str, str]]:
    """Load all config rows, split into scalars and JSON blobs."""
    rows = db.fetchall("SELECT key, value FROM config")
    scalars: dict[str, str] = {}
    json_blobs: dict[str, str] = {}
    for r in rows:
        key, value = r["key"], r["value"]
        if key in _JSON_KEYS:
            json_blobs[key] = value or ""
        elif key in _SCALAR_KEYS:
            scalars[key] = value or ""
    return scalars, json_blobs


def _apply_scalars(config: HiveConfig, scalars: dict[str, str]) -> None:
    """Apply scalar config values to HiveConfig."""
    config.session_name = scalars.get("session_name", "swarm")
    config.projects_dir = scalars.get("projects_dir", "~/projects")
    config.provider = scalars.get("provider", "claude")
    config.default_group = scalars.get("default_group", "")
    config.watch_interval = int(scalars.get("watch_interval", "5"))
    config.log_level = scalars.get("log_level", "WARNING")
    config.log_file = scalars.get("log_file") or None
    config.port = int(scalars.get("port", "9090"))
    config.daemon_url = scalars.get("daemon_url") or None
    config.api_password = scalars.get("api_password") or None
    config.graph_client_id = scalars.get("graph_client_id", "")
    config.graph_tenant_id = scalars.get("graph_tenant_id", "common")
    config.graph_client_secret = scalars.get("graph_client_secret", "")
    config.auto_mode = scalars.get("auto_mode", "") in ("True", "true", "1")
    config.trust_proxy = scalars.get("trust_proxy", "") in (
        "True",
        "true",
        "1",
    )
    config.tunnel_domain = scalars.get("tunnel_domain", "")
    config.domain = scalars.get("domain", "")


def _load_workers(db: SwarmDB) -> list[WorkerConfig]:
    """Load workers with their approval rules from DB (single JOIN)."""
    rows = db.fetchall(
        "SELECT w.id, w.name, w.path, w.description, w.provider,"
        "       w.isolation, w.identity,"
        "       ar.pattern, ar.action"
        " FROM workers w"
        " LEFT JOIN approval_rules ar"
        "   ON ar.owner_type = 'worker' AND ar.owner_id = w.id"
        " ORDER BY w.sort_order, w.name, ar.sort_order"
    )
    workers_by_id: dict[str, WorkerConfig] = {}
    for r in rows:
        wid = r["id"]
        if wid not in workers_by_id:
            workers_by_id[wid] = WorkerConfig(
                name=r["name"],
                path=r["path"],
                description=r["description"] or "",
                provider=r["provider"] or "",
                isolation=r["isolation"] or "",
                identity=r["identity"] or "",
                approval_rules=[],
            )
        if r["pattern"] is not None:
            workers_by_id[wid].approval_rules.append(
                DroneApprovalRule(pattern=r["pattern"], action=r["action"])
            )
    return list(workers_by_id.values())


def _load_groups(db: SwarmDB) -> list[GroupConfig]:
    """Load groups with their member workers from DB (single JOIN)."""
    rows = db.fetchall(
        "SELECT g.id, g.name, w.name AS worker_name"
        " FROM groups g"
        " LEFT JOIN group_workers gw ON gw.group_id = g.id"
        " LEFT JOIN workers w ON gw.worker_id = w.id"
        " ORDER BY g.name"
    )
    groups_by_id: dict[str, GroupConfig] = {}
    for r in rows:
        gid = r["id"]
        if gid not in groups_by_id:
            groups_by_id[gid] = GroupConfig(name=r["name"], workers=[])
        if r["worker_name"] is not None:
            groups_by_id[gid].workers.append(r["worker_name"])
    return list(groups_by_id.values())


def _apply_json_blobs(
    config: HiveConfig,
    json_blobs: dict[str, str],
    global_rules: list[Any],
) -> None:
    """Apply JSON blob config sections to HiveConfig."""
    _apply_special_blobs(config, json_blobs, global_rules)
    _apply_generic_blobs(config, json_blobs)


def _apply_special_blobs(
    config: HiveConfig,
    json_blobs: dict[str, str],
    global_rules: list[Any],
) -> None:
    """Apply config sections that need custom parsers."""
    if "drones" in json_blobs:
        config.drones = _parse_drone_config(json_blobs["drones"], global_rules)
    if "queen" in json_blobs:
        config.queen = _parse_queen_config(json_blobs["queen"])
    if "notifications" in json_blobs:
        config.notifications = _parse_notify_config(json_blobs["notifications"])
    if "workflows" in json_blobs:
        try:
            config.workflows = json.loads(json_blobs["workflows"])
        except json.JSONDecodeError:
            pass
    if "custom_llms" in json_blobs:
        config.custom_llms = _parse_custom_llms(json_blobs["custom_llms"])
    if "provider_overrides" in json_blobs:
        config.provider_overrides = _parse_provider_overrides(json_blobs["provider_overrides"])


# Blob key → (config attr, parser function, parser arg type)
_DATACLASS_BLOBS: dict[str, tuple[str, type]] = {
    "coordination": ("coordination", CoordinationConfig),
    "jira": ("jira", JiraConfig),
    "test": ("test", TestConfig),
    "terminal": ("terminal", TerminalConfig),
    "resources": ("resources", ResourceConfig),
}
_BUTTON_BLOBS: dict[str, tuple[str, type]] = {
    "tool_buttons": ("tool_buttons", ToolButtonConfig),
    "action_buttons": ("action_buttons", ActionButtonConfig),
    "task_buttons": ("task_buttons", TaskButtonConfig),
}


def _apply_generic_blobs(config: HiveConfig, json_blobs: dict[str, str]) -> None:
    """Apply config sections that use generic dataclass/button parsers."""
    for key, (attr, cls) in _DATACLASS_BLOBS.items():
        if key in json_blobs:
            setattr(config, attr, _parse_json_dataclass(json_blobs[key], cls))
    for key, (attr, cls) in _BUTTON_BLOBS.items():
        if key in json_blobs:
            setattr(config, attr, _parse_button_list(json_blobs[key], cls))


def save_config_to_db(db: SwarmDB, config: HiveConfig) -> None:
    """Save HiveConfig to the database."""
    now = time.time()

    # Save scalars
    for key in _SCALAR_KEYS:
        value = getattr(config, key, None)
        if value is None:
            value = ""
        else:
            value = str(value)
        db.execute(
            "INSERT OR REPLACE INTO config (key, value, updated_at) VALUES (?, ?, ?)",
            (key, value, now),
        )

    # Save JSON blobs
    from swarm.config.serialization import serialize_config

    full = serialize_config(config)
    for key in _JSON_KEYS:
        if key in full:
            db.execute(
                "INSERT OR REPLACE INTO config (key, value, updated_at) VALUES (?, ?, ?)",
                (key, json.dumps(full[key]), now),
            )

    # Save workers (normalized)
    _save_workers(db, config.workers, now)

    # Save groups (normalized)
    _save_groups(db, config.groups, config.workers, now)

    # Save global approval rules
    db.delete("approval_rules", "owner_type = 'global'", ())
    for i, rule in enumerate(config.drones.approval_rules):
        db.execute(
            "INSERT INTO approval_rules "
            "(owner_type, owner_id, pattern, action, sort_order) "
            "VALUES ('global', NULL, ?, ?, ?)",
            (rule.pattern, rule.action, i),
        )

    db.commit()
    _log.info("saved config to swarm.db (%d workers)", len(config.workers))


def _save_workers(db: SwarmDB, workers: list[WorkerConfig], now: float) -> None:
    """Sync workers table with config worker list."""
    # Get existing worker IDs by name
    existing = {}
    for r in db.fetchall("SELECT id, name FROM workers"):
        existing[r["name"]] = r["id"]

    seen_names: set[str] = set()
    for i, wc in enumerate(workers):
        seen_names.add(wc.name)
        wid = existing.get(wc.name, uuid.uuid4().hex[:16])
        db.execute(
            "INSERT OR REPLACE INTO workers "
            "(id, name, path, description, provider, isolation, "
            "identity, sort_order, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                wid,
                wc.name,
                wc.path,
                wc.description,
                wc.provider,
                wc.isolation,
                wc.identity,
                i,
                now,
            ),
        )
        # Save worker-specific approval rules
        db.delete(
            "approval_rules",
            "owner_type = 'worker' AND owner_id = ?",
            (wid,),
        )
        for j, rule in enumerate(wc.approval_rules):
            db.execute(
                "INSERT INTO approval_rules "
                "(owner_type, owner_id, pattern, action, sort_order) "
                "VALUES ('worker', ?, ?, ?, ?)",
                (wid, rule.pattern, rule.action, j),
            )

    # Remove workers no longer in config
    for name, wid in existing.items():
        if name not in seen_names:
            db.delete("workers", "id = ?", (wid,))


def _save_groups(
    db: SwarmDB,
    groups: list[GroupConfig],
    workers: list[WorkerConfig],
    now: float,
) -> None:
    """Sync groups table with config group list."""
    # Build name → worker ID map
    worker_ids = {}
    for r in db.fetchall("SELECT id, name FROM workers"):
        worker_ids[r["name"]] = r["id"]

    existing_groups = {}
    for r in db.fetchall("SELECT id, name FROM groups"):
        existing_groups[r["name"]] = r["id"]

    seen_names: set[str] = set()
    for gc in groups:
        seen_names.add(gc.name)
        gid = existing_groups.get(gc.name, uuid.uuid4().hex[:16])
        db.execute(
            "INSERT OR REPLACE INTO groups (id, name, label) VALUES (?, ?, ?)",
            (gid, gc.name, ""),
        )
        # Sync members
        db.delete("group_workers", "group_id = ?", (gid,))
        for wname in gc.workers:
            wid = worker_ids.get(wname)
            if wid:
                db.execute(
                    "INSERT OR IGNORE INTO group_workers (group_id, worker_id) VALUES (?, ?)",
                    (gid, wid),
                )

    for name, gid in existing_groups.items():
        if name not in seen_names:
            db.delete("groups", "id = ?", (gid,))


# ---------------------------------------------------------------------------
# JSON blob parsers
# ---------------------------------------------------------------------------


def _parse_drone_config(blob: str, global_rules: list[Any]) -> DroneConfig:
    """Parse DroneConfig from JSON blob + global approval rules."""
    try:
        d = json.loads(blob)
    except json.JSONDecodeError:
        return DroneConfig()
    if not isinstance(d, dict):
        return DroneConfig()

    rules = [DroneApprovalRule(pattern=r["pattern"], action=r["action"]) for r in global_rules]

    # Handle nested dataclass before generic field filter
    st_data = d.pop("state_thresholds", {})
    if st_data:
        st_valid = StateThresholds.__dataclass_fields__
        state_thresholds = StateThresholds(**{k: v for k, v in st_data.items() if k in st_valid})
    else:
        state_thresholds = StateThresholds()

    # Drop the raw approval_rules from the blob — we use DB-stored rules
    d.pop("approval_rules", None)

    valid = DroneConfig.__dataclass_fields__
    kwargs: dict[str, Any] = {k: v for k, v in d.items() if k in valid}
    kwargs["approval_rules"] = rules
    kwargs["state_thresholds"] = state_thresholds
    return DroneConfig(**kwargs)


def _parse_queen_config(blob: str) -> QueenConfig:
    try:
        d = json.loads(blob)
    except json.JSONDecodeError:
        return QueenConfig()
    if not isinstance(d, dict):
        return QueenConfig()

    # Handle nested dataclass before generic field filter
    oversight_data = d.pop("oversight", {})
    if oversight_data:
        ov_valid = OversightConfig.__dataclass_fields__
        oversight = OversightConfig(**{k: v for k, v in oversight_data.items() if k in ov_valid})
    else:
        oversight = OversightConfig()

    valid = QueenConfig.__dataclass_fields__
    kwargs: dict[str, Any] = {k: v for k, v in d.items() if k in valid}
    kwargs["oversight"] = oversight
    return QueenConfig(**kwargs)


def _parse_notify_config(blob: str) -> NotifyConfig:
    try:
        d = json.loads(blob)
    except json.JSONDecodeError:
        return NotifyConfig()
    if not isinstance(d, dict):
        return NotifyConfig()

    # Parse nested dataclasses before passing to NotifyConfig
    webhook_data = d.pop("webhook", None)
    email_data = d.pop("email", None)

    valid = NotifyConfig.__dataclass_fields__
    kwargs: dict[str, Any] = {k: v for k, v in d.items() if k in valid}

    if isinstance(webhook_data, dict):
        wh_valid = WebhookConfig.__dataclass_fields__
        kwargs["webhook"] = WebhookConfig(
            **{k: v for k, v in webhook_data.items() if k in wh_valid}
        )
    if isinstance(email_data, dict):
        em_valid = EmailConfig.__dataclass_fields__
        kwargs["email"] = EmailConfig(**{k: v for k, v in email_data.items() if k in em_valid})

    return NotifyConfig(**kwargs)


def _parse_json_dataclass(blob: str, cls: type) -> Any:
    """Generic parser for simple dataclasses from JSON."""
    try:
        d = json.loads(blob)
    except json.JSONDecodeError:
        return cls()
    if not isinstance(d, dict):
        return cls()
    valid = cls.__dataclass_fields__
    return cls(**{k: v for k, v in d.items() if k in valid})


def _parse_button_list(blob: str, cls: type) -> list:
    try:
        items = json.loads(blob)
    except json.JSONDecodeError:
        return []
    if not isinstance(items, list):
        return []
    valid = cls.__dataclass_fields__
    result = []
    for item in items:
        if isinstance(item, dict):
            try:
                result.append(cls(**{k: v for k, v in item.items() if k in valid}))
            except TypeError:
                continue
    return result


def _parse_custom_llms(blob: str) -> list[CustomLLMConfig]:
    try:
        items = json.loads(blob)
    except json.JSONDecodeError:
        return []
    if not isinstance(items, list):
        return []
    valid = CustomLLMConfig.__dataclass_fields__
    result = []
    for item in items:
        if isinstance(item, dict):
            try:
                result.append(CustomLLMConfig(**{k: v for k, v in item.items() if k in valid}))
            except TypeError:
                continue
    return result


def _parse_provider_overrides(
    blob: str,
) -> dict[str, ProviderTuning]:
    try:
        d = json.loads(blob)
    except json.JSONDecodeError:
        return {}
    if not isinstance(d, dict):
        return {}
    valid = ProviderTuning.__dataclass_fields__
    result: dict[str, ProviderTuning] = {}
    for name, tuning_data in d.items():
        if isinstance(tuning_data, dict):
            try:
                result[name] = ProviderTuning(
                    **{k: v for k, v in tuning_data.items() if k in valid}
                )
            except TypeError:
                continue
    return result
