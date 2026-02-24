"""Tier 1: Database schema initialization tests."""
import aiosqlite
import pytest

from app.database import init_database, get_db


class TestDatabaseInit:
    """Verify init_database creates all required tables and indexes."""

    async def test_creates_sessions_table(self, db):
        async with get_db() as conn:
            cursor = await conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='sessions'"
            )
            assert await cursor.fetchone() is not None

    async def test_creates_messages_table(self, db):
        async with get_db() as conn:
            cursor = await conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='messages'"
            )
            assert await cursor.fetchone() is not None

    async def test_creates_tasks_table(self, db):
        async with get_db() as conn:
            cursor = await conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='tasks'"
            )
            assert await cursor.fetchone() is not None

    async def test_creates_events_table(self, db):
        async with get_db() as conn:
            cursor = await conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='events'"
            )
            assert await cursor.fetchone() is not None

    async def test_creates_interjections_table(self, db):
        async with get_db() as conn:
            cursor = await conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='interjections'"
            )
            assert await cursor.fetchone() is not None

    async def test_creates_logs_table(self, db):
        async with get_db() as conn:
            cursor = await conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='logs'"
            )
            assert await cursor.fetchone() is not None

    async def test_creates_indexes(self, db):
        expected_indexes = [
            "idx_messages_session_id",
            "idx_messages_created_at",
            "idx_sessions_status",
            "idx_logs_timestamp",
            "idx_logs_session_id",
            "idx_logs_level",
            "idx_tasks_status_due",
            "idx_tasks_priority",
            "idx_events_start_time",
            "idx_interjections_status",
        ]
        async with get_db() as conn:
            cursor = await conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            )
            rows = await cursor.fetchall()
            index_names = {row[0] for row in rows}

        for idx in expected_indexes:
            assert idx in index_names, f"Missing index: {idx}"

    async def test_idempotent(self, db):
        """Calling init_database twice should not raise."""
        await init_database()
        async with get_db() as conn:
            cursor = await conn.execute(
                "SELECT count(*) FROM sqlite_master WHERE type='table'"
            )
            row = await cursor.fetchone()
            assert row[0] >= 6  # sessions, messages, logs, tasks, events, interjections

    async def test_sessions_schema_columns(self, db):
        async with get_db() as conn:
            cursor = await conn.execute("PRAGMA table_info(sessions)")
            columns = {row[1] for row in await cursor.fetchall()}
        expected = {"id", "created_at", "last_activity", "status", "claude_state"}
        assert expected.issubset(columns)

    async def test_messages_schema_columns(self, db):
        async with get_db() as conn:
            cursor = await conn.execute("PRAGMA table_info(messages)")
            columns = {row[1] for row in await cursor.fetchall()}
        expected = {"id", "session_id", "role", "content", "created_at"}
        assert expected.issubset(columns)

    async def test_tasks_schema_columns(self, db):
        async with get_db() as conn:
            cursor = await conn.execute("PRAGMA table_info(tasks)")
            columns = {row[1] for row in await cursor.fetchall()}
        expected = {
            "id", "title", "description", "status", "priority", "category",
            "due_date", "due_time", "recurrence", "source", "source_ref",
            "parent_task_id", "created_at", "updated_at", "completed_at",
        }
        assert expected.issubset(columns)
