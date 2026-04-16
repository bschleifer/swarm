"""Tests for the SkillsStore + workflows.get_skill_command integration."""

from __future__ import annotations

import pytest

from swarm.db.core import SwarmDB
from swarm.db.skills_store import SkillRecord, SkillsStore
from swarm.tasks.task import TaskType
from swarm.tasks.workflows import (
    _DEFAULT_SKILL_DESCRIPTIONS,
    SKILL_COMMANDS,
    attach_skills_store,
    detach_skills_store,
    get_skill_command,
)


@pytest.fixture
def store(tmp_path):
    db = SwarmDB(tmp_path / "swarm.db")
    yield SkillsStore(db)
    db.close()
    detach_skills_store()


class TestSkillsStoreCRUD:
    def test_list_empty(self, store):
        assert store.list_all() == []

    def test_upsert_creates_row(self, store):
        record = store.upsert(
            "/my-skill",
            description="Custom skill",
            task_types=["bug", "chore"],
        )
        assert record.name == "/my-skill"
        assert record.description == "Custom skill"
        assert record.task_types == ["bug", "chore"]
        assert record.usage_count == 0

    def test_upsert_updates_without_resetting_usage(self, store):
        store.upsert("/foo", description="v1", task_types=["bug"])
        store.record_usage("/foo")
        store.record_usage("/foo")
        updated = store.upsert("/foo", description="v2", task_types=["feature"])
        assert updated.description == "v2"
        assert updated.task_types == ["feature"]
        assert updated.usage_count == 2

    def test_delete(self, store):
        store.upsert("/foo", description="", task_types=["bug"])
        assert store.delete("/foo") is True
        assert store.get("/foo") is None

    def test_delete_missing_returns_false(self, store):
        assert store.delete("/nope") is False

    def test_record_usage_bumps_counter(self, store):
        store.upsert("/foo", description="", task_types=["bug"])
        store.record_usage("/foo")
        store.record_usage("/foo")
        record = store.get("/foo")
        assert record is not None
        assert record.usage_count == 2
        assert record.last_used_at is not None

    def test_record_usage_unknown_noops(self, store):
        # Should not raise
        store.record_usage("/never-registered")

    def test_seed_defaults_inserts_once(self, store):
        defaults = {"/x": ("desc-x", ["bug"]), "/y": ("desc-y", ["feature"])}
        added_first = store.seed_defaults(defaults)
        added_second = store.seed_defaults(defaults)
        assert added_first == 2
        assert added_second == 0  # idempotent

    def test_seed_defaults_preserves_existing_description(self, store):
        """Seeding doesn't clobber operator customizations."""
        store.upsert("/foo", description="operator-set", task_types=["bug"])
        store.seed_defaults({"/foo": ("default-text", ["feature"])})
        record = store.get("/foo")
        assert record is not None
        assert record.description == "operator-set"


class TestGetSkillCommandIntegration:
    def test_no_store_attached_uses_defaults(self):
        detach_skills_store()
        result = get_skill_command(TaskType.BUG)
        assert result == SKILL_COMMANDS[TaskType.BUG]

    def test_store_lookup_overrides_defaults(self, store):
        # Seed store with a custom skill for BUG
        store.upsert("/custom-bug-workflow", description="", task_types=["bug"])
        attach_skills_store(store)
        result = get_skill_command(TaskType.BUG)
        assert result == "/custom-bug-workflow"

    def test_default_seeding_occurs_on_attach(self, store):
        """attach_skills_store seeds the built-in defaults so a fresh DB
        still returns the canonical mapping without operator action."""
        attach_skills_store(store)
        names = {s.name for s in store.list_all()}
        assert "/fix-and-ship" in names
        assert "/feature" in names
        assert "/verify" in names

    def test_usage_counter_increments_on_lookup(self, store):
        attach_skills_store(store)
        before = store.get("/fix-and-ship")
        assert before is not None
        assert before.usage_count == 0

        get_skill_command(TaskType.BUG)
        get_skill_command(TaskType.BUG)

        after = store.get("/fix-and-ship")
        assert after is not None
        assert after.usage_count == 2

    def test_falls_back_when_no_skill_covers_type(self, store):
        # Store attached but empty — fallback to built-in defaults
        attach_skills_store(store)
        # After attach the store is seeded, so remove the feature skill
        store.delete("/feature")
        # But defaults still have TaskType.FEATURE → /feature
        assert get_skill_command(TaskType.FEATURE) == SKILL_COMMANDS[TaskType.FEATURE]


class TestApiListSkills:
    @pytest.mark.asyncio
    async def test_api_returns_defaults_when_no_store(self, tmp_path):
        from aiohttp.test_utils import TestClient, TestServer

        from swarm.server.api import create_app
        from tests.conftest import make_daemon

        detach_skills_store()
        daemon = make_daemon()
        app = create_app(daemon, enable_web=False)
        async with TestClient(TestServer(app)) as c:
            resp = await c.get("/api/skills", headers={"X-Requested-With": "test"})
            assert resp.status == 200
            data = await resp.json()
            names = {s["name"] for s in data["skills"]}
            assert "/fix-and-ship" in names
            assert "/feature" in names

    @pytest.mark.asyncio
    async def test_api_returns_store_rows_when_attached(self, tmp_path, store):
        from aiohttp.test_utils import TestClient, TestServer

        from swarm.server.api import create_app
        from tests.conftest import make_daemon

        store.upsert("/db-registered", description="from DB", task_types=["bug"])
        attach_skills_store(store)

        daemon = make_daemon()
        app = create_app(daemon, enable_web=False)
        async with TestClient(TestServer(app)) as c:
            resp = await c.get("/api/skills", headers={"X-Requested-With": "test"})
            assert resp.status == 200
            data = await resp.json()
            names = {s["name"] for s in data["skills"]}
            assert "/db-registered" in names


class TestSkillRecordSerialization:
    def test_to_api_round_trip(self):
        record = SkillRecord(
            name="/foo",
            description="x",
            task_types=["bug"],
            usage_count=3,
            last_used_at=1234.5,
            created_at=1000.0,
        )
        api = record.to_api()
        assert api["name"] == "/foo"
        assert api["usage_count"] == 3
        assert api["last_used_at"] == 1234.5


def test_default_descriptions_cover_defaults():
    """Every default skill has a matching description for seeding."""
    for task_type, command in SKILL_COMMANDS.items():
        # Built-in defaults should have descriptions so the registry
        # renders useful info on first boot.
        if task_type in (TaskType.BUG, TaskType.FEATURE, TaskType.VERIFY):
            assert command in _DEFAULT_SKILL_DESCRIPTIONS
