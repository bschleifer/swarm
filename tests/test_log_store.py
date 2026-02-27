"""Tests for drones/store.py — SQLite persistence layer for the system log."""

import time

from swarm.drones.store import LogStore


class TestLogStoreInit:
    def test_creates_db(self, tmp_path):
        db = tmp_path / "test.db"
        store = LogStore(db_path=db)
        assert db.exists()
        store.close()

    def test_creates_parent_dirs(self, tmp_path):
        db = tmp_path / "sub" / "dir" / "test.db"
        store = LogStore(db_path=db)
        assert db.exists()
        store.close()


class TestLogStoreInsert:
    def test_insert_returns_row_id(self, tmp_path):
        store = LogStore(db_path=tmp_path / "test.db")
        row_id = store.insert(
            timestamp=time.time(),
            action="CONTINUED",
            worker_name="api",
            detail="choice menu",
        )
        assert row_id is not None
        assert row_id > 0
        store.close()

    def test_insert_with_metadata(self, tmp_path):
        store = LogStore(db_path=tmp_path / "test.db")
        row_id = store.insert(
            timestamp=time.time(),
            action="ESCALATED",
            worker_name="web",
            detail="needs approval",
            category="drone",
            is_notification=True,
            metadata={"confidence": 0.9},
        )
        assert row_id is not None
        rows = store.query(limit=1)
        assert len(rows) == 1
        assert rows[0]["metadata"]["confidence"] == 0.9
        assert rows[0]["is_notification"] is True
        store.close()

    def test_insert_sequential_ids(self, tmp_path):
        store = LogStore(db_path=tmp_path / "test.db")
        id1 = store.insert(timestamp=1.0, action="CONTINUED", worker_name="api")
        id2 = store.insert(timestamp=2.0, action="REVIVED", worker_name="web")
        assert id1 is not None and id2 is not None
        assert id2 > id1
        store.close()


class TestLogStoreQuery:
    def _seed(self, store: LogStore) -> None:
        store.insert(timestamp=100.0, action="CONTINUED", worker_name="api", category="drone")
        store.insert(timestamp=200.0, action="ESCALATED", worker_name="web", category="drone")
        store.insert(timestamp=300.0, action="CONTINUED", worker_name="api", category="drone")
        store.insert(
            timestamp=400.0,
            action="TASK_CREATED",
            worker_name="user",
            category="task",
        )

    def test_query_all(self, tmp_path):
        store = LogStore(db_path=tmp_path / "test.db")
        self._seed(store)
        rows = store.query()
        assert len(rows) == 4
        store.close()

    def test_query_by_worker(self, tmp_path):
        store = LogStore(db_path=tmp_path / "test.db")
        self._seed(store)
        rows = store.query(worker_name="api")
        assert len(rows) == 2
        assert all(r["worker_name"] == "api" for r in rows)
        store.close()

    def test_query_by_action(self, tmp_path):
        store = LogStore(db_path=tmp_path / "test.db")
        self._seed(store)
        rows = store.query(action="ESCALATED")
        assert len(rows) == 1
        assert rows[0]["action"] == "ESCALATED"
        store.close()

    def test_query_by_category(self, tmp_path):
        store = LogStore(db_path=tmp_path / "test.db")
        self._seed(store)
        rows = store.query(category="task")
        assert len(rows) == 1
        assert rows[0]["action"] == "TASK_CREATED"
        store.close()

    def test_query_by_time_range(self, tmp_path):
        store = LogStore(db_path=tmp_path / "test.db")
        self._seed(store)
        rows = store.query(since=200.0, until=300.0)
        assert len(rows) == 2
        store.close()

    def test_query_limit(self, tmp_path):
        store = LogStore(db_path=tmp_path / "test.db")
        self._seed(store)
        rows = store.query(limit=2)
        assert len(rows) == 2
        store.close()

    def test_query_offset(self, tmp_path):
        store = LogStore(db_path=tmp_path / "test.db")
        self._seed(store)
        all_rows = store.query()
        offset_rows = store.query(offset=2)
        assert len(offset_rows) == 2
        assert offset_rows[0]["id"] == all_rows[2]["id"]
        store.close()

    def test_query_returns_ordered_by_timestamp_desc(self, tmp_path):
        store = LogStore(db_path=tmp_path / "test.db")
        self._seed(store)
        rows = store.query()
        timestamps = [r["timestamp"] for r in rows]
        assert timestamps == sorted(timestamps, reverse=True)
        store.close()


