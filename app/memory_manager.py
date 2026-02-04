"""
Memory manager: orchestrates the Subconscious and REM background agents.

Tracks per-session user message counts and spawns background asyncio tasks
at the appropriate lifecycle moments. All errors are silently logged so
the main chat flow is never disrupted.
"""
import asyncio
import logging
from typing import Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# In-memory state (resets on process restart — acceptable)
# ---------------------------------------------------------------------------
_session_counters: dict[str, int] = {}
_pending_tasks: dict[str, asyncio.Task] = {}

SUBCONSCIOUS_INTERVAL = 5  # trigger every N user messages


def get_basic_memory_mcp_config() -> dict:
    """Return MCP stdio server config for basic-memory."""
    return {
        "type": "stdio",
        "command": "uvx",
        "args": ["basic-memory", "mcp"],
    }


# ---------------------------------------------------------------------------
# Public hooks called from main.py
# ---------------------------------------------------------------------------

async def on_user_message(session_id: str) -> None:
    """Called after each user message is saved.

    Increments the per-session counter and fires the Subconscious agent
    every SUBCONSCIOUS_INTERVAL messages.
    """
    _session_counters[session_id] = _session_counters.get(session_id, 0) + 1
    count = _session_counters[session_id]

    if count % SUBCONSCIOUS_INTERVAL == 0:
        # Cancel any still-running subconscious task for this session
        existing = _pending_tasks.get(session_id)
        if existing and not existing.done():
            existing.cancel()

        task = asyncio.create_task(
            _run_subconscious(session_id),
            name=f"subconscious-{session_id[:8]}",
        )
        _pending_tasks[session_id] = task


async def on_session_end(session_id: str) -> None:
    """Called when a session is ended. Spawns REM as a fire-and-forget task."""
    # Cancel any pending subconscious work — session is over
    existing = _pending_tasks.pop(session_id, None)
    if existing and not existing.done():
        existing.cancel()
    _session_counters.pop(session_id, None)

    asyncio.create_task(
        _run_rem(session_id),
        name=f"rem-{session_id[:8]}",
    )


# ---------------------------------------------------------------------------
# Internal runners (isolate agent errors from the main loop)
# ---------------------------------------------------------------------------

async def _run_subconscious(session_id: str) -> None:
    try:
        from app.subconscious_agent import search_memories
        await search_memories(session_id)
    except asyncio.CancelledError:
        logger.debug("Subconscious task cancelled for session %s", session_id[:8])
    except Exception:
        logger.exception("Subconscious agent failed for session %s", session_id[:8])
    finally:
        _pending_tasks.pop(session_id, None)


async def _run_rem(session_id: str) -> None:
    try:
        from app.rem_agent import consolidate_memories
        await consolidate_memories(session_id)
    except Exception:
        logger.exception("REM agent failed for session %s", session_id[:8])
