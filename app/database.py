"""
SQLite database initialization and connection management.
"""
import os
import aiosqlite
from pathlib import Path
from typing import Optional

DATABASE_PATH = Path(
    os.environ.get("DATABASE_PATH", "/data/sessions.db")
)


def get_db():
    """Get database connection as an async context manager."""
    # aiosqlite.connect returns a context manager that yields a connection
    # The connection's row_factory needs to be set after connection is established
    # We'll use a helper to set row_factory
    class DBConnection:
        async def __aenter__(self):
            self.conn = await aiosqlite.connect(DATABASE_PATH)
            self.conn.row_factory = aiosqlite.Row
            return self.conn

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            await self.conn.close()

    return DBConnection()


async def init_database():
    """Initialize database with required tables and indexes."""
    DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)

    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_activity TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                status TEXT DEFAULT 'active',
                claude_state TEXT
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
            )
        """)

        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_messages_session_id
            ON messages(session_id)
        """)

        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_messages_created_at
            ON messages(created_at)
        """)

        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_sessions_status
            ON sessions(status)
        """)

        await db.commit()
