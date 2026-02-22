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
    escalation_threshold: float = 120.0
    poll_interval: float = 5.0
    # State-aware polling: override base interval for specific worker states.
    # Defaults derive from poll_interval if not set explicitly.
    poll_interval_buzzing: float = 0.0  # 0 = 2× poll_interval
    poll_interval_waiting: float = 0.0  # 0 = poll_interval (fast — prompt needs response)
    poll_interval_resting: float = 0.0  # 0 = 3× poll_interval
    auto_approve_yn: bool = False
    max_revive_attempts: int = 3
    max_poll_failures: int = 5
    max_idle_interval: float = 30.0
    auto_stop_on_complete: bool = False
    auto_approve_assignments: bool = True
    idle_assign_threshold: int = 3
    auto_complete_min_idle: float = 45.0  # seconds idle before proposing task completion
    sleeping_poll_interval: float = 30.0  # full poll interval for sleeping workers
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
    max_session_calls: int = 20
    max_session_age: float = 1800.0  # 30 minutes


@dataclass
class NotifyConfig:
    """Notification settings (``notifications:`` section in swarm.yaml)."""

    terminal_bell: bool = True
    desktop: bool = True
    debounce_seconds: float = 5.0


@dataclass
class ToolButtonConfig:
    """A configurable tool button (``tool_buttons:`` section in swarm.yaml)."""

    label: str
    command: str


@dataclass
class ActionButtonConfig:
    """A unified action button for the dashboard action bar.

    Replaces the hardcoded built-in buttons + separate ``tool_buttons`` with a
    single reorderable, visibility-togglable list.
    """

    label: str
    action: str = ""  # built-in: revive, refresh, queen, kill; empty = custom
    command: str = ""  # text sent to worker (custom buttons; blank = continue)
    style: str = "secondary"  # CSS class suffix: secondary, queen, danger
    show_mobile: bool = True
    show_desktop: bool = True


DEFAULT_ACTION_BUTTONS: list[ActionButtonConfig] = [
    ActionButtonConfig(label="Revive", action="revive", style="secondary"),
    ActionButtonConfig(label="Refresh", action="refresh", style="secondary"),
    ActionButtonConfig(label="Ask Queen", action="queen", style="queen"),
    ActionButtonConfig(label="Kill", action="kill", style="danger"),
]


@dataclass
class TaskButtonConfig:
    """A configurable task-list button (``task_buttons:`` section in swarm.yaml).

    Controls order and mobile/desktop visibility of task action buttons.
    Styles are derived from the action name (hardcoded CSS per action type).
    """

    label: str
    action: str  # edit, assign, done, unassign, fail, reopen, log, retry_draft, remove
    show_mobile: bool = True
    show_desktop: bool = True


DEFAULT_TASK_BUTTONS: list[TaskButtonConfig] = [
    TaskButtonConfig(label="Edit", action="edit"),
    TaskButtonConfig(label="Assign", action="assign"),
    TaskButtonConfig(label="Done", action="done"),
    TaskButtonConfig(label="Unassign", action="unassign"),
    TaskButtonConfig(label="Fail", action="fail"),
    TaskButtonConfig(label="Reopen", action="reopen"),
    TaskButtonConfig(label="Log", action="log"),
    TaskButtonConfig(label="Retry Draft", action="retry_draft"),
    TaskButtonConfig(label="\u00d7", action="remove"),
]


@dataclass
class WorkerConfig:
    name: str
    path: str
    description: str = ""
    provider: str = ""  # empty = inherit HiveConfig.provider

    @functools.cached_property
    def resolved_path(self) -> Path:
        return Path(self.path).expanduser().resolve()


@dataclass
class GroupConfig:
    name: str
    workers: list[str]


@dataclass
class TestConfig:
    """Settings for ``swarm test`` supervised orchestration testing."""

    enabled: bool = False
    port: int = 9091  # dedicated test port (separate from main web UI)
    auto_resolve_delay: float = 4.0  # seconds before Queen resolves proposal
    report_dir: str = "~/.swarm/reports"
    auto_complete_min_idle: float = 10.0  # shorter idle threshold for test mode


