"""YAML configuration loader for hive definitions."""

from __future__ import annotations

import functools
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


class ConfigError(Exception):
    """Raised when swarm.yaml is invalid."""


@dataclass
class DroneApprovalRule:
    """A pattern→action rule for drone choice menu handling."""

    pattern: str  # regex matched against choice menu text
    action: str = "approve"  # "approve" or "escalate"


@dataclass
class DroneConfig:
    """Background drones settings (``drones:`` section in swarm.yaml)."""

    enabled: bool = True
    escalation_threshold: float = 15.0
    poll_interval: float = 5.0
    auto_approve_yn: bool = False
    max_revive_attempts: int = 3
    max_poll_failures: int = 5
    max_idle_interval: float = 30.0
    auto_stop_on_complete: bool = True
    approval_rules: list[DroneApprovalRule] = field(default_factory=list)
    # Directory prefixes that are always safe to read from (e.g. "~/.swarm/uploads/").
    # Read operations matching these paths are auto-approved regardless of approval_rules.
    allowed_read_paths: list[str] = field(default_factory=list)


@dataclass
class QueenConfig:
    """Queen conductor settings (``queen:`` section in swarm.yaml)."""

    cooldown: float = 30.0
    enabled: bool = True
    system_prompt: str = ""
    min_confidence: float = 0.7


@dataclass
class NotifyConfig:
    """Notification settings (``notifications:`` section in swarm.yaml)."""

    terminal_bell: bool = True
    desktop: bool = True
    debounce_seconds: float = 5.0


@dataclass
class WorkerConfig:
    name: str
    path: str
    description: str = ""

    @functools.cached_property
    def resolved_path(self) -> Path:
        return Path(self.path).expanduser().resolve()


@dataclass
class GroupConfig:
    name: str
    workers: list[str]