class TestLogStoreOverrides:
    def test_mark_overridden(self, tmp_path):
        store = LogStore(db_path=tmp_path / "test.db")
        row_id = store.insert(timestamp=1.0, action="CONTINUED", worker_name="api")
        assert row_id is not None
        assert store.mark_overridden(row_id, "user_rejected")

        rows = store.query(overridden=True)
        assert len(rows) == 1
        assert rows[0]["overridden"] is True
        assert rows[0]["override_action"] == "user_rejected"
        store.close()

    def test_mark_recent_overridden(self, tmp_path):
        store = LogStore(db_path=tmp_path / "test.db")
        now = time.time()
        store.insert(timestamp=now - 10, action="CONTINUED", worker_name="api")
        store.insert(timestamp=now - 5, action="ESCALATED", worker_name="api")
        store.insert(timestamp=now - 3, action="CONTINUED", worker_name="web")

        # Should mark the most recent entry for "api"
        row_id = store.mark_recent_overridden("api", "user_approved")
        assert row_id is not None

        rows = store.query(worker_name="api", overridden=True)
        assert len(rows) == 1
        assert rows[0]["action"] == "ESCALATED"
        store.close()

    def test_mark_recent_overridden_with_filter(self, tmp_path):
        store = LogStore(db_path=tmp_path / "test.db")
        now = time.time()
        store.insert(timestamp=now - 10, action="ESCALATED", worker_name="api")
        store.insert(timestamp=now - 5, action="CONTINUED", worker_name="api")

        # Should skip CONTINUED and find ESCALATED
        row_id = store.mark_recent_overridden("api", "user_rejected", action_filter=["ESCALATED"])
        assert row_id is not None

        rows = store.query(worker_name="api", overridden=True)
        assert len(rows) == 1
        assert rows[0]["action"] == "ESCALATED"
        store.close()

    def test_mark_recent_overridden_no_match(self, tmp_path):
        store = LogStore(db_path=tmp_path / "test.db")
        now = time.time()
        store.insert(timestamp=now - 10, action="CONTINUED", worker_name="web")

        row_id = store.mark_recent_overridden("api", "user_rejected")
        assert row_id is None
        store.close()

    def test_query_overridden_false(self, tmp_path):
        store = LogStore(db_path=tmp_path / "test.db")
        store.insert(timestamp=1.0, action="CONTINUED", worker_name="api")
        row_id = store.insert(timestamp=2.0, action="ESCALATED", worker_name="api")
        assert row_id is not None
        store.mark_overridden(row_id, "user_rejected")

        not_overridden = store.query(overridden=False)
        assert len(not_overridden) == 1
        assert not_overridden[0]["action"] == "CONTINUED"
        store.close()


class TestLogStoreCount:
    def test_count_all(self, tmp_path):
        store = LogStore(db_path=tmp_path / "test.db")
        store.insert(timestamp=1.0, action="CONTINUED", worker_name="api")
        store.insert(timestamp=2.0, action="ESCALATED", worker_name="web")
        assert store.count() == 2
        store.close()

    def test_count_filtered(self, tmp_path):
        store = LogStore(db_path=tmp_path / "test.db")
        store.insert(timestamp=1.0, action="CONTINUED", worker_name="api")
        store.insert(timestamp=2.0, action="ESCALATED", worker_name="web")
        store.insert(timestamp=3.0, action="CONTINUED", worker_name="api")
        assert store.count(worker_name="api") == 2
        assert store.count(action="ESCALATED") == 1
        store.close()


class TestLogStorePrune:
    def test_prune_old_entries(self, tmp_path):
        store = LogStore(db_path=tmp_path / "test.db")
        old_time = time.time() - (31 * 86400)  # 31 days ago
        store.insert(timestamp=old_time, action="CONTINUED", worker_name="api")
        store.insert(timestamp=time.time(), action="ESCALATED", worker_name="web")

        deleted = store.prune(max_age_days=30)
        assert deleted == 1
        assert store.count() == 1
        store.close()

    def test_prune_nothing(self, tmp_path):
        store = LogStore(db_path=tmp_path / "test.db")
        store.insert(timestamp=time.time(), action="CONTINUED", worker_name="api")
        deleted = store.prune()
        assert deleted == 0
        assert store.count() == 1
        store.close()


class TestLogStoreClose:
    def test_operations_after_close(self, tmp_path):
        store = LogStore(db_path=tmp_path / "test.db")
        store.insert(timestamp=1.0, action="CONTINUED", worker_name="api")
        store.close()
        # Operations after close should return safe defaults
        assert store.insert(timestamp=2.0, action="CONTINUED", worker_name="api") is None
        assert store.query() == []
        assert store.count() == 0