@dataclass
class HiveConfig:
    session_name: str = "swarm"
    projects_dir: str = "~/projects"
    provider: str = "claude"  # global default: "claude" | "gemini" | "codex"
    workers: list[WorkerConfig] = field(default_factory=list)
    groups: list[GroupConfig] = field(default_factory=list)
    default_group: str = ""
    watch_interval: int = 5
    source_path: str | None = None
    drones: DroneConfig = field(default_factory=DroneConfig)
    queen: QueenConfig = field(default_factory=QueenConfig)
    notifications: NotifyConfig = field(default_factory=NotifyConfig)
    test: TestConfig = field(default_factory=TestConfig)
    # Skill overrides per task type (e.g. {"bug": "/fix-and-ship", "feature": "/feature"}).
    # Keys are TaskType values: bug, feature, verify, chore.
    # Set a value to null/empty to disable skill invocation for that type.
    workflows: dict[str, str] = field(default_factory=dict)
    tool_buttons: list[ToolButtonConfig] = field(default_factory=list)
    action_buttons: list[ActionButtonConfig] = field(default_factory=list)
    task_buttons: list[TaskButtonConfig] = field(default_factory=list)
    log_level: str = "WARNING"
    log_file: str | None = None
    port: int = 9090  # web UI / API server port
    daemon_url: str | None = None  # e.g. "http://localhost:9090" — dashboard connects via API
    api_password: str | None = None  # password for web UI config-mutating endpoints
    graph_client_id: str = ""  # Azure AD app client ID for Microsoft Graph
    graph_tenant_id: str = "common"  # Azure AD tenant ID (or "common")
    tunnel_domain: str = ""  # custom domain for named Cloudflare tunnels (advanced)

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

    def _validate_workers(self) -> list[str]:
        """Check worker definitions: existence, duplicates, paths."""
        errors: list[str] = []
        if not self.workers:
            errors.append("No workers defined — add at least one worker to swarm.yaml")
        names = [w.name.lower() for w in self.workers]
        seen: set[str] = set()
        for n in names:
            if n in seen:
                errors.append(f"Duplicate worker name: '{n}'")
            seen.add(n)
        for w in self.workers:
            p = w.resolved_path
            if not p.exists():
                errors.append(f"Worker '{w.name}' path does not exist: {p}")
        return errors

    def _validate_groups(self) -> list[str]:
        """Check group references and duplicate group names."""
        errors: list[str] = []
        valid_names = {w.name.lower() for w in self.workers}
        for g in self.groups:
            for member in g.workers:
                if member.lower() not in valid_names:
                    errors.append(f"Group '{g.name}' references unknown worker: '{member}'")
        gnames = [g.name.lower() for g in self.groups]
        seen_g: set[str] = set()
        for gn in gnames:
            if gn in seen_g:
                errors.append(f"Duplicate group name: '{gn}'")
            seen_g.add(gn)
        if self.default_group:
            group_names_set = {g.name.lower() for g in self.groups}
            if self.default_group.lower() not in group_names_set:
                errors.append(
                    f"default_group '{self.default_group}' does not match any defined group"
                )
        return errors

    def _validate_numeric_ranges(self) -> list[str]:
        """Check numeric field ranges, paths, and approval rule patterns."""
        errors: list[str] = []
        if self.log_file:
            log_parent = Path(self.log_file).expanduser().parent
            if not log_parent.exists():
                errors.append(f"Log file parent directory does not exist: {log_parent}")
        if self.watch_interval <= 0:
            errors.append("watch_interval must be > 0")
        if self.drones.poll_interval <= 0:
            errors.append("drones.poll_interval must be > 0")
        if self.drones.escalation_threshold <= 0:
            errors.append("drones.escalation_threshold must be > 0")
        if self.queen.cooldown < 0:
            errors.append("queen.cooldown must be >= 0")
        if not (0.0 <= self.queen.min_confidence <= 1.0):
            errors.append("queen.min_confidence must be between 0.0 and 1.0")
        errors.extend(self._validate_approval_rules())
        return errors

    def _validate_approval_rules(self) -> list[str]:
        """Validate drone approval rule regex patterns and action values."""
        errors: list[str] = []
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

    def validate(self) -> list[str]:
        """Validate config, returning a list of error messages (empty = valid)."""
        errors: list[str] = []
        errors.extend(self._validate_workers())
        errors.extend(self._validate_groups())
        errors.extend(self._validate_numeric_ranges())
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
    data = yaml.safe_load(path.read_text()) or {}

    try:
        workers = [
            WorkerConfig(
                name=w["name"],
                path=w["path"],
                description=w.get("description", ""),
                provider=w.get("provider", ""),
            )
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
        escalation_threshold=drones_data.get("escalation_threshold", 120.0),
        poll_interval=drones_data.get("poll_interval", 5.0),
        poll_interval_buzzing=drones_data.get("poll_interval_buzzing", 0.0),
        poll_interval_waiting=drones_data.get("poll_interval_waiting", 0.0),
        poll_interval_resting=drones_data.get("poll_interval_resting", 0.0),
        auto_approve_yn=drones_data.get("auto_approve_yn", False),
        max_revive_attempts=drones_data.get("max_revive_attempts", 3),
        max_poll_failures=drones_data.get("max_poll_failures", 5),
        max_idle_interval=drones_data.get("max_idle_interval", 30.0),
        auto_stop_on_complete=drones_data.get("auto_stop_on_complete", True),
        auto_approve_assignments=drones_data.get("auto_approve_assignments", True),
        idle_assign_threshold=drones_data.get("idle_assign_threshold", 3),
        auto_complete_min_idle=drones_data.get("auto_complete_min_idle", 45.0),
        sleeping_poll_interval=drones_data.get("sleeping_poll_interval", 30.0),
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
        max_session_calls=queen_data.get("max_session_calls", 20),
        max_session_age=queen_data.get("max_session_age", 1800.0),
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

    # Parse tool_buttons section (legacy)
    tool_buttons_raw = data.get("tool_buttons", [])
    tool_buttons = [
        ToolButtonConfig(label=b.get("label", ""), command=b.get("command", ""))
        for b in tool_buttons_raw
        if isinstance(b, dict) and b.get("label")
    ]

    # Parse action_buttons — unified reorderable action bar
    action_buttons_raw = data.get("action_buttons", [])
    if action_buttons_raw:
        action_buttons = [
            ActionButtonConfig(
                label=b.get("label", ""),
                action=b.get("action", ""),
                command=b.get("command", ""),
                style=b.get("style", "secondary"),
                show_mobile=b.get("show_mobile", True),
                show_desktop=b.get("show_desktop", True),
            )
            for b in action_buttons_raw
            if isinstance(b, dict) and b.get("label")
        ]
    else:
        # Backward compat: build from defaults + legacy tool_buttons
        action_buttons = list(DEFAULT_ACTION_BUTTONS)
        for tb in tool_buttons:
            action_buttons.append(
                ActionButtonConfig(label=tb.label, command=tb.command, style="secondary")
            )

    # Parse task_buttons — configurable task-list buttons
    task_buttons_raw = data.get("task_buttons", [])
    if task_buttons_raw:
        task_buttons = [
            TaskButtonConfig(
                label=b.get("label", ""),
                action=b.get("action", ""),
                show_mobile=b.get("show_mobile", True),
                show_desktop=b.get("show_desktop", True),
            )
            for b in task_buttons_raw
            if isinstance(b, dict) and b.get("label") and b.get("action")
        ]
    else:
        task_buttons = list(DEFAULT_TASK_BUTTONS)

    # Parse test section
    test_data = data.get("test", {})
    test = TestConfig(
        enabled=test_data.get("enabled", False),
        port=int(test_data.get("port", 9091)),
        auto_resolve_delay=test_data.get("auto_resolve_delay", 4.0),
        report_dir=test_data.get("report_dir", "~/.swarm/reports"),
        auto_complete_min_idle=test_data.get("auto_complete_min_idle", 10.0),
    )

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
        provider=data.get("provider", "claude"),
        workers=workers,
        groups=groups,
        default_group=data.get("default_group", ""),
        watch_interval=data.get("watch_interval", 5),
        source_path=str(path),
        drones=drones,
        queen=queen,
        notifications=notifications,
        test=test,
        workflows=workflows,
        tool_buttons=tool_buttons,
        action_buttons=action_buttons,
        task_buttons=task_buttons,
        log_level=data.get("log_level", "WARNING"),
        log_file=data.get("log_file"),
        port=data.get("port", 9090),
        daemon_url=data.get("daemon_url"),
        api_password=data.get("api_password"),
        graph_client_id=graph_data.get("client_id", ""),
        graph_tenant_id=graph_data.get("tenant_id", "common"),
        tunnel_domain=data.get("tunnel_domain", ""),
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
    api_password: str | None = None,
    ported_settings: dict[str, Any] | None = None,
) -> None:
    """Write a swarm.yaml config file.

    Args:
        ported_settings: Optional dict of settings to carry over from a
            previous config (e.g. queen, drones, notifications, port).
            Workers and groups come from the scan, not from ported settings.
    """
    data: dict[str, Any] = {
        "session_name": "swarm",
        "projects_dir": projects_dir,
        "workers": [{"name": name, "path": path} for name, path in workers],
        "groups": [{"name": gname, "workers": members} for gname, members in groups.items()],
    }
    if ported_settings:
        # Merge ported settings, but never override workers/groups/projects_dir
        skip_keys = {"workers", "groups", "projects_dir", "session_name"}
        for key, value in ported_settings.items():
            if key not in skip_keys:
                data[key] = value
    if api_password:
        data["api_password"] = api_password
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    content = "# Generated by swarm init\n" + yaml.dump(
        data, default_flow_style=False, sort_keys=False
    )
    out.write_text(content)
    out.chmod(0o600)


