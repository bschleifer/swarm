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
    """Load workers with their approval rules from DB."""
    workers: list[WorkerConfig] = []
    worker_rows = db.fetchall("SELECT * FROM workers ORDER BY sort_order, name")
    for wr in worker_rows:
        rules = db.fetchall(
            "SELECT pattern, action FROM approval_rules "
            "WHERE owner_type = 'worker' AND owner_id = ? "
            "ORDER BY sort_order",
            (wr["id"],),
        )
        approval_rules = [
            DroneApprovalRule(pattern=r["pattern"], action=r["action"]) for r in rules
        ]
        workers.append(
            WorkerConfig(
                name=wr["name"],
                path=wr["path"],
                description=wr["description"] or "",
                provider=wr["provider"] or "",
                isolation=wr["isolation"] or "",
                identity=wr["identity"] or "",
                approval_rules=approval_rules,
            )
        )
    return workers


def _load_groups(db: SwarmDB) -> list[GroupConfig]:
    """Load groups with their member workers from DB."""
    groups: list[GroupConfig] = []
    group_rows = db.fetchall("SELECT * FROM groups ORDER BY name")
    for gr in group_rows:
        member_rows = db.fetchall(
            "SELECT w.name FROM group_workers gw "
            "JOIN workers w ON gw.worker_id = w.id "
            "WHERE gw.group_id = ?",
            (gr["id"],),
        )
        groups.append(
            GroupConfig(
                name=gr["name"],
                workers=[m["name"] for m in member_rows],
            )
        )
    return groups


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

    st_data = d.get("state_thresholds", {})
    state_thresholds = (
        StateThresholds(**{k: v for k, v in st_data.items() if hasattr(StateThresholds, k)})
        if st_data
        else StateThresholds()
    )

    return DroneConfig(
        enabled=d.get("enabled", True),
        escalation_threshold=d.get("escalation_threshold", 120),
        poll_interval=d.get("poll_interval", 5),
        approval_rules=rules,
        state_thresholds=state_thresholds,
        allowed_read_paths=d.get("allowed_read_paths", []),
        context_warning_threshold=d.get("context_warning_threshold", 0.7),
        context_critical_threshold=d.get("context_critical_threshold", 0.9),
        speculation_enabled=d.get("speculation_enabled", False),
    )


def _parse_queen_config(blob: str) -> QueenConfig:
    try:
        d = json.loads(blob)
    except json.JSONDecodeError:
        return QueenConfig()
    if not isinstance(d, dict):
        return QueenConfig()

    oversight_data = d.get("oversight", {})
    oversight = (
        OversightConfig(**{k: v for k, v in oversight_data.items() if hasattr(OversightConfig, k)})
        if oversight_data
        else OversightConfig()
    )

    return QueenConfig(
        cooldown=d.get("cooldown", 30),
        enabled=d.get("enabled", True),
        system_prompt=d.get("system_prompt", ""),
        min_confidence=d.get("min_confidence", 0.8),
        max_session_calls=d.get("max_session_calls", 50),
        max_session_age=d.get("max_session_age", 1800),
        auto_assign_tasks=d.get("auto_assign_tasks", True),
        oversight=oversight,
        model=d.get("model", ""),
    )


def _parse_notify_config(blob: str) -> NotifyConfig:
    try:
        d = json.loads(blob)
    except json.JSONDecodeError:
        return NotifyConfig()
    if not isinstance(d, dict):
        return NotifyConfig()
    return NotifyConfig(**{k: v for k, v in d.items() if hasattr(NotifyConfig, k)})


def _parse_json_dataclass(blob: str, cls: type) -> Any:
    """Generic parser for simple dataclasses from JSON."""
    try:
        d = json.loads(blob)
    except json.JSONDecodeError:
        return cls()
    if not isinstance(d, dict):
        return cls()
    return cls(**{k: v for k, v in d.items() if hasattr(cls, k)})


def _parse_button_list(blob: str, cls: type) -> list:
    try:
        items = json.loads(blob)
    except json.JSONDecodeError:
        return []
    if not isinstance(items, list):
        return []
    result = []
    for item in items:
        if isinstance(item, dict):
            try:
                result.append(cls(**{k: v for k, v in item.items() if hasattr(cls, k)}))
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
    result = []
    for item in items:
        if isinstance(item, dict):
            try:
                result.append(
                    CustomLLMConfig(
                        **{k: v for k, v in item.items() if hasattr(CustomLLMConfig, k)}
                    )
                )
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
    result: dict[str, ProviderTuning] = {}
    for name, tuning_data in d.items():
        if isinstance(tuning_data, dict):
            try:
                result[name] = ProviderTuning(
                    **{k: v for k, v in tuning_data.items() if hasattr(ProviderTuning, k)}
                )
            except TypeError:
                continue
    return result
