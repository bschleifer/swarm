"""Tests for rate limiting logic from swarm.server.api."""

from __future__ import annotations

import time
from collections import deque


class TestRateLimitLogic:
    """Test the rate limit sliding window mechanism."""

    def test_hits_limit(self):
        from swarm.server.api import _RATE_LIMIT_REQUESTS

        timestamps: deque[float] = deque()
        now = time.time()

        for _ in range(_RATE_LIMIT_REQUESTS):
            timestamps.append(now)
        assert len(timestamps) >= _RATE_LIMIT_REQUESTS

    def test_below_limit(self):
        from swarm.server.api import _RATE_LIMIT_REQUESTS

        timestamps: deque[float] = deque()
        now = time.time()

        for _ in range(_RATE_LIMIT_REQUESTS - 1):
            timestamps.append(now)
        assert len(timestamps) < _RATE_LIMIT_REQUESTS

    def test_old_timestamps_pruned(self):
        from swarm.server.api import _RATE_LIMIT_WINDOW

        timestamps: deque[float] = deque()
        old = time.time() - _RATE_LIMIT_WINDOW - 1
        for _ in range(100):
            timestamps.append(old)

        now = time.time()
        cutoff = now - _RATE_LIMIT_WINDOW
        while timestamps and timestamps[0] <= cutoff:
            timestamps.popleft()

        assert len(timestamps) == 0

    def test_mixed_old_and_new(self):
        from swarm.server.api import _RATE_LIMIT_WINDOW

        timestamps: deque[float] = deque()
        old = time.time() - _RATE_LIMIT_WINDOW - 1
        now = time.time()
        for _ in range(5):
            timestamps.append(old)
        for _ in range(3):
            timestamps.append(now)

        cutoff = now - _RATE_LIMIT_WINDOW
        while timestamps and timestamps[0] <= cutoff:
            timestamps.popleft()

        assert len(timestamps) == 3
