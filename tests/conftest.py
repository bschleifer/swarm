"""Shared test fixtures and helpers."""

from __future__ import annotations

import logging
from unittest.mock import patch

import pytest

from tests.fakes.process import FakeWorkerProcess
from swarm.worker.worker import Worker, WorkerState


@pytest.fixture(autouse=True, scope="session")
def _isolate_logging():
    """Prevent tests from writing to the production ``~/.swarm/swarm.log``.

    CLI tests invoke click commands that call ``setup_logging()`` which
    attaches a ``RotatingFileHandler`` pointing at ``~/.swarm/swarm.log``.
    We patch ``setup_logging`` to redirect all file output to ``/dev/null``
    so test warnings never pollute the production debug log.
    """
    import swarm.cli as _cli
    import swarm.logging as _swarm_logging

    _real_setup = _swarm_logging.setup_logging

    def _test_setup(level="WARNING", log_file=None, stderr=False):
        return _real_setup(level=level, log_file="/dev/null", stderr=False)

    with (
        patch.object(_swarm_logging, "setup_logging", _test_setup),
        patch.object(_cli, "setup_logging", _test_setup),
    ):
        # Also neutralise the logger right now for tests that never
        # call setup_logging but still emit warnings.
        logger = logging.getLogger("swarm")
        logger.handlers.clear()
        logger.addHandler(logging.NullHandler())
        logger.setLevel(logging.WARNING)
        yield


def make_worker(
    name: str = "api",
    state: WorkerState = WorkerState.BUZZING,
    process: FakeWorkerProcess | None = None,
    resting_since: float | None = None,
    revive_count: int = 0,
) -> Worker:
    """Create a Worker for testing.

    Parameters
    ----------
    name:
        Worker name.
    state:
        Initial worker state.
    process:
        Fake process for the worker. Defaults to a new ``FakeWorkerProcess``.
    resting_since:
        If set, overrides ``state_since`` (useful for escalation threshold tests).
    revive_count:
        Initial revive counter.
    """
    if process is None:
        process = FakeWorkerProcess(name=name)
    w = Worker(name=name, path="/tmp", process=process, state=state)
    if resting_since is not None:
        w.state_since = resting_since
    w.revive_count = revive_count
    return w