def _serialize_queen(q: QueenConfig) -> dict[str, Any]:
    """Serialize QueenConfig, omitting empty system_prompt."""
    d: dict[str, Any] = {
        "cooldown": q.cooldown,
        "enabled": q.enabled,
        "min_confidence": q.min_confidence,
        "max_session_calls": q.max_session_calls,
        "max_session_age": q.max_session_age,
    }
    if q.system_prompt:
        d["system_prompt"] = q.system_prompt
    return d


def _serialize_worker(w: WorkerConfig) -> dict[str, Any]:
    """Serialize a WorkerConfig, omitting empty description and provider."""
    d: dict[str, Any] = {"name": w.name, "path": w.path}
    if w.description:
        d["description"] = w.description
    if w.provider:
        d["provider"] = w.provider
    return d


def _serialize_test(t: TestConfig) -> dict[str, Any]:
    """Serialize TestConfig. Always returns a dict (templates access unconditionally)."""
    return {
        "enabled": t.enabled,
        "port": t.port,
        "auto_resolve_delay": t.auto_resolve_delay,
        "report_dir": t.report_dir,
        "auto_complete_min_idle": t.auto_complete_min_idle,
    }


def _serialize_optional(config: HiveConfig, data: dict[str, Any]) -> None:
    """Serialize optional config fields into *data* (mutating). Keeps serialize_config lean."""
    if config.log_file is not None:
        data["log_file"] = config.log_file
    if config.daemon_url is not None:
        data["daemon_url"] = config.daemon_url
    if config.api_password is not None:
        data["api_password"] = config.api_password
    if config.workflows:
        data["workflows"] = dict(config.workflows)
    if config.tool_buttons:
        data["tool_buttons"] = [
            {"label": b.label, "command": b.command} for b in config.tool_buttons
        ]
    if config.action_buttons:
        data["action_buttons"] = [
            {
                "label": b.label,
                "action": b.action,
                "command": b.command,
                "style": b.style,
                "show_mobile": b.show_mobile,
                "show_desktop": b.show_desktop,
            }
            for b in config.action_buttons
        ]
    if config.task_buttons:
        data["task_buttons"] = [
            {
                "label": b.label,
                "action": b.action,
                "show_mobile": b.show_mobile,
                "show_desktop": b.show_desktop,
            }
            for b in config.task_buttons
        ]
    data["test"] = _serialize_test(config.test)
    if config.tunnel_domain:
        data["tunnel_domain"] = config.tunnel_domain
    if config.graph_client_id:
        data["integrations"] = {
            "graph": {
                "client_id": config.graph_client_id,
                "tenant_id": config.graph_tenant_id,
            }
        }


