"""
Telegram session lifecycle management.

Maps Telegram chat_ids to SU sessions with a 1-hour inactivity timeout.
When a session times out, it's ended and REM runs to consolidate memories.
"""
import asyncio
from datetime import datetime
from typing import Optional

from app.logger import get_logger
from app.session_manager import (
    create_session,
    get_session,
    end_session,
    save_message,
    update_session_activity,
)
from app.agent_registry import release_session
from app.memory_manager import on_session_end, on_checkpoint

log = get_logger(__name__)

# In-memory state: chat_id → session_id
_chat_sessions: dict[int, str] = {}
# In-memory timers: session_id → TimerHandle
_timers: dict[str, asyncio.TimerHandle] = {}
# Checkpoint timers: session_id → TimerHandle
_checkpoint_timers: dict[str, asyncio.TimerHandle] = {}

# Inactivity timeout in seconds (1 hour)
INACTIVITY_TIMEOUT = 3600
# Checkpoint timeout in seconds (10 minutes)
CHECKPOINT_TIMEOUT = 600


def _cancel_timer(session_id: str) -> None:
    """Cancel an existing inactivity timer for a session."""
    handle = _timers.pop(session_id, None)
    if handle:
        handle.cancel()


def _cancel_checkpoint_timer(session_id: str) -> None:
    """Cancel an existing checkpoint timer for a session."""
    handle = _checkpoint_timers.pop(session_id, None)
    if handle:
        handle.cancel()


def _schedule_timeout(session_id: str, chat_id: int) -> None:
    """Schedule (or reschedule) the inactivity timeout for a session."""
    _cancel_timer(session_id)
    loop = asyncio.get_running_loop()
    handle = loop.call_later(
        INACTIVITY_TIMEOUT,
        lambda: asyncio.ensure_future(_on_timeout(session_id, chat_id)),
    )
    _timers[session_id] = handle


def _schedule_checkpoint(session_id: str) -> None:
    """Schedule (or reschedule) the checkpoint timer for a session."""
    _cancel_checkpoint_timer(session_id)
    loop = asyncio.get_running_loop()
    handle = loop.call_later(
        CHECKPOINT_TIMEOUT,
        lambda: asyncio.ensure_future(_on_checkpoint(session_id)),
    )
    _checkpoint_timers[session_id] = handle


async def _on_checkpoint(session_id: str) -> None:
    """Handle checkpoint timeout: run mid-session REM on unprocessed messages."""
    _checkpoint_timers.pop(session_id, None)

    # Only checkpoint if this session is still active
    if session_id not in _chat_sessions.values():
        return

    log.info("telegram.checkpoint", session_id=session_id)
    asyncio.ensure_future(on_checkpoint(session_id))

    # Reschedule for the next checkpoint (resets if user sends a message)
    _schedule_checkpoint(session_id)


async def _on_timeout(session_id: str, chat_id: int) -> None:
    """Handle inactivity timeout: end session, run REM."""
    _timers.pop(session_id, None)
    _cancel_checkpoint_timer(session_id)

    # Only expire if this session is still the active one for this chat
    if _chat_sessions.get(chat_id) != session_id:
        return

    _chat_sessions.pop(chat_id, None)
    log.info("telegram.session_timeout", session_id=session_id, chat_id=chat_id)

    await end_session(session_id)
    await release_session(session_id)
    asyncio.ensure_future(on_session_end(session_id))


async def _build_last_session_context(prev_session_id: str) -> Optional[str]:
    """Build a brief context summary from the previous session."""
    session = await get_session(prev_session_id)
    if not session or not session.messages:
        return None

    # Grab the last few user/assistant messages for context
    recent = [
        m for m in session.messages
        if m.role in ("user", "assistant")
    ][-6:]  # last 3 exchanges

    if not recent:
        return None

    lines = []
    for m in recent:
        prefix = "User" if m.role == "user" else "SU"
        # Truncate long messages
        text = m.content[:200] + "..." if len(m.content) > 200 else m.content
        lines.append(f"{prefix}: {text}")

    return (
        "<context>\n"
        "Summary of recent conversation (previous session, now ended):\n"
        + "\n".join(lines) +
        "\n</context>"
    )


async def get_or_create_session(chat_id: int) -> tuple[str, bool]:
    """Get the active session for a chat, or create a new one.

    Returns (session_id, is_new).
    """
    existing_sid = _chat_sessions.get(chat_id)
    if existing_sid:
        # Still active — reset timers and return
        _schedule_timeout(existing_sid, chat_id)
        _schedule_checkpoint(existing_sid)
        return existing_sid, False

    # No active session — create a new one
    session_id = await create_session()
    _chat_sessions[chat_id] = session_id
    _schedule_timeout(session_id, chat_id)
    _schedule_checkpoint(session_id)

    # Inject context from previous session if there was one
    # (find the most recently ended session — we don't track which was the
    # last for this chat, but there's only 2 users so this is fine)
    log.info("telegram.session_created", session_id=session_id, chat_id=chat_id)
    return session_id, True


async def inject_previous_context(session_id: str, chat_id: int) -> None:
    """If there was a previous session, inject its context into the new one."""
    # Find the most recently ended session from the DB
    from app.database import get_db
    async with get_db() as db:
        cursor = await db.execute(
            "SELECT id FROM sessions WHERE status = 'ended' "
            "ORDER BY last_activity DESC LIMIT 1"
        )
        row = await cursor.fetchone()

    if row:
        context = await _build_last_session_context(row["id"])
        if context:
            await save_message(session_id, "memory", context)
            log.info("telegram.previous_context_injected", session_id=session_id)


def touch_session(session_id: str, chat_id: int) -> None:
    """Reset the inactivity and checkpoint timers (call on every message)."""
    if _chat_sessions.get(chat_id) == session_id:
        _schedule_timeout(session_id, chat_id)
        _schedule_checkpoint(session_id)


def get_active_session(chat_id: int) -> Optional[str]:
    """Return the active session_id for a chat, if any."""
    return _chat_sessions.get(chat_id)