@dataclass
class HiveConfig:
    session_name: str = "swarm"
    projects_dir: str = "~/projects"
    workers: list[WorkerConfig] = field(default_factory=list)
    groups: list[GroupConfig] = field(default_factory=list)
    default_group: str = ""
    panes_per_window: int = 9
    watch_interval: int = 5
    source_path: str | None = None
    drones: DroneConfig = field(default_factory=DroneConfig)
    queen: QueenConfig = field(default_factory=QueenConfig)
    notifications: NotifyConfig = field(default_factory=NotifyConfig)
    # Skill overrides per task type (e.g. {"bug": "/fix-and-ship", "feature": "/feature"}).
    # Keys are TaskType values: bug, feature, verify, chore.
    # Set a value to null/empty to disable skill invocation for that type.
    workflows: dict[str, str] = field(default_factory=dict)
    log_level: str = "WARNING"
    log_file: str | None = None
    port: int = 9090  # web UI / API server port
    daemon_url: str | None = None  # e.g. "http://localhost:9090" — TUI connects via API
    api_password: str | None = None  # password for web UI config-mutating endpoints
    graph_client_id: str = ""  # Azure AD app client ID for Microsoft Graph
    graph_tenant_id: str = "common"  # Azure AD tenant ID (or "common")

    def get_group(self, name: str) -> list[WorkerConfig]:
        name_lower = name.lower()
        for g in self.groups:
            if g.name.lower() == name_lower:
                members = {m.lower() for m in g.workers}
                return [w for w in self.workers if w.name.lower() in members]
        raise ValueError(f"Unknown group: {name}")

    def get_worker(self, name: str) -> WorkerConfig | None:
        name_lower = name.lower()
        for w in self.workers:
            if w.name.lower() == name_lower:
                return w
        return None

    def validate(self) -> list[str]:  # noqa: C901
        """Validate config, returning a list of error messages (empty = valid)."""
        errors: list[str] = []

        if not self.workers:
            errors.append("No workers defined — add at least one worker to swarm.yaml")

        # Check for duplicate worker names
        names = [w.name.lower() for w in self.workers]
        seen: set[str] = set()
        for n in names:
            if n in seen:
                errors.append(f"Duplicate worker name: '{n}'")
            seen.add(n)

        # Check worker paths exist
        for w in self.workers:
            p = w.resolved_path
            if not p.exists():
                errors.append(f"Worker '{w.name}' path does not exist: {p}")

        # Check group references
        valid_names = {w.name.lower() for w in self.workers}
        for g in self.groups:
            for member in g.workers:
                if member.lower() not in valid_names:
                    errors.append(f"Group '{g.name}' references unknown worker: '{member}'")

        # Check duplicate group names
        gnames = [g.name.lower() for g in self.groups]
        seen_g: set[str] = set()
        for gn in gnames:
            if gn in seen_g:
                errors.append(f"Duplicate group name: '{gn}'")
            seen_g.add(gn)

        # Validate default_group references an existing group
        if self.default_group:
            group_names_set = {g.name.lower() for g in self.groups}
            if self.default_group.lower() not in group_names_set:
                errors.append(
                    f"default_group '{self.default_group}' does not match any defined group"
                )

        # Validate log_file parent directory
        if self.log_file:
            log_parent = Path(self.log_file).expanduser().parent
            if not log_parent.exists():
                errors.append(f"Log file parent directory does not exist: {log_parent}")

        # Range checks for numeric fields
        if self.watch_interval <= 0:
            errors.append("watch_interval must be > 0")
        if self.panes_per_window <= 0:
            errors.append("panes_per_window must be > 0")
        if self.drones.poll_interval <= 0:
            errors.append("drones.poll_interval must be > 0")
        if self.drones.escalation_threshold <= 0:
            errors.append("drones.escalation_threshold must be > 0")
        if self.queen.cooldown < 0:
            errors.append("queen.cooldown must be >= 0")
        if not (0.0 <= self.queen.min_confidence <= 1.0):
            errors.append("queen.min_confidence must be between 0.0 and 1.0")

        # Validate approval rule patterns
        for i, rule in enumerate(self.drones.approval_rules):
            try:
                re.compile(rule.pattern)
            except re.error as exc:
                errors.append(f"drones.approval_rules[{i}]: invalid regex '{rule.pattern}': {exc}")
            if rule.action not in ("approve", "escalate"):
                errors.append(
                    f"drones.approval_rules[{i}]: action must be 'approve' or 'escalate', "
                    f"got '{rule.action}'"
                )

        return errors

    def apply_env_overrides(self) -> None:
        """Apply environment variable overrides."""

        if val := os.environ.get("SWARM_SESSION_NAME"):
            self.session_name = val
        if val := os.environ.get("SWARM_WATCH_INTERVAL"):
            try:
                self.watch_interval = int(val)
            except ValueError:
                pass
        if val := os.environ.get("SWARM_DAEMON_URL"):
            self.daemon_url = val


def load_config(path: str | None = None) -> HiveConfig:
    """Load config from explicit path, swarm.yaml in CWD, or ~/.config/swarm/config.yaml."""
    candidates = []
    if path:
        candidates.append(Path(path))
    else:
        candidates.append(Path.cwd() / "swarm.yaml")
        candidates.append(Path.home() / ".config" / "swarm" / "config.yaml")

    for candidate in candidates:
        if candidate.exists():
            return _parse_config(candidate)

    # Return default config with auto-detected workers
    return _auto_detect_config()


