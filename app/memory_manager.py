"""
Memory manager: orchestrates the Subconscious and REM background agents.

Tracks per-session user message counts and spawns background asyncio tasks
at the appropriate lifecycle moments. All errors are silently logged so
the main chat flow is never disrupted.
"""
import asyncio
from typing import Optional

from app.daemon_registry import (
    daemon_registry, DaemonInfo, DaemonCategory, RunStatus,
)
from app.logger import get_logger

log = get_logger(__name__)

# Register memory daemons with the process index
daemon_registry.register(DaemonInfo(
    name="subconscious",
    display_name="Subconscious",
    category=DaemonCategory.MEMORY,
    description="Searches knowledge base for relevant context (per-session)",
))
daemon_registry.register(DaemonInfo(
    name="rem",
    display_name="REM Memory",
    category=DaemonCategory.MEMORY,
    description="Post-session memory consolidation into knowledge base",
))

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

async def on_first_message(session_id: str) -> None:
    """Await a subconscious run immediately before the first response.

    Called synchronously (not fire-and-forget) so that any surfaced memory
    is available for injection before the agent starts generating.
    """
    _session_counters[session_id] = 1
    log.info("memory.subconscious_immediate", session_id=session_id)
    await _run_subconscious(session_id)


async def on_user_message(session_id: str) -> None:
    """Called after each user message is saved (message 2+)."""
    count = _session_counters.get(session_id, 0) + 1
    _session_counters[session_id] = count
    log.debug("memory.message_count", session_id=session_id, count=count)

    if count % SUBCONSCIOUS_INTERVAL == 0:
        existing = _pending_tasks.get(session_id)
        if existing and not existing.done():
            existing.cancel()
            log.info("memory.subconscious_cancelled", session_id=session_id)

        log.info("memory.subconscious_triggered", session_id=session_id, message_count=count)
        task = asyncio.create_task(
            _run_subconscious(session_id),
            name=f"subconscious-{session_id[:8]}",
        )
        _pending_tasks[session_id] = task


async def on_session_end(session_id: str) -> None:
    """Called when a session is ended. Spawns REM as a fire-and-forget task."""
    existing = _pending_tasks.pop(session_id, None)
    if existing and not existing.done():
        existing.cancel()
        log.info("memory.subconscious_cancelled_for_end", session_id=session_id)
    _session_counters.pop(session_id, None)

    log.info("memory.rem_triggered", session_id=session_id)
    asyncio.create_task(
        _run_rem(session_id),
        name=f"rem-{session_id[:8]}",
    )


# ---------------------------------------------------------------------------
# Internal runners (isolate agent errors from the main loop)
# ---------------------------------------------------------------------------

async def _run_subconscious(session_id: str) -> None:
    run_id = await daemon_registry.start_run("subconscious", session_id=session_id)
    try:
        from app.subconscious_agent import search_memories
        await search_memories(session_id)
        await daemon_registry.end_run(run_id, "subconscious", RunStatus.COMPLETED)
    except asyncio.CancelledError:
        await daemon_registry.end_run(run_id, "subconscious", RunStatus.FAILED, error="cancelled")
        log.debug("memory.subconscious_task_cancelled", session_id=session_id)
    except Exception:
        await daemon_registry.end_run(run_id, "subconscious", RunStatus.FAILED,
                                       error="see logs")
        log.exception("memory.subconscious_failed", session_id=session_id)
    finally:
        _pending_tasks.pop(session_id, None)


async def _run_rem(session_id: str) -> None:
    run_id = await daemon_registry.start_run("rem", session_id=session_id)
    try:
        from app.rem_agent import consolidate_memories
        await consolidate_memories(session_id)
        await daemon_registry.end_run(run_id, "rem", RunStatus.COMPLETED)
    except Exception:
        await daemon_registry.end_run(run_id, "rem", RunStatus.FAILED, error="see logs")
        log.exception("memory.rem_failed", session_id=session_id)