def serialize_config(config: HiveConfig) -> dict[str, Any]:
    """Full round-trip serialization of HiveConfig to a dict. Omits None optional fields."""
    data: dict[str, Any] = {
        "session_name": config.session_name,
        "projects_dir": config.projects_dir,
        "provider": config.provider,
        "port": config.port,
        "watch_interval": config.watch_interval,
        "log_level": config.log_level,
        "workers": [_serialize_worker(w) for w in config.workers],
        "groups": [{"name": g.name, "workers": g.workers} for g in config.groups],
    }
    if config.default_group:
        data["default_group"] = config.default_group
    drones_dict: dict[str, Any] = {
        "enabled": config.drones.enabled,
        "escalation_threshold": config.drones.escalation_threshold,
        "poll_interval": config.drones.poll_interval,
        "auto_approve_yn": config.drones.auto_approve_yn,
        "max_revive_attempts": config.drones.max_revive_attempts,
        "max_poll_failures": config.drones.max_poll_failures,
        "max_idle_interval": config.drones.max_idle_interval,
        "auto_stop_on_complete": config.drones.auto_stop_on_complete,
        "auto_approve_assignments": config.drones.auto_approve_assignments,
        "idle_assign_threshold": config.drones.idle_assign_threshold,
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
    _serialize_optional(config, data)
    return data


def save_config(config: HiveConfig, path: str | None = None) -> None:
    """Write full YAML config. Requires explicit path or config.source_path."""
    import logging
    import shutil

    _save_log = logging.getLogger("swarm.config.save")
    resolved = path or config.source_path
    if not resolved:
        _save_log.error("save_config called with no path and no source_path — refusing to write")
        return
    target = Path(resolved)
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

    target.write_text(yaml.dump(data, default_flow_style=False, sort_keys=False))
    target.chmod(0o600)
