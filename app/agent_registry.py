"""
Agent registry: manages per-session conversation state using pydantic-ai.

Each session gets a SessionState that holds message history. The chat agent
itself is a module-level singleton (defined in agents.py) — no subprocesses,
no semaphores, just HTTP calls to the Anthropic API.
"""
import asyncio
import os
import time
from typing import Any, AsyncGenerator

from pydantic_ai import Agent
from pydantic_ai.messages import ModelMessage, TextPart

from app.config import settings
from app.daemon_registry import (
    daemon_registry, DaemonInfo, DaemonCategory, RunStatus,
)
from app.logger import get_logger
from app.session_manager import get_session

log = get_logger(__name__)


def _unwrap_exception_group(exc: BaseException) -> str:
    """Recursively unwrap ExceptionGroup to find the root cause message."""
    if isinstance(exc, BaseExceptionGroup):
        for sub in exc.exceptions:
            return _unwrap_exception_group(sub)
    return f"{type(exc).__name__}: {exc}"

# Register the agent cleanup daemon
daemon_registry.register(DaemonInfo(
    name="agent_cleanup",
    display_name="Agent Cleanup",
    category=DaemonCategory.SYSTEM,
    interval_seconds=60,
    description="Cleans up idle session state to free memory",
))


class SessionState:
    """Tracks conversation state for one chat session."""

    def __init__(self, session_id: str, agent: Agent):
        self.session_id = session_id
        self.agent = agent
        self.message_history: list[ModelMessage] = []
        self._lock = asyncio.Lock()

    async def send_message(self, user_message: str) -> AsyncGenerator[dict[str, Any], None]:
        """Send a message through the agent and yield structured events.

        Yields dicts with type: text, tool_use, tool_result, error
        matching the existing WebSocket protocol.
        """
        try:
            async with self.agent.iter(user_message, message_history=self.message_history) as agent_run:
                async for node in agent_run:
                    # Process events from each node
                    if hasattr(node, 'stream_event'):
                        # ModelRequestNode or similar — we iterate the run
                        pass

            # After the run completes, extract the result
            result = agent_run.result
            if result:
                # Update message history
                self.message_history = result.all_messages()

                # Yield the final text
                output = result.output
                if isinstance(output, str) and output:
                    yield {"type": "text", "content": output}

        except BaseException as e:
            detail = _unwrap_exception_group(e) if isinstance(e, BaseExceptionGroup) else str(e)
            log.exception("session.send_error", session_id=self.session_id, error=detail)
            yield {"type": "error", "content": f"Error communicating with LLM: {detail}"}

    async def send_message_streaming(self, user_message: str) -> AsyncGenerator[dict[str, Any], None]:
        """Send a message and stream back events in real-time.

        Uses agent.run_stream() for streaming text deltas and tool events.
        """
        try:
            async with self.agent.run_stream(
                user_message,
                message_history=self.message_history,
            ) as stream:
                # Stream text deltas
                async for text_delta in stream.stream_text(delta=True):
                    yield {"type": "text", "content": text_delta}

                # After streaming completes, update history
                result = await stream.get_output()
                self.message_history = stream.all_messages()

        except BaseException as e:
            detail = _unwrap_exception_group(e) if isinstance(e, BaseExceptionGroup) else str(e)
            log.exception("session.stream_error", session_id=self.session_id, error=detail)
            yield {"type": "error", "content": f"Error communicating with LLM: {detail}"}

    async def send_message_with_tools(self, user_message: str) -> AsyncGenerator[dict[str, Any], None]:
        """Send a message and yield all events including tool calls/results.

        This is the primary method for chat — it yields:
        - {"type": "text", "content": "..."} for text chunks
        - {"type": "tool_use", "id": "...", "name": "...", "input": {...}} for tool calls
        - {"type": "tool_result", "tool_use_id": "...", "content": "...", "is_error": bool}
        - {"type": "error", "content": "..."} on failure
        """
        try:
            async with self.agent.iter(
                user_message,
                message_history=self.message_history,
            ) as agent_run:
                async for node in agent_run:
                    # Each node in the agent graph can be a model request,
                    # tool call, or end node. We need to extract events.
                    pass

            # After the run, extract all events from the messages
            result = agent_run.result
            if result:
                # Walk through new messages to yield events
                new_messages = result.new_messages()
                for msg in new_messages:
                    for part in msg.parts:
                        if hasattr(part, 'content') and isinstance(part, TextPart):
                            yield {"type": "text", "content": part.content}
                        elif hasattr(part, 'tool_name'):
                            # ToolCallPart
                            yield {
                                "type": "tool_use",
                                "id": getattr(part, 'tool_call_id', ''),
                                "name": part.tool_name,
                                "input": getattr(part, 'args', {}),
                            }
                        elif hasattr(part, 'tool_call_id') and hasattr(part, 'content'):
                            # ToolReturnPart
                            yield {
                                "type": "tool_result",
                                "tool_use_id": part.tool_call_id,
                                "content": part.content if isinstance(part.content, str) else str(part.content),
                                "is_error": False,
                            }

                self.message_history = result.all_messages()

        except BaseException as e:
            detail = _unwrap_exception_group(e) if isinstance(e, BaseExceptionGroup) else str(e)
            log.exception("session.send_error", session_id=self.session_id, error=detail)
            yield {"type": "error", "content": f"Error communicating with LLM: {detail}"}