def _parse_config(path: Path) -> HiveConfig:
    with open(path) as f:
        data = yaml.safe_load(f) or {}

    try:
        workers = [
            WorkerConfig(name=w["name"], path=w["path"], description=w.get("description", ""))
            for w in data.get("workers", [])
        ]
    except (KeyError, TypeError) as exc:
        raise ConfigError(f"Worker entry missing required field 'name' or 'path': {exc}") from exc

    try:
        groups = [GroupConfig(name=g["name"], workers=g["workers"]) for g in data.get("groups", [])]
    except (KeyError, TypeError) as exc:
        raise ConfigError(
            f"Group entry must be a dict with 'name' and 'workers' fields: {exc}"
        ) from exc

    # Parse drones section
    drones_data = data.get("drones", {})
    approval_rules_raw = drones_data.get("approval_rules", [])
    approval_rules = [
        DroneApprovalRule(
            pattern=r.get("pattern", ""),
            action=r.get("action", "approve"),
        )
        for r in approval_rules_raw
        if isinstance(r, dict)
    ]
    drones = DroneConfig(
        enabled=drones_data.get("enabled", True),
        escalation_threshold=drones_data.get("escalation_threshold", 15.0),
        poll_interval=drones_data.get("poll_interval", 5.0),
        auto_approve_yn=drones_data.get("auto_approve_yn", False),
        max_revive_attempts=drones_data.get("max_revive_attempts", 3),
        max_poll_failures=drones_data.get("max_poll_failures", 5),
        max_idle_interval=drones_data.get("max_idle_interval", 30.0),
        auto_stop_on_complete=drones_data.get("auto_stop_on_complete", True),
        approval_rules=approval_rules,
        allowed_read_paths=drones_data.get("allowed_read_paths", []),
    )

    # Parse queen section
    queen_data = data.get("queen", {})
    queen = QueenConfig(
        cooldown=queen_data.get("cooldown", 30.0),
        enabled=queen_data.get("enabled", True),
        system_prompt=queen_data.get("system_prompt", ""),
        min_confidence=queen_data.get("min_confidence", 0.7),
    )

    # Parse notifications section
    notify_data = data.get("notifications", {})
    notifications = NotifyConfig(
        terminal_bell=notify_data.get("terminal_bell", True),
        desktop=notify_data.get("desktop", True),
        debounce_seconds=notify_data.get("debounce_seconds", 5.0),
    )

    # Parse integrations section
    integrations = data.get("integrations", {})
    graph_data = integrations.get("graph", {}) if isinstance(integrations, dict) else {}

    # Parse workflows section — maps task type names to skill commands
    workflows_raw = data.get("workflows", {})
    workflows = (
        {k: str(v) for k, v in workflows_raw.items() if isinstance(k, str) and v}
        if isinstance(workflows_raw, dict)
        else {}
    )

    return HiveConfig(
        session_name=data.get("session_name", "swarm"),
        projects_dir=data.get("projects_dir", "~/projects"),
        workers=workers,
        groups=groups,
        default_group=data.get("default_group", ""),
        panes_per_window=data.get("panes_per_window", 9),
        watch_interval=data.get("watch_interval", 5),
        source_path=str(path),
        drones=drones,
        queen=queen,
        notifications=notifications,
        workflows=workflows,
        log_level=data.get("log_level", "WARNING"),
        log_file=data.get("log_file"),
        port=data.get("port", 9090),
        daemon_url=data.get("daemon_url"),
        api_password=data.get("api_password"),
        graph_client_id=graph_data.get("client_id", ""),
        graph_tenant_id=graph_data.get("tenant_id", "common"),
    )


def _auto_detect_config() -> HiveConfig:
    """Auto-detect git repos in ~/projects/ as workers."""
    projects_dir = Path.home() / "projects"
    projects = discover_projects(projects_dir)
    workers = [WorkerConfig(name=name, path=path) for name, path in projects]

    return HiveConfig(
        workers=workers,
        groups=[GroupConfig(name="all", workers=[w.name for w in workers])],
    )


def discover_projects(scan_dir: Path) -> list[tuple[str, str]]:
    """Scan a directory for git repos. Returns list of (name, path) tuples."""
    projects: list[tuple[str, str]] = []
    if not scan_dir.is_dir():
        return projects
    for child in sorted(scan_dir.iterdir()):
        if child.is_dir() and (child / ".git").exists():
            projects.append((child.name, str(child)))
    return projects


def write_config(
    output_path: str,
    workers: list[tuple[str, str]],
    groups: dict[str, list[str]],
    projects_dir: str,
) -> None:
    """Write a swarm.yaml config file."""
    data = {
        "session_name": "swarm",
        "projects_dir": projects_dir,
        "workers": [{"name": name, "path": path} for name, path in workers],
        "groups": [{"name": gname, "workers": members} for gname, members in groups.items()],
    }
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w") as f:
        f.write("# Generated by swarm init\n")
        yaml.dump(data, f, default_flow_style=False, sort_keys=False)
    os.chmod(output_path, 0o600)


