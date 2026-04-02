"""SQLite-backed pipeline store — drop-in replacement for PipelineStore."""

from __future__ import annotations

import json
import threading
from typing import TYPE_CHECKING

from swarm.db.base_store import BaseStore
from swarm.logging import get_logger
from swarm.pipelines.models import Pipeline, pipeline_from_dict

if TYPE_CHECKING:
    from swarm.db.core import SwarmDB

_log = get_logger("db.pipeline_store")


class SqlitePipelineStore(BaseStore):
    """Persist pipelines to the unified swarm.db.

    Stores each pipeline as a JSON blob in the pipelines table's
    config column (the full to_dict() output).
    """

    def __init__(self, db: SwarmDB) -> None:
        self._db = db
        self._lock = threading.Lock()

    def save(self, pipelines: dict[str, Pipeline]) -> None:
        """Write all pipelines to DB."""
        with self._lock:
            for p in pipelines.values():
                data = p.to_dict()
                self._db.execute(
                    "INSERT OR REPLACE INTO pipelines "
                    "(id, name, description, enabled, schedule, "
                    "config, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        p.id,
                        p.name,
                        p.description,
                        1,
                        None,
                        json.dumps(data),
                        p.created_at,
                        p.updated_at,
                    ),
                )
            # Remove pipelines no longer in the dict
            existing = self._db.fetchall("SELECT id FROM pipelines")
            for row in existing:
                if row["id"] not in pipelines:
                    self._db.delete("pipelines", "id = ?", (row["id"],))
            self._db.commit()

    def load(self) -> dict[str, Pipeline]:
        """Load all pipelines from DB."""
        with self._lock:
            rows = self._db.fetchall("SELECT config FROM pipelines")
            pipelines: dict[str, Pipeline] = {}
            for row in rows:
                data = self._parse_json_field(row["config"], None)
                if data is None:
                    continue
                try:
                    p = pipeline_from_dict(data)
                    pipelines[p.id] = p
                except (KeyError, TypeError, ValueError):
                    continue
            if pipelines:
                _log.info("loaded %d pipelines from swarm.db", len(pipelines))
            return pipelines
