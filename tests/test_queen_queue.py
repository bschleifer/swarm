"""Tests for queen/queue.py — QueenCallQueue concurrency control."""

from __future__ import annotations

import asyncio

import pytest

from swarm.queen.queue import QueenCallQueue, QueenCallRequest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_request(
    dedup_key: str = "escalation:api",
    call_type: str = "escalation",
    worker_name: str | None = "api",
    worker_state: str | None = "RESTING",
    force: bool = False,
    result: object = "ok",
    delay: float = 0.0,
) -> QueenCallRequest:
    """Build a QueenCallRequest with a simple async factory."""

    async def factory():
        if delay > 0:
            await asyncio.sleep(delay)
        return result

    return QueenCallRequest(
        call_type=call_type,
        coro_factory=factory,
        worker_name=worker_name,
        worker_state_at_enqueue=worker_state,
        dedup_key=dedup_key,
        force=force,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestFIFOOrdering:
    """Calls should be drained in FIFO order."""

    @pytest.mark.asyncio
    async def test_fifo_order(self):
        order: list[str] = []

        def factory(label: str):
            async def run():
                order.append(label)
                return label

            return run

        q = QueenCallQueue(max_concurrent=1)
        reqs = []
        for label in ["a", "b", "c"]:
            req = QueenCallRequest(
                call_type="escalation",
                coro_factory=factory(label),
                worker_name=label,
                worker_state_at_enqueue="RESTING",
                dedup_key=f"esc:{label}",
                force=False,
            )
            reqs.append(req)
            await q.submit(req)

        # Wait for all to complete
        for req in reqs:
            await req.future

        assert order == ["a", "b", "c"]


class TestConcurrencyLimit:
    """Only max_concurrent calls should run simultaneously."""

    @pytest.mark.asyncio
    async def test_concurrency_limit(self):
        max_running = 0
        current_running = 0
        lock = asyncio.Lock()

        def factory(label: str):
            async def run():
                nonlocal max_running, current_running
                async with lock:
                    current_running += 1
                    if current_running > max_running:
                        max_running = current_running
                await asyncio.sleep(0.02)
                async with lock:
                    current_running -= 1
                return label

            return run

        q = QueenCallQueue(max_concurrent=2)
        reqs = []
        for i in range(4):
            req = QueenCallRequest(
                call_type="escalation",
                coro_factory=factory(f"w{i}"),
                worker_name=f"w{i}",
                worker_state_at_enqueue="RESTING",
                dedup_key=f"esc:w{i}",
                force=False,
            )
            reqs.append(req)
            await q.submit(req)

        for req in reqs:
            await req.future

        assert max_running == 2


class TestDedup:
    """Same dedup_key should not be submitted twice."""

    @pytest.mark.asyncio
    async def test_dedup_drops_duplicate(self):
        call_count = 0

        async def factory():
            nonlocal call_count
            call_count += 1
            await asyncio.sleep(0.05)
            return "done"

        q = QueenCallQueue(max_concurrent=2)

        req1 = QueenCallRequest(
            call_type="escalation",
            coro_factory=factory,
            worker_name="api",
            worker_state_at_enqueue="RESTING",
            dedup_key="esc:api",
            force=False,
        )
        req2 = QueenCallRequest(
            call_type="escalation",
            coro_factory=factory,
            worker_name="api",
            worker_state_at_enqueue="RESTING",
            dedup_key="esc:api",
            force=False,
        )

        await q.submit(req1)
        await q.submit(req2)

        await req1.future
        # req2 should have been resolved immediately as None
        assert req2.future.done()
        assert call_count == 1


class TestForceBypass:
    """force=True calls bypass queue and concurrency limit."""

    @pytest.mark.asyncio
    async def test_force_bypasses_queue(self):
        started: list[str] = []

        def factory(label: str):
            async def run():
                started.append(label)
                await asyncio.sleep(0.05)
                return label

            return run

        q = QueenCallQueue(max_concurrent=1)

        # Fill the single slot
        bg = QueenCallRequest(
            call_type="escalation",
            coro_factory=factory("bg"),
            worker_name="api",
            worker_state_at_enqueue="RESTING",
            dedup_key="esc:api",
            force=False,
        )
        await q.submit(bg)

        # Force call should start immediately despite slot being full
        force_req = QueenCallRequest(
            call_type="escalation",
            coro_factory=factory("force"),
            worker_name="web",
            worker_state_at_enqueue="RESTING",
            dedup_key="esc:web",
            force=True,
        )
        await q.submit(force_req)

        # Give tasks a tick to start
        await asyncio.sleep(0.01)
        assert "force" in started

        await bg.future
        await force_req.future


class TestStaleness:
    """Stale calls should be dropped at dequeue time."""

    @pytest.mark.asyncio
    async def test_stale_call_dropped(self):
        call_count = 0
        worker_states = {"api": "RESTING", "web": "RESTING"}

        def get_state(name: str) -> str | None:
            return worker_states.get(name)

        def factory(label: str):
            async def run():
                nonlocal call_count
                call_count += 1
                return label

            return run

        q = QueenCallQueue(max_concurrent=1, get_worker_state=get_state)

        # First call fills the slot
        r1 = QueenCallRequest(
            call_type="escalation",
            coro_factory=factory("api"),
            worker_name="api",
            worker_state_at_enqueue="RESTING",
            dedup_key="esc:api",
            force=False,
        )
        # Second call will be queued
        r2 = QueenCallRequest(
            call_type="escalation",
            coro_factory=factory("web"),
            worker_name="web",
            worker_state_at_enqueue="RESTING",
            dedup_key="esc:web",
            force=False,
        )

        await q.submit(r1)
        await q.submit(r2)

        # Change web's state before r1 finishes (making r2 stale)
        worker_states["web"] = "BUZZING"

        await r1.future
        # r2 should be resolved as None (dropped)
        result = await r2.future
        assert result is None
        assert call_count == 1  # Only r1 actually ran


class TestClearWorker:
    """clear_worker removes queued calls for a specific worker."""

    @pytest.mark.asyncio
    async def test_clear_worker_removes_queued(self):
        call_count = 0

        def factory(label: str):
            async def run():
                nonlocal call_count
                call_count += 1
                await asyncio.sleep(0.05)
                return label

            return run

        q = QueenCallQueue(max_concurrent=1)

        # Fill slot
        r1 = QueenCallRequest(
            call_type="escalation",
            coro_factory=factory("api"),
            worker_name="api",
            worker_state_at_enqueue="RESTING",
            dedup_key="esc:api",
            force=False,
        )
        # Queue another for web
        r2 = QueenCallRequest(
            call_type="completion",
            coro_factory=factory("web"),
            worker_name="web",
            worker_state_at_enqueue="RESTING",
            dedup_key="comp:web:t1",
            force=False,
        )

        await q.submit(r1)
        await q.submit(r2)

        # Clear web before it gets to run
        q.clear_worker("web")

        await r1.future
        # r2 should be resolved as None
        assert r2.future.done()
        assert await r2.future is None
        assert call_count == 1

        # Dedup key should be cleared
        assert not q.has_pending("comp:web:t1")


class TestStatusCallback:
    """on_status_change fires on every queue transition."""

    @pytest.mark.asyncio
    async def test_status_callback_fires(self):
        statuses: list[dict] = []

        def on_change(s):
            statuses.append(dict(s))

        q = QueenCallQueue(max_concurrent=1, on_status_change=on_change)

        req = _make_request(delay=0.02)
        await q.submit(req)
        await req.future

        # Should have at least 2 status changes: submit + completion
        assert len(statuses) >= 2
        # First status: 1 running
        assert statuses[0]["running"] == 1
        # Last status: 0 running, 0 queued
        assert statuses[-1]["running"] == 0
        assert statuses[-1]["queued"] == 0


class TestCancelAll:
    """cancel_all should clean up everything."""

    @pytest.mark.asyncio
    async def test_cancel_all(self):
        q = QueenCallQueue(max_concurrent=1)

        r1 = _make_request(dedup_key="esc:api", delay=1.0)
        r2 = _make_request(
            dedup_key="esc:web",
            worker_name="web",
            delay=1.0,
        )

        await q.submit(r1)
        await q.submit(r2)

        q.cancel_all()

        assert q.status()["running"] == 0
        assert q.status()["queued"] == 0
        assert not q.has_pending("esc:api")
        assert not q.has_pending("esc:web")


class TestSubmitAndWait:
    """submit_and_wait should return the coroutine result."""

    @pytest.mark.asyncio
    async def test_submit_and_wait_returns_result(self):
        q = QueenCallQueue(max_concurrent=2)
        req = _make_request(result={"action": "continue"})
        result = await q.submit_and_wait(req)
        assert result == {"action": "continue"}


class TestDrain:
    """Queued calls start after running ones finish."""

    @pytest.mark.asyncio
    async def test_queued_starts_after_running_finishes(self):
        order: list[str] = []

        def factory(label: str, delay: float = 0.0):
            async def run():
                if delay:
                    await asyncio.sleep(delay)
                order.append(label)
                return label

            return run

        q = QueenCallQueue(max_concurrent=1)

        r1 = QueenCallRequest(
            call_type="escalation",
            coro_factory=factory("first", 0.02),
            worker_name="api",
            worker_state_at_enqueue="RESTING",
            dedup_key="esc:api",
            force=False,
        )
        r2 = QueenCallRequest(
            call_type="escalation",
            coro_factory=factory("second"),
            worker_name="web",
            worker_state_at_enqueue="RESTING",
            dedup_key="esc:web",
            force=False,
        )

        await q.submit(r1)
        await q.submit(r2)

        # r2 should not run yet
        assert not r2.future.done()

        await r1.future
        await r2.future

        assert order == ["first", "second"]


class TestHasPending:
    """has_pending should reflect queued + running keys."""

    @pytest.mark.asyncio
    async def test_has_pending(self):
        q = QueenCallQueue(max_concurrent=1)
        req = _make_request(dedup_key="esc:api", delay=0.05)

        assert not q.has_pending("esc:api")
        await q.submit(req)
        assert q.has_pending("esc:api")

        await req.future
        assert not q.has_pending("esc:api")
