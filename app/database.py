"""
SQLite database initialization and connection management.
"""
import os
import aiosqlite
from pathlib import Path
from typing import Optional

from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession

DATABASE_PATH = Path(
    os.environ.get("DATABASE_PATH", "/data/sessions.db")
)

# SQLAlchemy async engine and session factory (used by repositories)
engine = create_async_engine(f"sqlite+aiosqlite:///{DATABASE_PATH}", echo=False)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


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

        await db.execute("""
            CREATE TABLE IF NOT EXISTS logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                level TEXT NOT NULL,
                event TEXT NOT NULL,
                module TEXT NOT NULL,
                session_id TEXT,
                data TEXT
            )
        """)

        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_logs_timestamp
            ON logs(timestamp)
        """)

        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_logs_session_id
            ON logs(session_id)
        """)

        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_logs_level
            ON logs(level)
        """)

        # -- Tasks (operational state for the planner) --
        await db.execute("""
            CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                description TEXT,
                status TEXT DEFAULT 'pending',
                priority INTEGER DEFAULT 3,
                category TEXT,
                due_date TEXT,
                due_time TEXT,
                recurrence TEXT,
                source TEXT DEFAULT 'manual',
                source_ref TEXT,
                parent_task_id TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                completed_at TIMESTAMP,
                FOREIGN KEY (parent_task_id) REFERENCES tasks(id)
            )
        """)

        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_tasks_status_due
            ON tasks(status, due_date)
        """)

        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_tasks_priority
            ON tasks(priority)
        """)

        # -- Calendar events --
        await db.execute("""
            CREATE TABLE IF NOT EXISTS events (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                description TEXT,
                start_time TEXT NOT NULL,
                end_time TEXT,
                all_day INTEGER DEFAULT 0,
                location TEXT,
                recurrence TEXT,
                source TEXT DEFAULT 'manual',
                source_ref TEXT,
                reminder_minutes INTEGER DEFAULT 30,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_events_start_time
            ON events(start_time)
        """)

        # -- Interjections (SU-initiated messages queued for delivery) --
        await db.execute("""
            CREATE TABLE IF NOT EXISTS interjections (
                id TEXT PRIMARY KEY,
                content TEXT NOT NULL,
                urgency TEXT DEFAULT 'normal',
                source TEXT,
                related_task_id TEXT,
                related_event_id TEXT,
                status TEXT DEFAULT 'pending',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                delivered_at TIMESTAMP
            )
        """)

        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_interjections_status
            ON interjections(status)
        """)

        # -- SU notes (internal notes-to-self for daemon coordination) --
        await db.execute("""
            CREATE TABLE IF NOT EXISTS su_notes (
                id TEXT PRIMARY KEY,
                content TEXT NOT NULL,
                note_type TEXT DEFAULT 'todo',
                status TEXT DEFAULT 'active',
                priority TEXT DEFAULT 'normal',
                activate_after TEXT,
                related_task_id TEXT,
                related_interjection_id TEXT,
                source TEXT,
                context_json TEXT,
                attempts INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                completed_at TIMESTAMP
            )
        """)

        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_su_notes_status_activate
            ON su_notes(status, activate_after)
        """)

        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_su_notes_source
            ON su_notes(source)
        """)

        # -- Interjection session_id column (links interjection → chat session) --
        # SQLite doesn't support IF NOT EXISTS for ALTER TABLE, so we check first.
        cursor = await db.execute("PRAGMA table_info(interjections)")
        columns = {row[1] for row in await cursor.fetchall()}
        if "session_id" not in columns:
            await db.execute(
                "ALTER TABLE interjections ADD COLUMN session_id TEXT"
            )
        if "related_su_note_id" not in columns:
            await db.execute(
                "ALTER TABLE interjections ADD COLUMN related_su_note_id TEXT"
            )

        # -- Daemon process runs (for the daemon index) --
        await db.execute("""
            CREATE TABLE IF NOT EXISTS daemon_runs (
                id TEXT PRIMARY KEY,
                daemon_name TEXT NOT NULL,
                started_at TEXT NOT NULL,
                ended_at TEXT,
                status TEXT NOT NULL DEFAULT 'running',
                error TEXT,
                metadata TEXT,
                duration_ms INTEGER
            )
        """)

        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_daemon_runs_name_started
            ON daemon_runs(daemon_name, started_at DESC)
        """)

        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_daemon_runs_status
            ON daemon_runs(status)
        """)

        # -- Push subscriptions (Web Push / VAPID) --
        await db.execute("""
            CREATE TABLE IF NOT EXISTS push_subscriptions (
                id TEXT PRIMARY KEY,
                endpoint TEXT NOT NULL UNIQUE,
                subscription_json TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        await db.commit()
