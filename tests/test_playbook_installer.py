"""Phase 3: install_worker_playbooks → .claude/skills/pb-<name>/SKILL.md."""

from __future__ import annotations

import pytest

from swarm.db.core import SwarmDB
from swarm.db.playbook_store import PlaybookStore
from swarm.playbooks.installer import install_worker_playbooks
from swarm.playbooks.models import Playbook, PlaybookStatus, project_scope, worker_scope


@pytest.fixture
def store(tmp_path):
    return PlaybookStore(SwarmDB(tmp_path / "swarm.db"))


@pytest.fixture
def worker_dir(tmp_path):
    d = tmp_path / "wkr"
    d.mkdir()
    return d


def _active(store, name, **kw):
    return store.create(
        Playbook(
            name=name,
            title=f"{name} title",
            trigger=f"use {name} when X",
            body=f"1. step\n2. step for {name}",
            status=PlaybookStatus.ACTIVE,
            **kw,
        )
    )


def test_writes_skill_md_for_active_in_scope(store, worker_dir):
    _active(store, "retry-backoff")
    n = install_worker_playbooks(worker_dir, store, worker_name="api")
    assert n == 1
    md = worker_dir / ".claude" / "skills" / "pb-retry-backoff" / "SKILL.md"
    assert md.is_file()
    text = md.read_text()
    assert "name: pb-retry-backoff" in text
    assert "use retry-backoff when X" in text  # trigger drives description
    assert "step for retry-backoff" in text  # body present


def test_candidates_not_installed(store, worker_dir):
    store.create(Playbook(name="cand", body="b", status=PlaybookStatus.CANDIDATE))
    assert install_worker_playbooks(worker_dir, store, worker_name="api") == 0
    assert not (worker_dir / ".claude" / "skills" / "pb-cand").exists()


def test_scope_filtering(store, worker_dir):
    _active(store, "g")  # global → installed
    _active(store, "mine", scope=worker_scope("api"))  # worker:api → installed
    _active(store, "theirs", scope=worker_scope("web"))  # worker:web → skipped
    _active(store, "thisrepo", scope=project_scope("wkr"))  # project == dir name → installed
    _active(store, "otherrepo", scope=project_scope("hub"))  # → skipped

    install_worker_playbooks(worker_dir, store, worker_name="api")
    skills = {p.name for p in (worker_dir / ".claude" / "skills").glob("pb-*")}
    assert skills == {"pb-g", "pb-mine", "pb-thisrepo"}


def test_idempotent_and_stale_cleanup(store, worker_dir):
    _active(store, "keep")
    pb = _active(store, "drop")
    install_worker_playbooks(worker_dir, store, worker_name="api")
    skills_dir = worker_dir / ".claude" / "skills"
    assert (skills_dir / "pb-drop").exists()

    # Retire one, re-run: idempotent for survivors, stale dir removed.
    store.retire("drop", "superseded")
    n = install_worker_playbooks(worker_dir, store, worker_name="api")
    assert n == 1
    assert (skills_dir / "pb-keep" / "SKILL.md").is_file()
    assert not (skills_dir / "pb-drop").exists()
    assert pb  # silence unused


def test_install_noop_does_not_touch_bundled_skills(store, worker_dir):
    # A non-pb skill dir must survive a playbook install run.
    bundled = worker_dir / ".claude" / "skills" / "swarm-checkpoint"
    bundled.mkdir(parents=True)
    (bundled / "SKILL.md").write_text("bundled")
    _active(store, "p1")
    install_worker_playbooks(worker_dir, store, worker_name="api")
    assert (bundled / "SKILL.md").read_text() == "bundled"  # untouched
