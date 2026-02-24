"""
Shared test fixtures for SU regression tests.

Provides:
- Temporary SQLite database per test (isolated)
- Initialized schema via init_database()
- FastAPI async test client wired to the test DB
- Mock ClaudeChat that yields predictable events
"""
import asyncio
import os
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession


# ---------------------------------------------------------------------------
# Patch DATABASE_PATH *before* importing any app modules so every module
# that reads `database.DATABASE_PATH` at import time picks up the override.
# ---------------------------------------------------------------------------
_tmp_dir = tempfile.mkdtemp()
_test_db_path = Path(_tmp_dir) / "test.db"
os.environ["DATABASE_PATH"] = str(_test_db_path)

# Now safe to import app modules
import app.database as db_mod
import app.repositories as repo_mod
from app.database import init_database, get_db


@pytest_asyncio.fixture(autouse=True)
async def _isolated_db(tmp_path):
    """Give each test its own database file and initialize the schema."""
    test_db = tmp_path / "test.db"

    # Patch aiosqlite DATABASE_PATH
    original_path = db_mod.DATABASE_PATH
    db_mod.DATABASE_PATH = test_db

    # Patch SQLAlchemy engine + session factory
    test_engine = create_async_engine(f"sqlite+aiosqlite:///{test_db}", echo=False)
    test_session_factory = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)

    original_engine = db_mod.engine
    original_session = db_mod.async_session
    db_mod.engine = test_engine
    db_mod.async_session = test_session_factory

    # Patch the repo module's copy of async_session
    original_repo_session = repo_mod.async_session
    repo_mod.async_session = test_session_factory

    # Initialize schema (creates all tables via aiosqlite)
    await init_database()

    yield

    # Dispose the test engine to release connections
    await test_engine.dispose()

    db_mod.DATABASE_PATH = original_path
    db_mod.engine = original_engine
    db_mod.async_session = original_session
    repo_mod.async_session = original_repo_session


# Keep the `db` name as an alias for tests that explicitly request it
@pytest_asyncio.fixture
async def db():
    """No-op fixture — schema is already initialized by _isolated_db."""
    yield


# ---------------------------------------------------------------------------
# FastAPI test client (async)
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def client():
    """Async HTTP client talking to the FastAPI app with mocked lifespan."""
    from app.main import app
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def test_lifespan(app):
        # Schema already created by _isolated_db; just yield.
        yield

    original_router_lifespan = app.router.lifespan_context
    app.router.lifespan_context = test_lifespan

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac

    app.router.lifespan_context = original_router_lifespan


# ---------------------------------------------------------------------------
# Mock ClaudeChat
# ---------------------------------------------------------------------------

class MockClaudeChat:
    """A fake ClaudeChat that yields configurable events."""

    def __init__(self, responses=None):
        self._responses = responses or [
            {"type": "text", "content": "Hello from mock Claude."},
        ]
        self.connected = False
        self.messages_sent = []

    async def connect(self):
        self.connected = True

    async def disconnect(self):
        self.connected = False

    async def send_message(self, message: str):
        self.messages_sent.append(message)
        for event in self._responses:
            yield event

    async def __aenter__(self):
        await self.connect()
        return self

    async def __aexit__(self, *args):
        await self.disconnect()


@pytest.fixture
def mock_claude():
    """Return a MockClaudeChat instance."""
    return MockClaudeChat()


@pytest.fixture
def mock_claude_factory():
    """Return a factory for creating MockClaudeChat with custom responses."""
    def factory(responses=None):
        return MockClaudeChat(responses=responses)
    return factory
