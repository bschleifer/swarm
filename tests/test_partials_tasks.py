"""Regression tests for /partials/tasks pagination.

Pre-fix the partial defaulted ``limit=100`` and capped at 500. The dashboard
JS never sends a ``limit`` query param, so any filter click silently truncated
the visible task list to 100 — even though the *initial* dashboard render
(``handle_dashboard``) returned every task. Operators with a few hundred
completed tasks would see ``300 tasks: ...`` in the summary line but only
the first 100 in the DOM, with no pagination affordance to load more.

The fix raises both the default and the cap to match ``MAX_QUERY_LIMIT``
(1000) — high enough that real-world swarm boards render in full while
still bounding pathological cases.
"""

from __future__ import annotations

from aiohttp.test_utils import make_mocked_request

# Import the app package first to break the partials <-> app circular import
# (the partials module imports helpers from swarm.web.app, and the app's
# router registration imports the handlers back from this module).
import swarm.web.app  # noqa: F401
from swarm.web.routes.partials import _paginate


class TestPaginateDefaults:
    def test_default_limit_returns_all_300_items(self) -> None:
        """A 300-item list with no ``limit`` param must come back in full.

        This is the regression: pre-fix the default was 100 and the user
        only saw the first third of their tasks after any filter click.
        """
        items = list(range(300))
        request = make_mocked_request("GET", "/partials/tasks")
        total, page, has_more = _paginate(request, items)
        assert total == 300
        assert len(page) == 300, "default pagination must not truncate 300 tasks"
        assert has_more is False

    def test_explicit_limit_still_paginates(self) -> None:
        """Pagination logic still works when a caller does pass ``limit``."""
        items = list(range(300))
        request = make_mocked_request("GET", "/partials/tasks?limit=50&offset=100")
        total, page, has_more = _paginate(request, items)
        assert total == 300
        assert len(page) == 50
        assert page[0] == 100
        assert has_more is True

    def test_cap_prevents_unbounded_query(self) -> None:
        """The cap still rejects pathologically large ``limit`` values."""
        items = list(range(2000))
        request = make_mocked_request("GET", "/partials/tasks?limit=999999")
        _total, page, _has_more = _paginate(request, items)
        # Cap aligns with MAX_QUERY_LIMIT = 1000; anything larger is clamped.
        assert len(page) == 1000