# Live session states keyed by session_id
_sessions: dict[str, SessionState] = {}

# Last activity timestamp per session (monotonic clock)
_last_activity: dict[str, float] = {}

# How long (seconds) an idle session stays alive before cleanup.
AGENT_TTL_SECONDS = int(os.environ.get("AGENT_TTL_SECONDS", "600"))

# The chat agent singleton — created lazily on first use
_chat_agent: Agent | None = None


def _get_chat_agent() -> Agent:
    """Get or create the chat agent singleton."""
    global _chat_agent
    if _chat_agent is None:
        from app.agents import build_chat_agent
        _chat_agent = build_chat_agent()
    return _chat_agent


async def get_or_create_session(session_id: str) -> SessionState:
    """Return an existing session state or create a new one."""
    if session_id in _sessions:
        _last_activity[session_id] = time.monotonic()
        log.info("registry.reuse_session", session_id=session_id)
        return _sessions[session_id]

    agent = _get_chat_agent()
    session_state = SessionState(session_id, agent)

    # Inject existing conversation history from the DB
    await _inject_history(session_id, session_state)

    _sessions[session_id] = session_state
    _last_activity[session_id] = time.monotonic()

    log.info("registry.created_session", session_id=session_id)
    return session_state


async def _inject_history(session_id: str, session_state: SessionState) -> None:
    """Inject conversation history from DB as a context message."""
    session = await get_session(session_id)
    if not session or not session.messages:
        return

    history_msgs = [m for m in session.messages if m.role in ("user", "assistant")]
    if not history_msgs:
        return

    # Build a condensed replay and send it as a context message
    lines: list[str] = []
    for m in history_msgs:
        tag = "User" if m.role == "user" else "Assistant"
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

    # Run a silent context injection — the agent processes this but we don't
    # yield events to the user
    try:
        result = await session_state.agent.run(
            context,
            message_history=session_state.message_history,
        )
        session_state.message_history = result.all_messages()
    except Exception:
        log.warning("registry.history_injection_failed", session_id=session_id)


def get_lock(session_id: str) -> asyncio.Lock:
    """Return the per-session lock."""
    if session_id in _sessions:
        return _sessions[session_id]._lock
    # Create a temporary lock for sessions not yet created
    return asyncio.Lock()


def touch(session_id: str) -> None:
    """Update the last-activity timestamp for a session."""
    _last_activity[session_id] = time.monotonic()


async def release_session(session_id: str) -> None:
    """Explicitly destroy a session state (e.g. when a session ends)."""
    _sessions.pop(session_id, None)
    _last_activity.pop(session_id, None)
    log.info("registry.released_session", session_id=session_id)


# Set of session IDs with an active WebSocket connection
_active_ws: set[str] = set()


def mark_ws_connected(session_id: str) -> None:
    _active_ws.add(session_id)


def mark_ws_disconnected(session_id: str) -> None:
    _active_ws.discard(session_id)


async def cleanup_idle_sessions() -> None:
    """Background task: periodically clean up sessions that have been idle too long."""
    log.info("registry.cleanup_started", ttl_seconds=AGENT_TTL_SECONDS)
    while True:
        await asyncio.sleep(60)
        run_id = await daemon_registry.start_run("agent_cleanup")
        try:
            now = time.monotonic()
            stale = [
                sid for sid, ts in _last_activity.items()
                if now - ts > AGENT_TTL_SECONDS and sid not in _active_ws
            ]
            for sid in stale:
                log.info("registry.cleanup_idle", session_id=sid)
                await release_session(sid)
            await daemon_registry.end_run(run_id, "agent_cleanup", RunStatus.COMPLETED)
        except Exception as exc:
            await daemon_registry.end_run(run_id, "agent_cleanup", RunStatus.FAILED,
                                           error=str(exc))
            raise
