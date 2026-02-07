"""YAML configuration loader for hive definitions."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml


class ConfigError(Exception):
    """Raised when swarm.yaml is invalid."""


@dataclass
class BuzzConfig:
    """Auto-pilot settings (``buzz:`` section in swarm.yaml)."""

    escalation_threshold: float = 15.0
    poll_interval: float = 5.0
    auto_approve_yn: bool = False
    max_revive_attempts: int = 3
    max_poll_failures: int = 5
    max_idle_interval: float = 30.0
    auto_stop_on_complete: bool = True


@dataclass
class QueenConfig:
    """Queen conductor settings (``queen:`` section in swarm.yaml)."""

    cooldown: float = 30.0
    enabled: bool = True


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

    @property
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
    panes_per_window: int = 4
    watch_interval: int = 5
    source_path: str | None = None
    buzz: BuzzConfig = field(default_factory=BuzzConfig)
    queen: QueenConfig = field(default_factory=QueenConfig)
    notifications: NotifyConfig = field(default_factory=NotifyConfig)
    log_level: str = "WARNING"
    log_file: str | None = None
    daemon_url: str | None = None  # e.g. "http://localhost:8080" — TUI connects via API

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

    def validate(self) -> list[str]:
        """Validate config, returning a list of error messages (empty = valid)."""
        errors: list[str] = []

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
                    errors.append(
                        f"Group '{g.name}' references unknown worker: '{member}'"
                    )

        # Check duplicate group names
        gnames = [g.name.lower() for g in self.groups]
        seen_g: set[str] = set()
        for gn in gnames:
            if gn in seen_g:
                errors.append(f"Duplicate group name: '{gn}'")
            seen_g.add(gn)

        # Validate log_file parent directory
        if self.log_file:
            log_parent = Path(self.log_file).expanduser().parent
            if not log_parent.exists():
                errors.append(f"Log file parent directory does not exist: {log_parent}")

        return errors

    def apply_env_overrides(self) -> None:
        """Apply environment variable overrides."""
        import os
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

    workers = [
        WorkerConfig(name=w["name"], path=w["path"])
        for w in data.get("workers", [])
    ]
    groups = [
        GroupConfig(name=g["name"], workers=g["workers"])
        for g in data.get("groups", [])
    ]

    # Parse buzz section
    buzz_data = data.get("buzz", {})
    buzz = BuzzConfig(
        escalation_threshold=buzz_data.get("escalation_threshold", 15.0),
        poll_interval=buzz_data.get("poll_interval", 5.0),
        auto_approve_yn=buzz_data.get("auto_approve_yn", False),
        max_revive_attempts=buzz_data.get("max_revive_attempts", 3),
        max_poll_failures=buzz_data.get("max_poll_failures", 5),
        max_idle_interval=buzz_data.get("max_idle_interval", 30.0),
        auto_stop_on_complete=buzz_data.get("auto_stop_on_complete", True),
    )

    # Parse queen section
    queen_data = data.get("queen", {})
    queen = QueenConfig(
        cooldown=queen_data.get("cooldown", 30.0),
        enabled=queen_data.get("enabled", True),
    )

    # Parse notifications section
    notify_data = data.get("notifications", {})
    notifications = NotifyConfig(
        terminal_bell=notify_data.get("terminal_bell", True),
        desktop=notify_data.get("desktop", True),
        debounce_seconds=notify_data.get("debounce_seconds", 5.0),
    )

    return HiveConfig(
        session_name=data.get("session_name", "swarm"),
        projects_dir=data.get("projects_dir", "~/projects"),
        workers=workers,
        groups=groups,
        panes_per_window=data.get("panes_per_window", 4),
        watch_interval=data.get("watch_interval", 5),
        source_path=str(path),
        buzz=buzz,
        queen=queen,
        notifications=notifications,
        log_level=data.get("log_level", "WARNING"),
        log_file=data.get("log_file"),
        daemon_url=data.get("daemon_url"),
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
    with open(output_path, "w") as f:
        f.write("# Generated by swarm init\n")
        yaml.dump(data, f, default_flow_style=False, sort_keys=False)
