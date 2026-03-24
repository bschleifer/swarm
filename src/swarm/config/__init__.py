"""YAML configuration loader for hive definitions.

This package re-exports all public names for backward compatibility so that
``from swarm.config import HiveConfig`` (and similar) continues to work.
"""

from __future__ import annotations

from swarm.config._known_keys import (
    _TUNING_FIELDS,
    _parse_tuning,
)
from swarm.config.loader import (
    _parse_config,
    discover_projects,
    load_config,
    write_config,
)
from swarm.config.models import (
    DEFAULT_ACTION_BUTTONS,
    DEFAULT_TASK_BUTTONS,
    ActionButtonConfig,
    ConfigError,
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
    WebhookConfig,
    WorkerConfig,
    _validate_tuning_patterns,
)
from swarm.config.serialization import (
    _serialize_test,
    _serialize_tuning,
    save_config,
    serialize_config,
)

__all__ = [
    "DEFAULT_ACTION_BUTTONS",
    "DEFAULT_TASK_BUTTONS",
    "_TUNING_FIELDS",
    "ActionButtonConfig",
    "ConfigError",
    "CoordinationConfig",
    "CustomLLMConfig",
    "DroneApprovalRule",
    "DroneConfig",
    "GroupConfig",
    "HiveConfig",
    "JiraConfig",
    "NotifyConfig",
    "OversightConfig",
    "ProviderTuning",
    "QueenConfig",
    "ResourceConfig",
    "StateThresholds",
    "TaskButtonConfig",
    "TerminalConfig",
    "TestConfig",
    "ToolButtonConfig",
    "WebhookConfig",
    "WorkerConfig",
    "_parse_config",
    "_parse_tuning",
    "_serialize_test",
    "_serialize_tuning",
    "_validate_tuning_patterns",
    "discover_projects",
    "load_config",
    "save_config",
    "serialize_config",
    "write_config",
]
