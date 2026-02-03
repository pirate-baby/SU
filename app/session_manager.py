"""
Session manager for CRUD operations on sessions and messages.
"""
import uuid
import json
from typing import Optional, List
from datetime import datetime, timedelta
from app.database import get_db
from app.models import Session, Message


async def create_session() -> str:
    """Create a new session and return its ID."""
    session_id = str(uuid.uuid4())

    async with get_db() as db:
        await db.execute(
            "INSERT INTO sessions (id, status) VALUES (?, ?)",
            (session_id, "active")
        )
        await db.commit()

    return session_id


async def get_session(session_id: str) -> Optional[Session]:
    """Get session with all messages."""
    async with get_db() as db:
        cursor = await db.execute(
            "SELECT * FROM sessions WHERE id = ?",
            (session_id,)
        )
        row = await cursor.fetchone()

        if not row:
            return None

        session_data = dict(row)

        cursor = await db.execute(
            "SELECT * FROM messages WHERE session_id = ? ORDER BY created_at ASC",
            (session_id,)
        )
        message_rows = await cursor.fetchall()

        messages = [Message(**dict(row)) for row in message_rows]

        return Session(**session_data, messages=messages)


async def update_session_activity(session_id: str):
    """Update last_activity timestamp for a session."""
    async with get_db() as db:
        await db.execute(
            "UPDATE sessions SET last_activity = CURRENT_TIMESTAMP WHERE id = ?",
            (session_id,)
        )
        await db.commit()


async def save_message(session_id: str, role: str, content: str) -> int:
    """Save a message to the database."""
    async with get_db() as db:
        cursor = await db.execute(
            "INSERT INTO messages (session_id, role, content) VALUES (?, ?, ?)",
            (session_id, role, content)
        )
        await db.commit()
        return cursor.lastrowid


async def save_claude_state(session_id: str, state_data: dict):
    """Save Claude agent state as JSON."""
    async with get_db() as db:
        await db.execute(
            "UPDATE sessions SET claude_state = ? WHERE id = ?",
            (json.dumps(state_data), session_id)
        )
        await db.commit()


async def get_claude_state(session_id: str) -> Optional[dict]:
    """Get Claude agent state from session."""
    async with get_db() as db:
        cursor = await db.execute(
            "SELECT claude_state FROM sessions WHERE id = ?",
            (session_id,)
        )
        row = await cursor.fetchone()

        if row and row["claude_state"]:
            return json.loads(row["claude_state"])
        return None


async def end_session(session_id: str):
    """Mark a session as ended."""
    async with get_db() as db:
        await db.execute(
            "UPDATE sessions SET status = 'ended', last_activity = CURRENT_TIMESTAMP WHERE id = ?",
            (session_id,)
        )
        await db.commit()


async def cleanup_old_sessions(days: int = 7):
    """Delete sessions older than specified days that are ended."""
    async with get_db() as db:
        cutoff_date = datetime.now() - timedelta(days=days)
        await db.execute(
            "DELETE FROM sessions WHERE status = 'ended' AND last_activity < ?",
            (cutoff_date,)
        )
        await db.commit()


async def session_exists(session_id: str) -> bool:
    """Check if a session exists and is active."""
    async with get_db() as db:
        cursor = await db.execute(
            "SELECT id FROM sessions WHERE id = ? AND status = 'active'",
            (session_id,)
        )
        row = await cursor.fetchone()
        return row is not None