def _serialize_queen(q: QueenConfig) -> dict[str, Any]:
    """Serialize QueenConfig, omitting empty system_prompt."""
    d: dict[str, Any] = {
        "cooldown": q.cooldown,
        "enabled": q.enabled,
        "min_confidence": q.min_confidence,
    }
    if q.system_prompt:
        d["system_prompt"] = q.system_prompt
    return d


def _serialize_worker(w: WorkerConfig) -> dict[str, Any]:
    """Serialize a WorkerConfig, omitting empty description."""
    d: dict[str, Any] = {"name": w.name, "path": w.path}
    if w.description:
        d["description"] = w.description
    return d


def serialize_config(config: HiveConfig) -> dict[str, Any]:
    """Full round-trip serialization of HiveConfig to a dict. Omits None optional fields."""
    data: dict = {
        "session_name": config.session_name,
        "projects_dir": config.projects_dir,
        "port": config.port,
        "panes_per_window": config.panes_per_window,
        "watch_interval": config.watch_interval,
        "log_level": config.log_level,
        "workers": [_serialize_worker(w) for w in config.workers],
        "groups": [{"name": g.name, "workers": g.workers} for g in config.groups],
    }
    if config.default_group:
        data["default_group"] = config.default_group
    drones_dict: dict = {
        "enabled": config.drones.enabled,
        "escalation_threshold": config.drones.escalation_threshold,
        "poll_interval": config.drones.poll_interval,
        "auto_approve_yn": config.drones.auto_approve_yn,
        "max_revive_attempts": config.drones.max_revive_attempts,
        "max_poll_failures": config.drones.max_poll_failures,
        "max_idle_interval": config.drones.max_idle_interval,
        "auto_stop_on_complete": config.drones.auto_stop_on_complete,
        "approval_rules": [
            {"pattern": r.pattern, "action": r.action} for r in config.drones.approval_rules
        ],
    }
    if config.drones.allowed_read_paths:
        drones_dict["allowed_read_paths"] = list(config.drones.allowed_read_paths)
    data["drones"] = drones_dict
    data["queen"] = _serialize_queen(config.queen)
    data["notifications"] = {
        "terminal_bell": config.notifications.terminal_bell,
        "desktop": config.notifications.desktop,
        "debounce_seconds": config.notifications.debounce_seconds,
    }
    # Optional fields — only include if set
    if config.log_file is not None:
        data["log_file"] = config.log_file
    if config.daemon_url is not None:
        data["daemon_url"] = config.daemon_url
    if config.api_password is not None:
        data["api_password"] = config.api_password
    # Workflows — only include if overrides are set
    if config.workflows:
        data["workflows"] = dict(config.workflows)
    # Integrations — only include if graph_client_id is set
    if config.graph_client_id:
        data["integrations"] = {
            "graph": {
                "client_id": config.graph_client_id,
                "tenant_id": config.graph_tenant_id,
            }
        }
    return data


def save_config(config: HiveConfig, path: str | None = None) -> None:
    """Write full YAML config. Defaults to config.source_path, falls back to ./swarm.yaml."""
    import logging
    import shutil

    _save_log = logging.getLogger("swarm.config.save")
    target = Path(path or config.source_path or "swarm.yaml")
    data = serialize_config(config)

    # Safety: refuse to overwrite a config that had workers with an empty one
    if target.exists() and not data.get("workers"):
        try:
            existing = yaml.safe_load(target.read_text()) or {}
            if existing.get("workers"):
                _save_log.error(
                    "BLOCKED: save_config would wipe %d workers — refusing to write",
                    len(existing["workers"]),
                )
                return
        except OSError:
            _save_log.debug("could not read existing config for safety check")
            pass  # Can't read existing — proceed cautiously

    # Backup before writing (keep one .bak copy)
    if target.exists():
        bak = target.with_suffix(".yaml.bak")
        try:
            shutil.copy2(str(target), str(bak))
        except Exception:
            _save_log.debug("could not create backup at %s", bak)

    with open(target, "w") as f:
        yaml.dump(data, f, default_flow_style=False, sort_keys=False)
    os.chmod(str(target), 0o600)
