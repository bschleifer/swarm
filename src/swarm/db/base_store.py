"""BaseStore mixin — shared helpers for SQLite-backed store classes."""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING, Any, TypeVar

if TYPE_CHECKING:
    from swarm.db.core import SwarmDB

T = TypeVar("T")


class BaseStore:
    """Mixin providing common store utilities.

    Subclasses must set ``self._db`` to a :class:`SwarmDB` instance.
    """

    _db: SwarmDB

    # ------------------------------------------------------------------
    # JSON helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_json_field(val: str | None, default: T) -> Any:
        """Safely parse a JSON string, returning *default* on failure.

        Handles ``None``, empty strings, decode errors, and type errors.
        The return type matches *default* at runtime but is typed as
        ``Any`` because ``json.loads`` erases the original type.
        """
        if not val:
            return default
        try:
            result = json.loads(val)
        except (json.JSONDecodeError, TypeError):
            return default
        # If the caller expects a list but got something else, fall back
        if isinstance(default, list) and not isinstance(result, list):
            return default
        if isinstance(default, dict) and not isinstance(result, dict):
            return default
        return result

    # ------------------------------------------------------------------
    # Pruning helpers
    # ------------------------------------------------------------------

    def _prune_older_than(
        self,
        table: str,
        timestamp_col: str,
        max_age_days: int,
        *,
        extra_where: str = "",
        extra_params: tuple[Any, ...] = (),
    ) -> int:
        """Delete rows older than *max_age_days* from *table*.

        Parameters
        ----------
        table:
            Target table name.
        timestamp_col:
            Column storing the Unix timestamp to compare against.
        max_age_days:
            Records older than this many days are deleted.
        extra_where:
            Optional additional SQL condition (AND-ed with the age check).
        extra_params:
            Bind parameters for *extra_where*.

        Returns the number of rows deleted.
        """
        cutoff = time.time() - (max_age_days * 86400)
        where = f"{timestamp_col} < ?"
        params: tuple[Any, ...] = (cutoff,)
        if extra_where:
            where = f"{extra_where} AND {where}"
            params = extra_params + params
        return self._db.delete(table, where, params)
