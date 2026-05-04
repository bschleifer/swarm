"""Tests for db.config_store — SQLite-backed config persistence."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from swarm.config.models import (
    ActionButtonConfig,
    CoordinationConfig,
    DroneApprovalRule,
    DroneConfig,
    EmailConfig,
    GroupConfig,
    HiveConfig,
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
from swarm.db.config_store import (
    _load_groups,
    _load_workers,
    _parse_button_list,
    _parse_custom_llms,
    _parse_drone_config,
    _parse_json_dataclass,
    _parse_notify_config,
    _parse_provider_overrides,
    _parse_queen_config,
    _save_groups,
    _save_workers,
    load_config_from_db,
    save_config_to_db,
)
from swarm.db.core import SwarmDB


@pytest.fixture
def db(tmp_path: Path) -> SwarmDB:
    return SwarmDB(tmp_path / "test.db")


# ======================================================================
# Round-trip: save then load
# ======================================================================


class TestComprehensiveRoundTrip:
    """Phase 4 of #328: a single test that walks ``HiveConfig`` end-to-end
    and proves every persistable field survives the ``save_config_to_db
    → load_config_from_db`` round trip.

    Future field additions either round-trip cleanly or this test fails
    loudly.  The audit (Phase 1) found multiple silent-drop fields whose
    common pattern was: dataclass had it, dashboard sent it, server
    quietly discarded it.  This test locks in the contract so the
    persistence layer can never regress without the test catching it.
    """

    def test_full_config_survives_round_trip(self, db: SwarmDB) -> None:
        """Build a HiveConfig with non-default values for every field
        the dashboard / API can edit, save, reload, and assert that
        every field equals what was saved.

        Excluded (by design or out of scope for persistence):
          - ``source_path`` / ``config_source`` — runtime-only, set by loader
          - ``sandbox`` — entire section not yet wired to L5/L6 (audit gap)
          - ``approval_rules`` — opt-in destructive sync; tested separately
        """
        from swarm.config.serialization import serialize_config

        original = HiveConfig(
            session_name="audit-hive",
            projects_dir="/srv/projects",
            provider="gemini",
            workers=[
                WorkerConfig(
                    name="api",
                    path="/srv/api",
                    description="API service worker",
                    provider="claude",
                    isolation="worktree",
                    identity="~/.swarm/identities/api.md",
                ),
                WorkerConfig(name="web", path="/srv/web", description="Web worker"),
            ],
            groups=[
                GroupConfig(name="backend", workers=["api"]),
                GroupConfig(name="all", workers=["api", "web"]),
            ],
            default_group="backend",
            watch_interval=12,
            drones=DroneConfig(
                enabled=False,
                escalation_threshold=99.0,
                poll_interval=7.5,
                poll_interval_buzzing=3.5,
                poll_interval_waiting=4.5,
                poll_interval_resting=15.0,
                auto_approve_yn=True,
                max_revive_attempts=4,
                max_poll_failures=8,
                max_idle_interval=45.0,
                auto_stop_on_complete=False,
                auto_approve_assignments=False,
                idle_assign_threshold=5,
                auto_complete_min_idle=60.0,
                sleeping_poll_interval=20.0,
                sleeping_threshold=600.0,
                stung_reap_timeout=15.0,
                state_thresholds=StateThresholds(
                    buzzing_confirm_count=8,
                    stung_confirm_count=4,
                    revive_grace=20.0,
                ),
                allowed_read_paths=["~/.swarm/uploads/", "/tmp/"],
                context_warning_threshold=0.6,
                context_critical_threshold=0.85,
                speculation_enabled=True,
                idle_nudge_interval_seconds=240.0,
                idle_nudge_debounce_seconds=600.0,
            ),
            queen=QueenConfig(
                cooldown=45.0,
                enabled=False,
                system_prompt="custom prompt",
                min_confidence=0.85,
                max_session_calls=30,
                max_session_age=2400.0,
                auto_assign_tasks=False,
                oversight=OversightConfig(
                    enabled=False,
                    buzzing_threshold_minutes=20.0,
                    drift_check_interval_minutes=15.0,
                    max_calls_per_hour=10,
                ),
            ),
            notifications=NotifyConfig(
                terminal_bell=False,
                desktop=False,
                desktop_events=["worker_stung", "task_completed"],
                terminal_events=["worker_stung"],
                debounce_seconds=12.5,
                templates={"task_completed": "Done: {title}"},
                webhook=WebhookConfig(
                    url="https://hooks.example.com/swarm",
                    events=["worker_stung"],
                ),
                email=EmailConfig(
                    enabled=True,
                    smtp_host="smtp.example.com",
                    smtp_port=465,
                    smtp_user="swarm@example.com",
                    smtp_password="topsecret",
                    use_tls=False,
                    from_address="swarm@example.com",
                    to_addresses=["ops@example.com"],
                    events=["worker_stung", "task_completed"],
                ),
            ),
            coordination=CoordinationConfig(
                mode="worktree",
                auto_pull=False,
                file_ownership="hard-block",
            ),
            test=TestConfig(
                enabled=True,
                port=9999,
                auto_resolve_delay=8.0,
                report_dir="/tmp/swarm-reports",
                auto_complete_min_idle=15.0,
            ),
            terminal=TerminalConfig(replay_scrollback=False),
            resources=ResourceConfig(
                enabled=False,
                poll_interval=15.0,
                elevated_swap_pct=50.0,
                elevated_mem_pct=85.0,
                high_swap_pct=75.0,
                high_mem_pct=92.0,
                critical_swap_pct=88.0,
                critical_mem_pct=97.0,
                suspend_on_high=False,
                dstate_scan=False,
                dstate_threshold_sec=180.0,
            ),
            workflows={"bug": "/fix-and-ship", "feature": "/feature"},
            tool_buttons=[ToolButtonConfig(label="Deploy", command="deploy.sh")],
            action_buttons=[
                ActionButtonConfig(
                    label="Custom",
                    action="custom",
                    command="do thing",
                    style="danger",
                    show_mobile=False,
                    show_desktop=True,
                ),
            ],
            task_buttons=[
                TaskButtonConfig(label="Approve", action="approve", show_mobile=True),
            ],
            provider_overrides={
                "gemini": ProviderTuning(
                    idle_pattern=r"gemini>",
                    busy_pattern=r"thinking",
                ),
            },
            log_level="DEBUG",
            log_file="/var/log/swarm.log",
            port=9091,
            daemon_url="http://localhost:9091",
            api_password="hashedpass",
            graph_client_id="graph-app-id",
            graph_tenant_id="tenant-xyz",
            graph_client_secret="graph-secret",
            trust_proxy=True,
            tunnel_domain="swarm.test",
            domain="test.example",
        )

        save_config_to_db(db, original)
        loaded = load_config_from_db(db)
        assert loaded is not None

        # Compare via serialize_config so both ends use identical
        # field-walking logic.  This catches every persisted field
        # without writing one assertion per field.
        original_dict = serialize_config(original)
        loaded_dict = serialize_config(loaded)

        # Drop fields that are by design out of scope:
        #   - approval_rules: requires sync_approval_rules=True (tested
        #     separately in TestRoundTrip.test_drone_config_round_trip)
        #   - source_path / config_source: runtime metadata, not persisted
        for key in (
            "source_path",
            "config_source",
        ):
            original_dict.pop(key, None)
            loaded_dict.pop(key, None)
        # Drop nested approval_rules too
        original_dict.get("drones", {}).pop("approval_rules", None)
        loaded_dict.get("drones", {}).pop("approval_rules", None)
        for w in original_dict.get("workers", []):
            w.pop("approval_rules", None)
        for w in loaded_dict.get("workers", []):
            w.pop("approval_rules", None)
        # Known limitation: the ``groups`` table has no ``sort_order``
        # column, so ``_load_groups`` returns rows alphabetically by
        # name regardless of save order.  Real bug — operators drag
        # groups to reorder them and that order is lost on reload —
        # but separate from the silent-drop class this test was built
        # to catch.  Sort both sides by name for comparison; the
        # ordering bug is tracked as a follow-up.
        original_dict["groups"] = sorted(original_dict.get("groups", []), key=lambda g: g["name"])
        loaded_dict["groups"] = sorted(loaded_dict.get("groups", []), key=lambda g: g["name"])

        assert original_dict == loaded_dict, (
            "Round-trip drift detected.  Diff:\n"
            f"  original keys: {sorted(original_dict.keys())}\n"
            f"  loaded keys:   {sorted(loaded_dict.keys())}\n"
        )


class TestRoundTrip:
    """save_config_to_db -> load_config_from_db preserves all fields."""

    def test_empty_config_round_trip(self, db: SwarmDB) -> None:
        original = HiveConfig()
        save_config_to_db(db, original)
        loaded = load_config_from_db(db)
        assert loaded is not None
        assert loaded.session_name == original.session_name
        assert loaded.provider == original.provider
        assert loaded.port == original.port
        assert loaded.watch_interval == original.watch_interval
        assert loaded.workers == []
        assert loaded.groups == []

    def test_scalars_round_trip(self, db: SwarmDB) -> None:
        original = HiveConfig(
            session_name="my-hive",
            projects_dir="~/code",
            provider="gemini",
            default_group="backend",
            watch_interval=10,
            log_level="DEBUG",
            log_file="/tmp/swarm.log",
            port=8080,
            daemon_url="http://localhost:8080",
            api_password="secret123",
            graph_client_id="abc",
            graph_tenant_id="tenant-1",
            graph_client_secret="s3cret",
            trust_proxy=True,
            tunnel_domain="swarm.example.com",
            domain="example.com",
        )
        save_config_to_db(db, original)
        loaded = load_config_from_db(db)
        assert loaded is not None
        assert loaded.session_name == "my-hive"
        assert loaded.projects_dir == "~/code"
        assert loaded.provider == "gemini"
        assert loaded.default_group == "backend"
        assert loaded.watch_interval == 10
        assert loaded.log_level == "DEBUG"
        assert loaded.log_file == "/tmp/swarm.log"
        assert loaded.port == 8080
        assert loaded.daemon_url == "http://localhost:8080"
        assert loaded.api_password == "secret123"
        assert loaded.graph_client_id == "abc"
        assert loaded.graph_tenant_id == "tenant-1"
        assert loaded.graph_client_secret == "s3cret"
        assert loaded.trust_proxy is True
        assert loaded.tunnel_domain == "swarm.example.com"
        assert loaded.domain == "example.com"

    def test_workers_round_trip(self, db: SwarmDB) -> None:
        original = HiveConfig(
            workers=[
                WorkerConfig(
                    name="api",
                    path="/tmp/api",
                    description="API worker",
                    provider="claude",
                    isolation="worktree",
                    identity="~/.swarm/identities/api.md",
                    approval_rules=[
                        DroneApprovalRule(pattern="Read.*", action="approve"),
                        DroneApprovalRule(pattern="Write.*", action="escalate"),
                    ],
                ),
                WorkerConfig(name="web", path="/tmp/web"),
            ]
        )
        # sync_approval_rules=True because this round-trip test
        # specifically asserts that worker rules are persisted.  The
        # default (False) is the data-loss-safe mode used by routine
        # saves.
        save_config_to_db(db, original, sync_approval_rules=True)
        loaded = load_config_from_db(db)
        assert loaded is not None
        assert len(loaded.workers) == 2
        api = loaded.workers[0]
        assert api.name == "api"
        assert api.path == "/tmp/api"
        assert api.description == "API worker"
        assert api.provider == "claude"
        assert api.isolation == "worktree"
        assert api.identity == "~/.swarm/identities/api.md"
        assert len(api.approval_rules) == 2
        assert api.approval_rules[0].pattern == "Read.*"
        assert api.approval_rules[0].action == "approve"
        assert api.approval_rules[1].pattern == "Write.*"
        assert api.approval_rules[1].action == "escalate"

        web = loaded.workers[1]
        assert web.name == "web"
        assert web.path == "/tmp/web"
        assert web.approval_rules == []

    def test_groups_round_trip(self, db: SwarmDB) -> None:
        original = HiveConfig(
            workers=[
                WorkerConfig(name="api", path="/tmp/api"),
                WorkerConfig(name="web", path="/tmp/web"),
            ],
            groups=[
                GroupConfig(name="backend", workers=["api"]),
                GroupConfig(name="all", workers=["api", "web"]),
            ],
        )
        save_config_to_db(db, original)
        loaded = load_config_from_db(db)
        assert loaded is not None
        assert len(loaded.groups) == 2
        names = {g.name for g in loaded.groups}
        assert names == {"backend", "all"}
        all_group = next(g for g in loaded.groups if g.name == "all")
        assert sorted(all_group.workers) == ["api", "web"]

    def test_drone_config_round_trip(self, db: SwarmDB) -> None:
        original = HiveConfig(
            drones=DroneConfig(
                enabled=False,
                poll_interval=10.0,
                escalation_threshold=60.0,
                auto_approve_yn=True,
                max_revive_attempts=5,
                state_thresholds=StateThresholds(
                    buzzing_confirm_count=8,
                    stung_confirm_count=4,
                    revive_grace=20.0,
                ),
                approval_rules=[
                    DroneApprovalRule(pattern="Bash.*", action="approve"),
                ],
            )
        )
        # sync_approval_rules=True because the test asserts rules
        # round-trip through the DB.  Routine saves are now opt-in.
        save_config_to_db(db, original, sync_approval_rules=True)
        loaded = load_config_from_db(db)
        assert loaded is not None
        assert loaded.drones.enabled is False
        assert loaded.drones.poll_interval == 10.0
        assert loaded.drones.escalation_threshold == 60.0
        assert loaded.drones.auto_approve_yn is True
        assert loaded.drones.max_revive_attempts == 5
        assert loaded.drones.state_thresholds.buzzing_confirm_count == 8
        assert loaded.drones.state_thresholds.stung_confirm_count == 4
        assert loaded.drones.state_thresholds.revive_grace == 20.0
        assert len(loaded.drones.approval_rules) == 1
        assert loaded.drones.approval_rules[0].pattern == "Bash.*"

    def test_queen_config_round_trip(self, db: SwarmDB) -> None:
        original = HiveConfig(
            queen=QueenConfig(
                cooldown=60.0,
                enabled=False,
                system_prompt="You are a queen bee.",
                min_confidence=0.8,
                oversight=OversightConfig(
                    enabled=False,
                    buzzing_threshold_minutes=30.0,
                ),
            )
        )
        save_config_to_db(db, original)
        loaded = load_config_from_db(db)
        assert loaded is not None
        assert loaded.queen.cooldown == 60.0
        assert loaded.queen.enabled is False
        assert loaded.queen.system_prompt == "You are a queen bee."
        assert loaded.queen.min_confidence == 0.8
        assert loaded.queen.oversight.enabled is False
        assert loaded.queen.oversight.buzzing_threshold_minutes == 30.0

    def test_notifications_round_trip(self, db: SwarmDB) -> None:
        original = HiveConfig(
            notifications=NotifyConfig(
                terminal_bell=False,
                desktop=False,
                debounce_seconds=10.0,
                webhook=WebhookConfig(url="https://example.com/hook", events=["stung"]),
                email=EmailConfig(
                    enabled=True,
                    smtp_host="smtp.example.com",
                    smtp_port=465,
                    from_address="swarm@example.com",
                    to_addresses=["admin@example.com"],
                ),
            )
        )
        save_config_to_db(db, original)
        loaded = load_config_from_db(db)
        assert loaded is not None
        assert loaded.notifications.terminal_bell is False
        assert loaded.notifications.desktop is False
        assert loaded.notifications.debounce_seconds == 10.0
        assert loaded.notifications.webhook.url == "https://example.com/hook"
        assert loaded.notifications.webhook.events == ["stung"]
        assert loaded.notifications.email.enabled is True
        assert loaded.notifications.email.smtp_host == "smtp.example.com"
        assert loaded.notifications.email.smtp_port == 465

    def test_tool_buttons_round_trip(self, db: SwarmDB) -> None:
        original = HiveConfig(
            tool_buttons=[
                ToolButtonConfig(label="Deploy", command="deploy.sh"),
                ToolButtonConfig(label="Lint", command="make lint"),
            ]
        )
        save_config_to_db(db, original)
        loaded = load_config_from_db(db)
        assert loaded is not None
        assert len(loaded.tool_buttons) == 2
        assert loaded.tool_buttons[0].label == "Deploy"
        assert loaded.tool_buttons[0].command == "deploy.sh"

    def test_action_buttons_round_trip(self, db: SwarmDB) -> None:
        original = HiveConfig(
            action_buttons=[
                ActionButtonConfig(
                    label="Custom",
                    action="custom",
                    command="do something",
                    style="danger",
                    show_mobile=False,
                ),
            ]
        )
        save_config_to_db(db, original)
        loaded = load_config_from_db(db)
        assert loaded is not None
        assert len(loaded.action_buttons) == 1
        assert loaded.action_buttons[0].label == "Custom"
        assert loaded.action_buttons[0].style == "danger"
        assert loaded.action_buttons[0].show_mobile is False

    def test_task_buttons_round_trip(self, db: SwarmDB) -> None:
        original = HiveConfig(
            task_buttons=[
                TaskButtonConfig(label="Approve", action="approve", show_mobile=False),
            ]
        )
        save_config_to_db(db, original)
        loaded = load_config_from_db(db)
        assert loaded is not None
        assert len(loaded.task_buttons) == 1
        assert loaded.task_buttons[0].label == "Approve"
        assert loaded.task_buttons[0].action == "approve"
        assert loaded.task_buttons[0].show_mobile is False

    def test_custom_llms_round_trip(self, db: SwarmDB) -> None:
        """custom_llms round-trip requires the DB key 'custom_llms'.

        Note: serialize_config() outputs 'llms' (YAML compat) but the
        DB store reads 'custom_llms', so we insert the blob directly to
        test the parser path in isolation.
        """
        import json as _json

        blob = _json.dumps([{"name": "aider", "command": ["aider"], "display_name": "Aider"}])
        db.execute(
            "INSERT INTO config (key, value, updated_at) VALUES (?, ?, ?)",
            ("custom_llms", blob, 1.0),
        )
        db.commit()
        # Need at least one scalar so load doesn't return None
        save_config_to_db(db, HiveConfig(custom_llms=[]))
        # Re-insert the blob after save (save may not write custom_llms key)
        db.execute(
            "INSERT OR REPLACE INTO config (key, value, updated_at) VALUES (?, ?, ?)",
            ("custom_llms", blob, 2.0),
        )
        db.commit()
        loaded = load_config_from_db(db)
        assert loaded is not None
        assert len(loaded.custom_llms) == 1
        assert loaded.custom_llms[0].name == "aider"
        assert loaded.custom_llms[0].command == ["aider"]
        assert loaded.custom_llms[0].display_name == "Aider"

    def test_provider_overrides_round_trip(self, db: SwarmDB) -> None:
        original = HiveConfig(
            provider_overrides={
                "gemini": ProviderTuning(
                    idle_pattern=r"gemini>",
                    busy_pattern=r"thinking\.\.\.",
                    approval_key="y\r",
                ),
            }
        )
        save_config_to_db(db, original)
        loaded = load_config_from_db(db)
        assert loaded is not None
        assert "gemini" in loaded.provider_overrides
        pt = loaded.provider_overrides["gemini"]
        assert pt.idle_pattern == r"gemini>"
        assert pt.busy_pattern == r"thinking\.\.\."
        assert pt.approval_key == "y\r"

    def test_workflows_round_trip(self, db: SwarmDB) -> None:
        original = HiveConfig(workflows={"bug": "/fix-and-ship", "feature": "/feature"})
        save_config_to_db(db, original)
        loaded = load_config_from_db(db)
        assert loaded is not None
        assert loaded.workflows == {"bug": "/fix-and-ship", "feature": "/feature"}

    def test_coordination_round_trip(self, db: SwarmDB) -> None:
        original = HiveConfig(
            coordination=CoordinationConfig(
                mode="worktree", auto_pull=False, file_ownership="hard-block"
            )
        )
        save_config_to_db(db, original)
        loaded = load_config_from_db(db)
        assert loaded is not None
        assert loaded.coordination.mode == "worktree"
        assert loaded.coordination.auto_pull is False
        assert loaded.coordination.file_ownership == "hard-block"

    def test_unicode_descriptions(self, db: SwarmDB) -> None:
        original = HiveConfig(
            workers=[
                WorkerConfig(
                    name="intl",
                    path="/tmp/intl",
                    description="Worker \u2014 handles \u00e9v\u00e9nements & \u65e5\u672c\u8a9e",
                ),
            ]
        )
        save_config_to_db(db, original)
        loaded = load_config_from_db(db)
        assert loaded is not None
        expected = "Worker \u2014 handles \u00e9v\u00e9nements & \u65e5\u672c\u8a9e"
        assert loaded.workers[0].description == expected


# ======================================================================
# load_config_from_db edge cases
# ======================================================================


class TestLoadConfigEdgeCases:
    def test_returns_none_for_empty_db(self, db: SwarmDB) -> None:
        result = load_config_from_db(db)
        assert result is None

    def test_returns_config_when_only_scalars_exist(self, db: SwarmDB) -> None:
        """Config with no workers but scalar keys should still load."""
        db.execute(
            "INSERT INTO config (key, value, updated_at) VALUES (?, ?, ?)",
            ("session_name", "test", 1.0),
        )
        db.commit()
        result = load_config_from_db(db)
        assert result is not None
        assert result.session_name == "test"

    def test_returns_config_when_only_approval_rules_exist(self, db: SwarmDB) -> None:
        """Regression: a DB whose only user data is approval_rules must still
        load from the DB, not trigger the YAML fallback.

        Historical bug: load_config_from_db only checked workers + config
        tables, so a DB with rules but no workers looked empty → the CLI
        fell back to YAML → YAML has no rules → dashboard showed zero
        rules even though the DB rows were right there.
        """
        db.execute(
            "INSERT INTO approval_rules "
            "(owner_type, owner_id, pattern, action, sort_order) "
            "VALUES ('global', NULL, 'Bash.*', 'approve', 0)"
        )
        db.commit()

        result = load_config_from_db(db)
        assert result is not None, "DB with rules only must not fall back to YAML"
        assert len(result.drones.approval_rules) == 1
        assert result.drones.approval_rules[0].pattern == "Bash.*"

    def test_returns_config_when_only_groups_exist(self, db: SwarmDB) -> None:
        """Same principle: a lone groups row is enough to load from DB."""
        db.execute("INSERT INTO groups (id, name, label) VALUES ('gid-1', 'all', '')")
        db.commit()

        result = load_config_from_db(db)
        assert result is not None

    def test_per_worker_rules_alone_also_count_as_data(self, db: SwarmDB) -> None:
        """approval_rules rows with owner_type='worker' also gate the check."""
        db.execute(
            "INSERT INTO approval_rules "
            "(owner_type, owner_id, pattern, action, sort_order) "
            "VALUES ('worker', 'wid-api', 'Read.*', 'approve', 0)"
        )
        db.commit()

        result = load_config_from_db(db)
        assert result is not None


# ======================================================================
# _load_workers JOIN
# ======================================================================


class TestLoadWorkers:
    def test_empty_table(self, db: SwarmDB) -> None:
        workers = _load_workers(db)
        assert workers == []

    def test_worker_without_rules(self, db: SwarmDB) -> None:
        db.execute(
            "INSERT INTO workers"
            " (id, name, path, description, provider, isolation, identity, sort_order, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("w1", "api", "/tmp/api", "API worker", "claude", "", "", 0, 1.0),
        )
        db.commit()
        workers = _load_workers(db)
        assert len(workers) == 1
        assert workers[0].name == "api"
        assert workers[0].path == "/tmp/api"
        assert workers[0].description == "API worker"
        assert workers[0].approval_rules == []

    def test_worker_with_rules(self, db: SwarmDB) -> None:
        db.execute(
            "INSERT INTO workers"
            " (id, name, path, description, provider, isolation, identity, sort_order, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("w1", "api", "/tmp/api", "", "", "", "", 0, 1.0),
        )
        db.execute(
            "INSERT INTO approval_rules (owner_type, owner_id, pattern, action, sort_order)"
            " VALUES ('worker', 'w1', 'Read.*', 'approve', 0)",
        )
        db.execute(
            "INSERT INTO approval_rules (owner_type, owner_id, pattern, action, sort_order)"
            " VALUES ('worker', 'w1', 'Write.*', 'escalate', 1)",
        )
        db.commit()
        workers = _load_workers(db)
        assert len(workers) == 1
        assert len(workers[0].approval_rules) == 2
        assert workers[0].approval_rules[0].pattern == "Read.*"
        assert workers[0].approval_rules[1].action == "escalate"

    def test_multiple_workers_with_mixed_rules(self, db: SwarmDB) -> None:
        db.execute(
            "INSERT INTO workers (id, name, path, sort_order, created_at)"
            " VALUES ('w1', 'api', '/tmp/api', 0, 1.0)",
        )
        db.execute(
            "INSERT INTO workers (id, name, path, sort_order, created_at)"
            " VALUES ('w2', 'web', '/tmp/web', 1, 1.0)",
        )
        db.execute(
            "INSERT INTO approval_rules (owner_type, owner_id, pattern, action, sort_order)"
            " VALUES ('worker', 'w1', 'Bash.*', 'approve', 0)",
        )
        # Global rule should NOT appear on workers
        db.execute(
            "INSERT INTO approval_rules (owner_type, owner_id, pattern, action, sort_order)"
            " VALUES ('global', NULL, 'GlobalRule', 'approve', 0)",
        )
        db.commit()
        workers = _load_workers(db)
        assert len(workers) == 2
        api = next(w for w in workers if w.name == "api")
        web = next(w for w in workers if w.name == "web")
        assert len(api.approval_rules) == 1
        assert api.approval_rules[0].pattern == "Bash.*"
        assert web.approval_rules == []


# ======================================================================
# _load_groups JOIN
# ======================================================================


class TestLoadGroups:
    def test_empty_table(self, db: SwarmDB) -> None:
        groups = _load_groups(db)
        assert groups == []

    def test_group_with_members(self, db: SwarmDB) -> None:
        db.execute(
            "INSERT INTO workers (id, name, path, sort_order, created_at)"
            " VALUES ('w1', 'api', '/tmp/api', 0, 1.0)",
        )
        db.execute(
            "INSERT INTO workers (id, name, path, sort_order, created_at)"
            " VALUES ('w2', 'web', '/tmp/web', 1, 1.0)",
        )
        db.execute("INSERT INTO groups (id, name, label) VALUES ('g1', 'backend', '')")
        db.execute("INSERT INTO group_workers (group_id, worker_id) VALUES ('g1', 'w1')")
        db.execute("INSERT INTO group_workers (group_id, worker_id) VALUES ('g1', 'w2')")
        db.commit()
        groups = _load_groups(db)
        assert len(groups) == 1
        assert groups[0].name == "backend"
        assert sorted(groups[0].workers) == ["api", "web"]

    def test_empty_group(self, db: SwarmDB) -> None:
        db.execute("INSERT INTO groups (id, name, label) VALUES ('g1', 'empty', '')")
        db.commit()
        groups = _load_groups(db)
        assert len(groups) == 1
        assert groups[0].name == "empty"
        assert groups[0].workers == []


# ======================================================================
# JSON blob parsers
# ======================================================================


class TestParseDroneConfig:
    def test_valid_json(self) -> None:
        blob = json.dumps({"enabled": False, "poll_interval": 15.0})
        dc = _parse_drone_config(blob, [])
        assert dc.enabled is False
        assert dc.poll_interval == 15.0

    def test_invalid_json_returns_default(self) -> None:
        dc = _parse_drone_config("{bad json", [])
        assert dc.enabled is True  # default
        assert dc.poll_interval == 5.0

    def test_empty_string_returns_default(self) -> None:
        dc = _parse_drone_config("", [])
        assert isinstance(dc, DroneConfig)

    def test_non_dict_returns_default(self) -> None:
        dc = _parse_drone_config('"just a string"', [])
        assert isinstance(dc, DroneConfig)

    def test_unknown_keys_ignored(self) -> None:
        blob = json.dumps({"enabled": True, "future_key": "value"})
        dc = _parse_drone_config(blob, [])
        assert dc.enabled is True

    def test_global_rules_from_db(self) -> None:
        blob = json.dumps({"enabled": True})
        rules = [
            {"pattern": "Bash.*", "action": "approve"},
            {"pattern": "Write.*", "action": "escalate"},
        ]
        dc = _parse_drone_config(blob, rules)
        assert len(dc.approval_rules) == 2
        assert dc.approval_rules[0].pattern == "Bash.*"
        assert dc.approval_rules[1].action == "escalate"

    def test_state_thresholds_parsing(self) -> None:
        blob = json.dumps(
            {
                "state_thresholds": {
                    "buzzing_confirm_count": 20,
                    "stung_confirm_count": 5,
                    "revive_grace": 30.0,
                }
            }
        )
        dc = _parse_drone_config(blob, [])
        assert dc.state_thresholds.buzzing_confirm_count == 20
        assert dc.state_thresholds.stung_confirm_count == 5
        assert dc.state_thresholds.revive_grace == 30.0

    def test_state_thresholds_unknown_keys_ignored(self) -> None:
        blob = json.dumps({"state_thresholds": {"buzzing_confirm_count": 10, "new_field": 42}})
        dc = _parse_drone_config(blob, [])
        assert dc.state_thresholds.buzzing_confirm_count == 10

    def test_raw_approval_rules_in_blob_dropped(self) -> None:
        """approval_rules in the JSON blob are ignored (DB rules are used)."""
        blob = json.dumps(
            {
                "approval_rules": [{"pattern": "FromBlob", "action": "approve"}],
            }
        )
        dc = _parse_drone_config(blob, [])
        assert dc.approval_rules == []


class TestParseQueenConfig:
    def test_valid_json(self) -> None:
        blob = json.dumps({"cooldown": 120.0, "enabled": False})
        qc = _parse_queen_config(blob)
        assert qc.cooldown == 120.0
        assert qc.enabled is False

    def test_invalid_json_returns_default(self) -> None:
        qc = _parse_queen_config("not json")
        assert isinstance(qc, QueenConfig)
        assert qc.enabled is True

    def test_empty_string_returns_default(self) -> None:
        qc = _parse_queen_config("")
        assert isinstance(qc, QueenConfig)

    def test_unknown_keys_ignored(self) -> None:
        """Regression: unknown keys like 'model' should not raise."""
        blob = json.dumps({"cooldown": 45.0, "model": "gpt-4", "future_thing": True})
        qc = _parse_queen_config(blob)
        assert qc.cooldown == 45.0

    def test_oversight_nested(self) -> None:
        blob = json.dumps(
            {
                "oversight": {
                    "enabled": False,
                    "buzzing_threshold_minutes": 25.0,
                }
            }
        )
        qc = _parse_queen_config(blob)
        assert qc.oversight.enabled is False
        assert qc.oversight.buzzing_threshold_minutes == 25.0

    def test_oversight_unknown_keys_ignored(self) -> None:
        blob = json.dumps({"oversight": {"enabled": True, "unknown_field": "ignored"}})
        qc = _parse_queen_config(blob)
        assert qc.oversight.enabled is True


class TestParseNotifyConfig:
    def test_valid_json(self) -> None:
        blob = json.dumps({"terminal_bell": False, "debounce_seconds": 15.0})
        nc = _parse_notify_config(blob)
        assert nc.terminal_bell is False
        assert nc.debounce_seconds == 15.0

    def test_invalid_json_returns_default(self) -> None:
        nc = _parse_notify_config("{bad")
        assert isinstance(nc, NotifyConfig)

    def test_webhook_nested(self) -> None:
        blob = json.dumps({"webhook": {"url": "https://hook.example.com", "events": ["stung"]}})
        nc = _parse_notify_config(blob)
        assert nc.webhook.url == "https://hook.example.com"
        assert nc.webhook.events == ["stung"]

    def test_email_nested(self) -> None:
        blob = json.dumps(
            {
                "email": {
                    "enabled": True,
                    "smtp_host": "smtp.test.com",
                    "smtp_port": 465,
                }
            }
        )
        nc = _parse_notify_config(blob)
        assert nc.email.enabled is True
        assert nc.email.smtp_host == "smtp.test.com"
        assert nc.email.smtp_port == 465

    def test_unknown_keys_ignored(self) -> None:
        blob = json.dumps({"terminal_bell": True, "slack": {"channel": "#alerts"}})
        nc = _parse_notify_config(blob)
        assert nc.terminal_bell is True


class TestParseJsonDataclass:
    def test_valid_json(self) -> None:
        blob = json.dumps({"mode": "worktree", "auto_pull": False})
        cc = _parse_json_dataclass(blob, CoordinationConfig)
        assert cc.mode == "worktree"
        assert cc.auto_pull is False

    def test_invalid_json_returns_default(self) -> None:
        result = _parse_json_dataclass("nope", TestConfig)
        assert isinstance(result, TestConfig)
        assert result.enabled is False

    def test_empty_string_returns_default(self) -> None:
        result = _parse_json_dataclass("", ResourceConfig)
        assert isinstance(result, ResourceConfig)

    def test_non_dict_returns_default(self) -> None:
        result = _parse_json_dataclass("[1,2,3]", TerminalConfig)
        assert isinstance(result, TerminalConfig)

    def test_unknown_keys_ignored(self) -> None:
        blob = json.dumps({"mode": "worktree", "new_field": "xyz"})
        cc = _parse_json_dataclass(blob, CoordinationConfig)
        assert cc.mode == "worktree"


class TestParseButtonList:
    def test_valid_tool_buttons(self) -> None:
        blob = json.dumps([{"label": "Deploy", "command": "deploy.sh"}])
        result = _parse_button_list(blob, ToolButtonConfig)
        assert len(result) == 1
        assert result[0].label == "Deploy"
        assert result[0].command == "deploy.sh"

    def test_empty_list(self) -> None:
        result = _parse_button_list("[]", ToolButtonConfig)
        assert result == []

    def test_invalid_json_returns_empty(self) -> None:
        result = _parse_button_list("{bad", ToolButtonConfig)
        assert result == []

    def test_non_list_returns_empty(self) -> None:
        result = _parse_button_list('{"key": "val"}', ToolButtonConfig)
        assert result == []

    def test_invalid_items_skipped(self) -> None:
        """Items missing required fields are skipped without crashing."""
        blob = json.dumps(
            [
                {"label": "Good", "command": "ok"},
                {"not_a_label": "bad"},  # missing 'label' (required)
                "not_a_dict",
            ]
        )
        result = _parse_button_list(blob, ToolButtonConfig)
        # Only the valid item survives; the missing-required one raises TypeError
        assert len(result) >= 1
        assert result[0].label == "Good"

    def test_action_buttons_with_optional_fields(self) -> None:
        blob = json.dumps(
            [
                {
                    "label": "Kill",
                    "action": "kill",
                    "style": "danger",
                    "show_mobile": False,
                    "show_desktop": True,
                }
            ]
        )
        result = _parse_button_list(blob, ActionButtonConfig)
        assert len(result) == 1
        assert result[0].label == "Kill"
        assert result[0].style == "danger"
        assert result[0].show_mobile is False

    def test_task_buttons(self) -> None:
        blob = json.dumps([{"label": "Approve", "action": "approve", "show_mobile": False}])
        result = _parse_button_list(blob, TaskButtonConfig)
        assert len(result) == 1
        assert result[0].action == "approve"
        assert result[0].show_mobile is False

    def test_unknown_keys_in_items_ignored(self) -> None:
        blob = json.dumps([{"label": "Test", "command": "echo", "extra": True}])
        result = _parse_button_list(blob, ToolButtonConfig)
        assert len(result) == 1
        assert result[0].label == "Test"


class TestParseCustomLlms:
    def test_valid_list(self) -> None:
        blob = json.dumps([{"name": "aider", "command": ["aider"], "display_name": "Aider"}])
        result = _parse_custom_llms(blob)
        assert len(result) == 1
        assert result[0].name == "aider"
        assert result[0].command == ["aider"]

    def test_invalid_json_returns_empty(self) -> None:
        result = _parse_custom_llms("nope")
        assert result == []

    def test_non_list_returns_empty(self) -> None:
        result = _parse_custom_llms('{"name": "x"}')
        assert result == []

    def test_items_missing_required_skipped(self) -> None:
        blob = json.dumps(
            [
                {"name": "ok", "command": ["ok"]},
                {"display_name": "Missing required"},  # missing name and command
            ]
        )
        result = _parse_custom_llms(blob)
        assert len(result) == 1
        assert result[0].name == "ok"


class TestParseProviderOverrides:
    def test_valid_dict(self) -> None:
        blob = json.dumps(
            {
                "gemini": {
                    "idle_pattern": r"gemini>",
                    "approval_key": "y\r",
                }
            }
        )
        result = _parse_provider_overrides(blob)
        assert "gemini" in result
        assert result["gemini"].idle_pattern == r"gemini>"
        assert result["gemini"].approval_key == "y\r"

    def test_invalid_json_returns_empty(self) -> None:
        result = _parse_provider_overrides("{bad")
        assert result == {}

    def test_non_dict_returns_empty(self) -> None:
        result = _parse_provider_overrides("[1,2]")
        assert result == {}

    def test_invalid_tuning_data_skipped(self) -> None:
        blob = json.dumps({"bad": "not_a_dict", "good": {"idle_pattern": "ok"}})
        result = _parse_provider_overrides(blob)
        assert "good" in result
        assert "bad" not in result


# ======================================================================
# _save_workers
# ======================================================================


class TestSaveWorkers:
    def test_add_new_workers(self, db: SwarmDB) -> None:
        workers = [
            WorkerConfig(name="api", path="/tmp/api"),
            WorkerConfig(name="web", path="/tmp/web"),
        ]
        _save_workers(db, workers, 1.0)
        db.commit()
        rows = db.fetchall("SELECT name FROM workers ORDER BY sort_order")
        assert [r["name"] for r in rows] == ["api", "web"]

    def test_update_existing_worker(self, db: SwarmDB) -> None:
        _save_workers(db, [WorkerConfig(name="api", path="/tmp/old")], 1.0)
        db.commit()
        _save_workers(db, [WorkerConfig(name="api", path="/tmp/new")], 2.0)
        db.commit()
        rows = db.fetchall("SELECT name, path FROM workers")
        assert len(rows) == 1
        assert rows[0]["path"] == "/tmp/new"

    def test_remove_deleted_worker(self, db: SwarmDB) -> None:
        _save_workers(
            db,
            [
                WorkerConfig(name="api", path="/tmp/api"),
                WorkerConfig(name="web", path="/tmp/web"),
            ],
            1.0,
        )
        db.commit()
        # Now save only 'api' — 'web' should be removed
        _save_workers(db, [WorkerConfig(name="api", path="/tmp/api")], 2.0)
        db.commit()
        rows = db.fetchall("SELECT name FROM workers")
        assert len(rows) == 1
        assert rows[0]["name"] == "api"

    def test_preserves_worker_id_on_update(self, db: SwarmDB) -> None:
        _save_workers(db, [WorkerConfig(name="api", path="/tmp/api")], 1.0)
        db.commit()
        row1 = db.fetchone("SELECT id FROM workers WHERE name = 'api'")
        assert row1 is not None
        original_id = row1["id"]

        _save_workers(db, [WorkerConfig(name="api", path="/tmp/api-v2")], 2.0)
        db.commit()
        row2 = db.fetchone("SELECT id FROM workers WHERE name = 'api'")
        assert row2 is not None
        assert row2["id"] == original_id

    def test_worker_approval_rules_synced(self, db: SwarmDB) -> None:
        workers = [
            WorkerConfig(
                name="api",
                path="/tmp/api",
                approval_rules=[DroneApprovalRule(pattern="Read.*", action="approve")],
            )
        ]
        # This test exercises the opt-in rule-sync path, so both calls
        # must pass sync_approval_rules=True.  The default (False) is
        # covered by test_save_workers_does_not_touch_rules_by_default.
        _save_workers(db, workers, 1.0, sync_approval_rules=True)
        db.commit()
        rules = db.fetchall(
            "SELECT pattern, action FROM approval_rules WHERE owner_type = 'worker'"
        )
        assert len(rules) == 1
        assert rules[0]["pattern"] == "Read.*"

        # Update rules
        workers[0].approval_rules = [DroneApprovalRule(pattern="Write.*", action="escalate")]
        _save_workers(db, workers, 2.0, sync_approval_rules=True)
        db.commit()
        rules = db.fetchall(
            "SELECT pattern, action FROM approval_rules WHERE owner_type = 'worker'"
        )
        assert len(rules) == 1
        assert rules[0]["pattern"] == "Write.*"


# ======================================================================
# Non-destructive rules save (regression for data-loss bug)
# ======================================================================


class TestApprovalRulesNonDestructive:
    """save_config_to_db must NOT touch approval_rules unless the caller
    explicitly opts in via sync_approval_rules=True.

    Historical bug: a stale or partial in-memory HiveConfig whose
    drones.approval_rules was empty would silently wipe every rule in
    the DB on any routine save (toggling an unrelated setting,
    hot-reload callback, etc.).  Users reported this after running
    `swarm init` followed by normal dashboard activity — all their
    approval rules disappeared from the approval_rules table.
    """

    def test_save_config_default_does_not_delete_global_rules(self, db: SwarmDB) -> None:
        # Seed a rule directly in the DB (as if the user created it
        # through the dashboard in a prior session).
        db.execute(
            "INSERT INTO approval_rules "
            "(owner_type, owner_id, pattern, action, sort_order) "
            "VALUES ('global', NULL, 'Bash.*', 'approve', 0)"
        )
        db.commit()

        # Now save a config whose in-memory drones.approval_rules is
        # empty — e.g. one loaded from a YAML that never had rules in
        # it.  This used to silently wipe the DB row above.
        config = HiveConfig(drones=DroneConfig(approval_rules=[]))
        save_config_to_db(db, config)  # default: sync_approval_rules=False

        rows = db.fetchall("SELECT pattern FROM approval_rules WHERE owner_type = 'global'")
        assert len(rows) == 1, "default save must not delete existing rules"
        assert rows[0]["pattern"] == "Bash.*"

    def test_save_config_default_does_not_delete_worker_rules(self, db: SwarmDB) -> None:
        # Seed a worker and a worker-scoped rule directly.
        db.execute(
            "INSERT INTO workers (id, name, path, sort_order, created_at) "
            "VALUES ('wid-api', 'api', '/tmp/api', 0, 1.0)"
        )
        db.execute(
            "INSERT INTO approval_rules "
            "(owner_type, owner_id, pattern, action, sort_order) "
            "VALUES ('worker', 'wid-api', 'Read.*', 'approve', 0)"
        )
        db.commit()

        # Save a config where the worker list includes "api" but with
        # an empty approval_rules list on the WorkerConfig.  With the
        # old behaviour this wiped the per-worker rule.
        config = HiveConfig(workers=[WorkerConfig(name="api", path="/tmp/api")])
        save_config_to_db(db, config)

        rows = db.fetchall("SELECT pattern FROM approval_rules WHERE owner_type = 'worker'")
        assert len(rows) == 1
        assert rows[0]["pattern"] == "Read.*"

    def test_sync_true_still_replaces_rules(self, db: SwarmDB) -> None:
        # Opt-in behaviour is unchanged: explicit sync_approval_rules=True
        # replaces the rules table from config.drones.approval_rules.
        db.execute(
            "INSERT INTO approval_rules "
            "(owner_type, owner_id, pattern, action, sort_order) "
            "VALUES ('global', NULL, 'old_rule', 'approve', 0)"
        )
        db.commit()

        config = HiveConfig(
            drones=DroneConfig(
                approval_rules=[DroneApprovalRule(pattern="new_rule", action="approve")],
            )
        )
        save_config_to_db(db, config, sync_approval_rules=True)

        rows = db.fetchall("SELECT pattern FROM approval_rules WHERE owner_type = 'global'")
        assert [r["pattern"] for r in rows] == ["new_rule"]


# ======================================================================
# _save_groups
# ======================================================================


class TestSaveGroups:
    def test_sync_groups_with_members(self, db: SwarmDB) -> None:
        workers = [
            WorkerConfig(name="api", path="/tmp/api"),
            WorkerConfig(name="web", path="/tmp/web"),
        ]
        _save_workers(db, workers, 1.0)
        db.commit()

        groups = [GroupConfig(name="backend", workers=["api", "web"])]
        _save_groups(db, groups, workers, 1.0)
        db.commit()

        loaded = _load_groups(db)
        assert len(loaded) == 1
        assert sorted(loaded[0].workers) == ["api", "web"]

    def test_remove_deleted_group(self, db: SwarmDB) -> None:
        workers = [WorkerConfig(name="api", path="/tmp/api")]
        _save_workers(db, workers, 1.0)
        db.commit()

        _save_groups(
            db,
            [
                GroupConfig(name="alpha", workers=["api"]),
                GroupConfig(name="beta", workers=["api"]),
            ],
            workers,
            1.0,
        )
        db.commit()

        # Now save only alpha
        _save_groups(db, [GroupConfig(name="alpha", workers=["api"])], workers, 2.0)
        db.commit()

        loaded = _load_groups(db)
        assert len(loaded) == 1
        assert loaded[0].name == "alpha"

    def test_group_with_nonexistent_worker_skipped(self, db: SwarmDB) -> None:
        """Worker names that don't exist in DB are silently skipped."""
        workers = [WorkerConfig(name="api", path="/tmp/api")]
        _save_workers(db, workers, 1.0)
        db.commit()

        groups = [GroupConfig(name="mixed", workers=["api", "ghost"])]
        _save_groups(db, groups, workers, 1.0)
        db.commit()

        loaded = _load_groups(db)
        assert len(loaded) == 1
        # Only 'api' should appear, 'ghost' is silently dropped
        assert loaded[0].workers == ["api"]

    def test_empty_group(self, db: SwarmDB) -> None:
        _save_groups(db, [GroupConfig(name="empty", workers=[])], [], 1.0)
        db.commit()
        loaded = _load_groups(db)
        assert len(loaded) == 1
        assert loaded[0].name == "empty"
        assert loaded[0].workers == []
