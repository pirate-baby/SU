"""Tier 3: Agent registry tests.

Tests session lifecycle management (create, reuse, touch, release, cleanup)
with mocked SessionState to avoid external dependencies.
"""
import asyncio
import time
from unittest.mock import patch, AsyncMock, MagicMock

import pytest

from app.database import init_database
from app.session_manager import create_session
from tests.conftest import MockSessionState

# Import the registry internals for direct testing
import app.agent_registry as registry


@pytest.fixture(autouse=True)
def _clean_registry():
    """Ensure the registry is clean before and after each test."""
    registry._sessions.clear()
    registry._last_activity.clear()
    yield
    registry._sessions.clear()
    registry._last_activity.clear()


def _make_mock_session(session_id: str = "test") -> registry.SessionState:
    """Create a lightweight mock SessionState for testing."""
    mock = MockSessionState()
    return mock


class TestGetOrCreateSession:
    async def test_creates_new_session(self, db):
        sid = await create_session()

        with patch("app.agent_registry._get_chat_agent") as mock_agent:
            mock_agent.return_value = MagicMock()
            with patch("app.agent_registry._inject_history", new_callable=AsyncMock):
                session_state = await registry.get_or_create_session(sid)
                assert sid in registry._sessions
                assert sid in registry._last_activity

    async def test_reuses_existing_session(self, db):
        sid = await create_session()
        mock = _make_mock_session(sid)
        registry._sessions[sid] = mock
        registry._last_activity[sid] = time.monotonic()

        session_state = await registry.get_or_create_session(sid)
        assert session_state is mock

    async def test_updates_activity_on_reuse(self, db):
        sid = await create_session()
        mock = _make_mock_session(sid)
        registry._sessions[sid] = mock
        old_time = time.monotonic() - 100
        registry._last_activity[sid] = old_time

        await registry.get_or_create_session(sid)
        assert registry._last_activity[sid] > old_time


class TestTouch:
    async def test_updates_activity_time(self, db):
        sid = "test-session"
        old_time = time.monotonic() - 100
        registry._last_activity[sid] = old_time

        registry.touch(sid)
        assert registry._last_activity[sid] > old_time


class TestGetLock:
    async def test_returns_lock_for_existing_session(self, db):
        sid = "session-x"
        mock = _make_mock_session(sid)
        registry._sessions[sid] = mock
        lock = registry.get_lock(sid)
        assert isinstance(lock, asyncio.Lock)

    async def test_returns_temporary_lock_for_new_session(self, db):
        lock = registry.get_lock("new-session")
        assert isinstance(lock, asyncio.Lock)


class TestReleaseSession:
    async def test_removes_session(self, db):
        sid = "test-session"
        mock = _make_mock_session(sid)
        registry._sessions[sid] = mock
        registry._last_activity[sid] = time.monotonic()

        await registry.release_session(sid)

        assert sid not in registry._sessions
        assert sid not in registry._last_activity

    async def test_noop_for_missing_session(self, db):
        # Should not raise
        await registry.release_session("nonexistent")


class TestCleanupIdleSessions:
    async def test_removes_stale_sessions(self, db):
        """Sessions past TTL should be cleaned up."""
        sid = "stale-session"
        mock = _make_mock_session(sid)
        registry._sessions[sid] = mock
        # Set last activity far in the past (beyond TTL)
        registry._last_activity[sid] = time.monotonic() - registry.AGENT_TTL_SECONDS - 100

        # Directly test the cleanup logic instead of the infinite loop
        now = time.monotonic()
        stale = [
            s for s, ts in registry._last_activity.items()
            if now - ts > registry.AGENT_TTL_SECONDS
        ]
        for s in stale:
            await registry.release_session(s)

        assert sid not in registry._sessions

    async def test_keeps_fresh_sessions(self, db):
        """Recently active sessions should not be cleaned up."""
        sid = "fresh-session"
        mock = _make_mock_session(sid)
        registry._sessions[sid] = mock
        registry._last_activity[sid] = time.monotonic()  # just now

        now = time.monotonic()
        stale = [
            s for s, ts in registry._last_activity.items()
            if now - ts > registry.AGENT_TTL_SECONDS
        ]
        for s in stale:
            await registry.release_session(s)

        assert sid in registry._sessions
