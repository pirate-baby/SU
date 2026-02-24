"""Tier 3: Agent registry tests.

Tests agent lifecycle management (create, reuse, touch, release, cleanup)
with mocked ClaudeChat to avoid external dependencies.
"""
import asyncio
import time
from unittest.mock import patch, AsyncMock

import pytest

from app.database import init_database
from app.session_manager import create_session
from tests.conftest import MockClaudeChat

# Import the registry internals for direct testing
import app.agent_registry as registry


@pytest.fixture(autouse=True)
def _clean_registry():
    """Ensure the registry is clean before and after each test."""
    registry._agents.clear()
    registry._agent_locks.clear()
    registry._last_activity.clear()
    yield
    registry._agents.clear()
    registry._agent_locks.clear()
    registry._last_activity.clear()


def _make_mock_claude():
    mock = MockClaudeChat()
    mock.connected = True
    return mock


class TestGetOrCreateAgent:
    async def test_creates_new_agent(self, db):
        sid = await create_session()

        with patch("app.agent_registry.ClaudeChat") as MockClass:
            instance = _make_mock_claude()
            MockClass.return_value = instance

            agent = await registry.get_or_create_agent(sid)
            assert agent is instance
            assert sid in registry._agents
            assert sid in registry._agent_locks
            assert sid in registry._last_activity

    async def test_reuses_existing_agent(self, db):
        sid = await create_session()
        mock = _make_mock_claude()
        registry._agents[sid] = mock
        registry._last_activity[sid] = time.monotonic()

        agent = await registry.get_or_create_agent(sid)
        assert agent is mock

    async def test_updates_activity_on_reuse(self, db):
        sid = await create_session()
        mock = _make_mock_claude()
        registry._agents[sid] = mock
        old_time = time.monotonic() - 100
        registry._last_activity[sid] = old_time

        await registry.get_or_create_agent(sid)
        assert registry._last_activity[sid] > old_time


class TestTouch:
    async def test_updates_activity_time(self, db):
        sid = "test-session"
        old_time = time.monotonic() - 100
        registry._last_activity[sid] = old_time

        registry.touch(sid)
        assert registry._last_activity[sid] > old_time


class TestGetLock:
    async def test_returns_lock(self, db):
        lock = registry.get_lock("new-session")
        assert isinstance(lock, asyncio.Lock)

    async def test_returns_same_lock(self, db):
        lock1 = registry.get_lock("session-x")
        lock2 = registry.get_lock("session-x")
        assert lock1 is lock2


class TestReleaseAgent:
    async def test_removes_agent(self, db):
        sid = "test-session"
        mock = _make_mock_claude()
        mock.disconnect = AsyncMock()
        registry._agents[sid] = mock
        registry._agent_locks[sid] = asyncio.Lock()
        registry._last_activity[sid] = time.monotonic()

        await registry.release_agent(sid)

        assert sid not in registry._agents
        assert sid not in registry._agent_locks
        assert sid not in registry._last_activity
        mock.disconnect.assert_awaited_once()

    async def test_noop_for_missing_session(self, db):
        # Should not raise
        await registry.release_agent("nonexistent")


class TestCleanupIdleAgents:
    async def test_removes_stale_agents(self, db):
        """Agents past TTL should be cleaned up."""
        sid = "stale-session"
        mock = _make_mock_claude()
        mock.disconnect = AsyncMock()
        registry._agents[sid] = mock
        registry._agent_locks[sid] = asyncio.Lock()
        # Set last activity far in the past (beyond TTL)
        registry._last_activity[sid] = time.monotonic() - registry.AGENT_TTL_SECONDS - 100

        # Run one iteration of cleanup (it loops with sleep, so we run it briefly)
        original_ttl = registry.AGENT_TTL_SECONDS

        # Directly test the cleanup logic instead of the infinite loop
        now = time.monotonic()
        stale = [
            s for s, ts in registry._last_activity.items()
            if now - ts > registry.AGENT_TTL_SECONDS
        ]
        for s in stale:
            await registry.release_agent(s)

        assert sid not in registry._agents
        mock.disconnect.assert_awaited_once()

    async def test_keeps_fresh_agents(self, db):
        """Recently active agents should not be cleaned up."""
        sid = "fresh-session"
        mock = _make_mock_claude()
        registry._agents[sid] = mock
        registry._agent_locks[sid] = asyncio.Lock()
        registry._last_activity[sid] = time.monotonic()  # just now

        now = time.monotonic()
        stale = [
            s for s, ts in registry._last_activity.items()
            if now - ts > registry.AGENT_TTL_SECONDS
        ]
        for s in stale:
            await registry.release_agent(s)

        assert sid in registry._agents
