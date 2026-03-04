"""Tests for the SQLite inbox database module."""

import tempfile
import time
from pathlib import Path

from lunch_time_mcp.db import (
    init_db,
    insert_message,
    get_unprocessed,
    mark_processed,
    get_stats,
)


class TestInitDb:
    def test_creates_database_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = init_db(str(Path(tmpdir) / "test.db"))
            assert path.exists()

    def test_creates_parent_directories(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = init_db(str(Path(tmpdir) / "nested" / "dir" / "test.db"))
            assert path.exists()

    def test_idempotent(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = str(Path(tmpdir) / "test.db")
            init_db(db_path)
            init_db(db_path)  # Should not raise


class TestInsertAndRetrieve:
    def test_insert_and_get_unprocessed(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = init_db(str(Path(tmpdir) / "test.db"))

            row_id = insert_message(
                db_path=path,
                timestamp=1000.0,
                sender_uuid="test-uuid-1234",
                message="hello world",
                group_id="group-abc",
            )
            assert row_id > 0

            msgs = get_unprocessed(path)
            assert len(msgs) == 1
            assert msgs[0].sender_uuid == "test-uuid-1234"
            assert msgs[0].message == "hello world"
            assert msgs[0].group_id == "group-abc"
            assert msgs[0].processed is False

    def test_insert_without_group(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = init_db(str(Path(tmpdir) / "test.db"))

            insert_message(
                db_path=path,
                timestamp=1000.0,
                sender_uuid="test-uuid",
                message="direct message",
            )

            msgs = get_unprocessed(path)
            assert len(msgs) == 1
            assert msgs[0].group_id is None

    def test_multiple_messages_ordered(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = init_db(str(Path(tmpdir) / "test.db"))

            insert_message(path, 1000.0, "uuid-a", "first")
            insert_message(path, 2000.0, "uuid-b", "second")
            insert_message(path, 3000.0, "uuid-a", "third")

            msgs = get_unprocessed(path)
            assert len(msgs) == 3
            assert msgs[0].message == "first"
            assert msgs[1].message == "second"
            assert msgs[2].message == "third"

    def test_limit(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = init_db(str(Path(tmpdir) / "test.db"))

            for i in range(10):
                insert_message(path, float(i), "uuid", f"msg-{i}")

            msgs = get_unprocessed(path, limit=3)
            assert len(msgs) == 3


class TestMarkProcessed:
    def test_mark_hides_messages(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = init_db(str(Path(tmpdir) / "test.db"))

            id1 = insert_message(path, 1000.0, "uuid", "msg1")
            id2 = insert_message(path, 2000.0, "uuid", "msg2")
            insert_message(path, 3000.0, "uuid", "msg3")

            mark_processed(path, [id1, id2])

            msgs = get_unprocessed(path)
            assert len(msgs) == 1
            assert msgs[0].message == "msg3"

    def test_mark_empty_list(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = init_db(str(Path(tmpdir) / "test.db"))
            result = mark_processed(path, [])
            assert result == 0


class TestStats:
    def test_stats(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = init_db(str(Path(tmpdir) / "test.db"))

            insert_message(path, 1000.0, "uuid", "msg1")
            id2 = insert_message(path, 2000.0, "uuid", "msg2")
            insert_message(path, 3000.0, "uuid", "msg3")

            mark_processed(path, [id2])

            stats = get_stats(path)
            assert stats["total"] == 3
            assert stats["unprocessed"] == 2
            assert stats["processed"] == 1
