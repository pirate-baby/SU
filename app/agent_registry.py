"""
Agent registry: keeps ClaudeChat instances alive independent of WebSocket
connections so that sessions survive disconnects and reconnects.
"""
import asyncio
import os
import time
from typing import Optional

from fastapi import WebSocket

from app.claude_client import ClaudeChat
from app.config import settings
from app.logger import get_logger
from app.process_limiter import _get_semaphore, claude_process_slot
from app.session_manager import get_session

log = get_logger(__name__)

# Live agents keyed by session_id
_agents: dict[str, ClaudeChat] = {}

# One lock per session to serialize message handling
_agent_locks: dict[str, asyncio.Lock] = {}

# Last activity timestamp per agent (monotonic clock)
_last_activity: dict[str, float] = {}

# How long (seconds) an idle agent stays alive before cleanup.
# Keep low on constrained instances — each agent holds a process slot.
AGENT_TTL_SECONDS = int(os.environ.get("AGENT_TTL_SECONDS", "600"))  # 10 minutes


async def get_or_create_agent(session_id: str) -> ClaudeChat:
    """Return an existing live agent or create and connect a new one."""
    if session_id in _agents:
        _last_activity[session_id] = time.monotonic()
        log.info("registry.reuse_agent", session_id=session_id)
        return _agents[session_id]

    # Acquire a process slot *before* spawning a new Claude subprocess.
    sem = _get_semaphore()
    await sem.acquire()
    try:
        oauth = settings.claude_code_oauth_token or None
        claude = ClaudeChat(oauth_token=oauth)
        await claude.connect()
    except BaseException:
        sem.release()
        raise

    # Slot stays held until release_agent() is called.
    _agents[session_id] = claude
    _agent_locks[session_id] = asyncio.Lock()
    _last_activity[session_id] = time.monotonic()

    # If there is existing conversation history, inject it so the agent has context
    await _inject_history(session_id, claude)

    log.info("registry.created_agent", session_id=session_id)
    return claude


async def _inject_history(session_id: str, claude: ClaudeChat) -> None:
    """Inject a conversation summary from the DB so a fresh agent has context."""
    session = await get_session(session_id)
    if not session or not session.messages:
        return

    history_msgs = [m for m in session.messages if m.role in ("user", "assistant")]
    if not history_msgs:
        return

    # Build a condensed replay of the conversation
    lines: list[str] = []
    for m in history_msgs:
        tag = "User" if m.role == "user" else "Assistant"
        # Truncate very long messages to keep the context reasonable
        content = m.content if len(m.content) <= 2000 else m.content[:2000] + "..."
        lines.append(f"[{tag}]: {content}")

    context = (
        "<context>\n"
        "The following is the conversation history from this session. "
        "You are resuming an ongoing conversation. Continue naturally.\n\n"
        + "\n\n".join(lines)
        + "\n</context>"
    )

    log.info("registry.injecting_history", session_id=session_id, message_count=len(history_msgs))
    # Send through the SDK so it enters the agent's internal history
    async for _ in claude.send_message(context):
        pass


def get_lock(session_id: str) -> asyncio.Lock:
    """Return the per-session lock (creating one if needed)."""
    if session_id not in _agent_locks:
        _agent_locks[session_id] = asyncio.Lock()
    return _agent_locks[session_id]


def touch(session_id: str) -> None:
    """Update the last-activity timestamp for an agent."""
    _last_activity[session_id] = time.monotonic()


async def release_agent(session_id: str) -> None:
    """Explicitly destroy an agent (e.g. when a session ends)."""
    claude = _agents.pop(session_id, None)
    _agent_locks.pop(session_id, None)
    _last_activity.pop(session_id, None)
    if claude:
        try:
            # Shield the disconnect so any internal CancelledError from
            # asyncio.wait_for does not propagate to the calling task.
            await asyncio.shield(asyncio.wait_for(claude.disconnect(), timeout=10))
        except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
            log.warning("registry.disconnect_failed", session_id=session_id)
        finally:
            _get_semaphore().release()
            log.info("registry.released_agent", session_id=session_id)


# Set of session IDs with an active WebSocket connection (maintained by main.py).
_active_ws: set[str] = set()


def mark_ws_connected(session_id: str) -> None:
    _active_ws.add(session_id)


def mark_ws_disconnected(session_id: str) -> None:
    _active_ws.discard(session_id)


async def cleanup_idle_agents() -> None:
    """Background task: periodically disconnect agents that have been idle too long."""
    log.info("registry.cleanup_started", ttl_seconds=AGENT_TTL_SECONDS)
    while True:
        await asyncio.sleep(60)
        now = time.monotonic()
        stale = [
            sid for sid, ts in _last_activity.items()
            if now - ts > AGENT_TTL_SECONDS and sid not in _active_ws
        ]
        for sid in stale:
            log.info("registry.cleanup_idle", session_id=sid)
            await release_agent(sid)
