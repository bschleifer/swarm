"""Tests for PlaybookSynthesizer (playbook-synthesis-loop Phase 1)."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from swarm.config.models import PlaybookConfig
from swarm.db.core import SwarmDB
from swarm.db.playbook_store import PlaybookStore
from swarm.playbooks.synthesizer import PlaybookSynthesizer


@dataclass
class _Type:
    value: str


@dataclass
class _Task:
    id: str = "t-1"
    title: str = "Add retry to webhook sender"
    description: str = "Sender dropped events on 5xx"
    task_type: _Type = None  # type: ignore[assignment]

    def __post_init__(self):
        if self.task_type is None:
            self.task_type = _Type("feature")


class _Queen:
    """Configurable fake headless Queen; counts ask() calls."""

    def __init__(self, verdict=None, raises=False):
        self.verdict = verdict if verdict is not None else {"synthesize": False}
        self.raises = raises
        self.calls = 0

    async def ask(self, prompt, **kwargs):
        self.calls += 1
        if self.raises:
            raise RuntimeError("boom")
        return self.verdict


_GOOD = {
    "synthesize": True,
    "name": "Webhook Retry With Backoff",
    "title": "Webhook retry with backoff",
    "scope": "global",
    "trigger": "when an outbound sender drops events on 5xx",
    "body": "1. wrap send in retry\n2. exp backoff\n3. dead-letter after N",
    "confidence": 0.82,
}

_RESOLUTION = (
    "Added exponential-backoff retry around the webhook sender; 5xx now "
    "retries 4x then dead-letters. Regression test added; /check green."
)


def _synth(tmp_path, *, queen, cfg=None, drone_log=None, now=None):
    db = SwarmDB(tmp_path / "swarm.db")
    store = PlaybookStore(db)
    kwargs = {}
    if now is not None:
        kwargs["now"] = now
    return (
        PlaybookSynthesizer(
            queen=queen,
            store=store,
            config=cfg or PlaybookConfig(),
            drone_log=drone_log,
            **kwargs,
        ),
        store,
    )


@pytest.mark.asyncio
async def test_synthesize_creates_candidate(tmp_path):
    q = _Queen(_GOOD)
    s, store = _synth(tmp_path, queen=q)
    pb = await s.synthesize(_Task(), worker="swarm", resolution=_RESOLUTION)
    assert pb is not None
    assert pb.name == "webhook-retry-with-backoff"
    assert pb.status.value == "candidate"
    assert pb.provenance_task_ids == ["t-1"]
    assert pb.source_worker == "swarm"
    assert store.get("webhook-retry-with-backoff") is not None
    assert q.calls == 1


@pytest.mark.asyncio
async def test_decline_creates_nothing(tmp_path):
    q = _Queen({"synthesize": False})
    s, store = _synth(tmp_path, queen=q)
    assert await s.synthesize(_Task(), worker="swarm", resolution=_RESOLUTION) is None
    assert store.list() == []
    assert q.calls == 1


@pytest.mark.asyncio
async def test_memoized_one_call_per_worker_task(tmp_path):
    q = _Queen(_GOOD)
    s, _ = _synth(tmp_path, queen=q)
    t = _Task()
    first = await s.synthesize(t, worker="swarm", resolution=_RESOLUTION)
    second = await s.synthesize(t, worker="swarm", resolution=_RESOLUTION)
    assert first is not None
    assert second is None
    assert q.calls == 1  # second fire is memoized — no extra Queen call


@pytest.mark.asyncio
async def test_ineligible_task_type_skipped(tmp_path):
    q = _Queen(_GOOD)
    s, _ = _synth(tmp_path, queen=q)
    t = _Task(task_type=_Type("verify"))
    assert await s.synthesize(t, worker="swarm", resolution=_RESOLUTION) is None
    assert q.calls == 0  # gated before any Queen call


@pytest.mark.asyncio
async def test_short_resolution_skipped(tmp_path):
    q = _Queen(_GOOD)
    s, _ = _synth(tmp_path, queen=q)
    assert await s.synthesize(_Task(), worker="swarm", resolution="done") is None
    assert q.calls == 0


@pytest.mark.asyncio
async def test_rate_cap_enforced(tmp_path):
    q = _Queen(_GOOD)
    s, _ = _synth(tmp_path, queen=q, cfg=PlaybookConfig(max_synth_per_hour=1))
    a = await s.synthesize(_Task(id="t-1"), worker="swarm", resolution=_RESOLUTION)
    b = await s.synthesize(_Task(id="t-2"), worker="swarm", resolution=_RESOLUTION)
    assert a is not None
    assert b is None
    assert q.calls == 1  # second distinct task blocked by per-hour cap


@pytest.mark.asyncio
async def test_queen_error_is_safe(tmp_path):
    s, store = _synth(tmp_path, queen=_Queen(raises=True))
    assert await s.synthesize(_Task(), worker="swarm", resolution=_RESOLUTION) is None
    assert store.list() == []


@pytest.mark.asyncio
async def test_queen_error_dict_is_safe(tmp_path):
    s, store = _synth(tmp_path, queen=_Queen({"error": "Rate limited"}))
    assert await s.synthesize(_Task(), worker="swarm", resolution=_RESOLUTION) is None
    assert store.list() == []


@pytest.mark.asyncio
async def test_invalid_scope_defaults_global(tmp_path):
    bad = dict(_GOOD, scope="nonsense::x")
    s, _ = _synth(tmp_path, queen=_Queen(bad))
    pb = await s.synthesize(_Task(), worker="swarm", resolution=_RESOLUTION)
    assert pb is not None and pb.scope == "global"
