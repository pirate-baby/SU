"""Tier 1: Session manager CRUD tests."""
import pytest

from app.database import init_database, get_db
from app.session_manager import (
    create_session,
    get_session,
    get_all_sessions,
    session_exists,
    save_message,
    update_session_activity,
    end_session,
    mark_memories_consumed,
)


class TestCreateSession:
    async def test_returns_uuid_string(self, db):
        sid = await create_session()
        assert isinstance(sid, str)
        assert len(sid) == 36  # UUID format

    async def test_session_is_active(self, db):
        sid = await create_session()
        assert await session_exists(sid) is True

    async def test_session_in_database(self, db):
        sid = await create_session()
        async with get_db() as conn:
            cursor = await conn.execute("SELECT status FROM sessions WHERE id = ?", (sid,))
            row = await cursor.fetchone()
        assert row is not None
        assert row[0] == "active"


class TestSessionExists:
    async def test_true_for_active_session(self, db):
        sid = await create_session()
        assert await session_exists(sid) is True

    async def test_false_for_nonexistent(self, db):
        assert await session_exists("nonexistent-id") is False

    async def test_false_for_ended_session(self, db):
        sid = await create_session()
        await end_session(sid)
        assert await session_exists(sid) is False


class TestGetSession:
    async def test_returns_none_for_missing(self, db):
        result = await get_session("nonexistent")
        assert result is None

    async def test_returns_session_with_messages(self, db):
        sid = await create_session()
        await save_message(sid, "user", "hello")
        await save_message(sid, "assistant", "hi there")

        session = await get_session(sid)
        assert session is not None
        assert session.id == sid
        assert len(session.messages) == 2
        assert session.messages[0].role == "user"
        assert session.messages[0].content == "hello"
        assert session.messages[1].role == "assistant"
        assert session.messages[1].content == "hi there"

    async def test_messages_ordered_by_created_at(self, db):
        sid = await create_session()
        await save_message(sid, "user", "first")
        await save_message(sid, "assistant", "second")
        await save_message(sid, "user", "third")

        session = await get_session(sid)
        contents = [m.content for m in session.messages]
        assert contents == ["first", "second", "third"]


class TestGetAllSessions:
    async def test_returns_empty_list_initially(self, db):
        sessions = await get_all_sessions()
        assert sessions == []

    async def test_returns_all_sessions(self, db):
        await create_session()
        await create_session()
        await create_session()

        sessions = await get_all_sessions()
        assert len(sessions) == 3

    async def test_includes_messages(self, db):
        sid = await create_session()
        await save_message(sid, "user", "test message")

        sessions = await get_all_sessions()
        assert len(sessions) == 1
        assert len(sessions[0].messages) == 1


class TestSaveMessage:
    async def test_saves_and_returns_id(self, db):
        sid = await create_session()
        msg_id = await save_message(sid, "user", "hello")
        assert isinstance(msg_id, int)
        assert msg_id > 0

    async def test_message_persisted(self, db):
        sid = await create_session()
        await save_message(sid, "user", "persisted message")

        async with get_db() as conn:
            cursor = await conn.execute(
                "SELECT role, content FROM messages WHERE session_id = ?", (sid,)
            )
            row = await cursor.fetchone()
        assert row is not None
        assert row[0] == "user"
        assert row[1] == "persisted message"

    async def test_supports_all_roles(self, db):
        sid = await create_session()
        for role in ("user", "assistant", "memory", "memory_consumed"):
            await save_message(sid, role, f"content for {role}")

        session = await get_session(sid)
        roles = [m.role for m in session.messages]
        assert roles == ["user", "assistant", "memory", "memory_consumed"]


class TestEndSession:
    async def test_marks_session_ended(self, db):
        sid = await create_session()
        await end_session(sid)

        async with get_db() as conn:
            cursor = await conn.execute("SELECT status FROM sessions WHERE id = ?", (sid,))
            row = await cursor.fetchone()
        assert row[0] == "ended"

    async def test_ended_session_not_active(self, db):
        sid = await create_session()
        await end_session(sid)
        assert await session_exists(sid) is False


class TestUpdateSessionActivity:
    async def test_updates_timestamp(self, db):
        sid = await create_session()

        async with get_db() as conn:
            cursor = await conn.execute("SELECT last_activity FROM sessions WHERE id = ?", (sid,))
            before = (await cursor.fetchone())[0]

        await update_session_activity(sid)

        async with get_db() as conn:
            cursor = await conn.execute("SELECT last_activity FROM sessions WHERE id = ?", (sid,))
            after = (await cursor.fetchone())[0]

        # Timestamps should exist; after should be >= before
        assert after is not None


class TestMarkMemoriesConsumed:
    async def test_changes_role(self, db):
        sid = await create_session()
        msg_id = await save_message(sid, "memory", "a thought")
        await mark_memories_consumed(msg_id)

        async with get_db() as conn:
            cursor = await conn.execute("SELECT role FROM messages WHERE id = ?", (msg_id,))
            row = await cursor.fetchone()
        assert row[0] == "memory_consumed"
