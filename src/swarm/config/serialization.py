"""Serialization and saving of HiveConfig to YAML."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

import yaml

from swarm.config.models import (
    ConfigError,
    HiveConfig,
    ProviderTuning,
    QueenConfig,
    StateThresholds,
    TestConfig,
    WorkerConfig,
)


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
    d["oversight"] = {
        "enabled": q.oversight.enabled,
        "buzzing_threshold_minutes": q.oversight.buzzing_threshold_minutes,
        "drift_check_interval_minutes": q.oversight.drift_check_interval_minutes,
        "max_calls_per_hour": q.oversight.max_calls_per_hour,
    }
    return d


def _serialize_worker(w: WorkerConfig) -> dict[str, Any]:
    """Serialize a WorkerConfig, omitting empty description and provider."""
    d: dict[str, Any] = {"name": w.name, "path": w.path}
    if w.description:
        d["description"] = w.description
    if w.provider:
        d["provider"] = w.provider
    if w.isolation:
        d["isolation"] = w.isolation
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


def _serialize_tuning(tuning: ProviderTuning) -> dict[str, Any]:
    """Serialize a ProviderTuning to a dict, omitting empty/default fields."""
    d: dict[str, Any] = {}
    for key in (
        "idle_pattern",
        "busy_pattern",
        "choice_pattern",
        "user_question_pattern",
        "safe_patterns",
        "approval_key",
        "rejection_key",
    ):
        val = getattr(tuning, key, "")
        if val:
            d[key] = val
    if tuning.env_strip_prefixes:
        d["env_strip_prefixes"] = list(tuning.env_strip_prefixes)
    if tuning.env_vars:
        d["env_vars"] = dict(tuning.env_vars)
    if tuning.tail_lines:
        d["tail_lines"] = tuning.tail_lines
    return d


def _serialize_llms_optional(config: HiveConfig, data: dict[str, Any]) -> None:
    """Serialize custom LLMs and provider overrides into *data*."""
    if config.custom_llms:
        data["llms"] = [
            {
                "name": llm.name,
                "command": llm.command,
                **({"display_name": llm.display_name} if llm.display_name else {}),
                **(_serialize_tuning(llm.tuning) if llm.tuning.has_tuning() else {}),
            }
            for llm in config.custom_llms
        ]
    if config.provider_overrides:
        overrides_dict: dict[str, Any] = {}
        for pname, tuning in config.provider_overrides.items():
            td = _serialize_tuning(tuning)
            if td:
                overrides_dict[pname] = td
        if overrides_dict:
            data["provider_overrides"] = overrides_dict


def _serialize_terminal_optional(config: HiveConfig, data: dict[str, Any]) -> None:
    if not config.terminal.replay_scrollback:
        data["terminal"] = {
            "replay_scrollback": config.terminal.replay_scrollback,
        }


def _serialize_integrations_optional(config: HiveConfig, data: dict[str, Any]) -> None:
    if config.graph_client_id:
        data["integrations"] = {
            "graph": {
                "client_id": config.graph_client_id,
                "tenant_id": config.graph_tenant_id,
            }
        }


def _serialize_optional(config: HiveConfig, data: dict[str, Any]) -> None:
    """Serialize optional config fields into *data* (mutating). Keeps serialize_config lean."""
    for key, val in (
        ("log_file", config.log_file),
        ("daemon_url", config.daemon_url),
        ("api_password", config.api_password),
    ):
        if val is not None:
            data[key] = val
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
    _serialize_llms_optional(config, data)
    _serialize_terminal_optional(config, data)
    data["test"] = _serialize_test(config.test)
    if config.auto_mode:
        data["auto_mode"] = config.auto_mode
    if config.trust_proxy:
        data["trust_proxy"] = config.trust_proxy
    if config.tunnel_domain:
        data["tunnel_domain"] = config.tunnel_domain
    _serialize_integrations_optional(config, data)


def serialize_config(config: HiveConfig) -> dict[str, Any]:
    """Full round-trip serialization of HiveConfig to a dict. Omits None optional fields."""
    data: dict[str, Any] = {
        "session_name": config.session_name,
        "projects_dir": config.projects_dir,
        "provider": config.provider,
        "host": config.host,
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
        "poll_interval_buzzing": config.drones.poll_interval_buzzing,
        "poll_interval_waiting": config.drones.poll_interval_waiting,
        "poll_interval_resting": config.drones.poll_interval_resting,
        "auto_approve_yn": config.drones.auto_approve_yn,
        "max_revive_attempts": config.drones.max_revive_attempts,
        "max_poll_failures": config.drones.max_poll_failures,
        "max_idle_interval": config.drones.max_idle_interval,
        "auto_stop_on_complete": config.drones.auto_stop_on_complete,
        "auto_approve_assignments": config.drones.auto_approve_assignments,
        "idle_assign_threshold": config.drones.idle_assign_threshold,
        "auto_complete_min_idle": config.drones.auto_complete_min_idle,
        "sleeping_poll_interval": config.drones.sleeping_poll_interval,
        "sleeping_threshold": config.drones.sleeping_threshold,
        "stung_reap_timeout": config.drones.stung_reap_timeout,
        "approval_rules": [
            {"pattern": r.pattern, "action": r.action} for r in config.drones.approval_rules
        ],
    }
    if config.drones.allowed_read_paths:
        drones_dict["allowed_read_paths"] = list(config.drones.allowed_read_paths)
    st = config.drones.state_thresholds
    default_st = StateThresholds()
    if st != default_st:
        drones_dict["state_thresholds"] = {
            "buzzing_confirm_count": st.buzzing_confirm_count,
            "stung_confirm_count": st.stung_confirm_count,
            "revive_grace": st.revive_grace,
        }
    data["drones"] = drones_dict
    data["queen"] = _serialize_queen(config.queen)
    data["notifications"] = {
        "terminal_bell": config.notifications.terminal_bell,
        "desktop": config.notifications.desktop,
        "debounce_seconds": config.notifications.debounce_seconds,
    }
    data["coordination"] = {
        "mode": config.coordination.mode,
        "auto_pull": config.coordination.auto_pull,
        "file_ownership": config.coordination.file_ownership,
    }
    if config.jira.enabled or config.jira.client_id:
        jira_out: dict[str, object] = {
            "enabled": config.jira.enabled,
            "project": config.jira.project,
            "sync_interval_minutes": config.jira.sync_interval_minutes,
            "import_filter": config.jira.import_filter,
            "import_label": config.jira.import_label,
            "status_map": dict(config.jira.status_map),
        }
        if config.jira.client_id:
            jira_out["client_id"] = config.jira.client_id
        if config.jira.client_secret:
            jira_out["client_secret"] = config.jira.client_secret
        if config.jira.cloud_id:
            jira_out["cloud_id"] = config.jira.cloud_id
        data["jira"] = jira_out
    _serialize_optional(config, data)
    return data


def save_config(config: HiveConfig, path: str | None = None) -> None:
    """Write full YAML config. Requires explicit path or config.source_path."""
    import shutil

    _save_log = logging.getLogger("swarm.config.save")
    resolved = path or config.source_path
    if not resolved:
        raise ConfigError("save_config called with no path and no source_path — refusing to write")
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
            pass  # Can't read existing -- proceed cautiously

    # Backup before writing (keep one .bak copy)
    if target.exists():
        bak = target.with_suffix(".yaml.bak")
        try:
            shutil.copy2(str(target), str(bak))
        except Exception:
            _save_log.debug("could not create backup at %s", bak)

    import tempfile

    # Atomic write: write to temp file then rename (prevents partial writes on crash)
    content = yaml.dump(data, default_flow_style=False, sort_keys=False)
    fd, tmp = tempfile.mkstemp(dir=str(target.parent), suffix=".tmp")
    closed = False
    try:
        os.write(fd, content.encode())
        os.fchmod(fd, 0o600)
        os.close(fd)
        closed = True
        os.replace(tmp, str(target))
    except BaseException:
        if not closed:
            os.close(fd)
        Path(tmp).unlink(missing_ok=True)
        raise
