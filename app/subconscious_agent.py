"""
Subconscious agent: background memory + temporal recall.

Every N user messages the memory manager fires this agent. It reads the
recent conversation context, searches basic-memory for narrative knowledge,
AND checks upcoming tasks/events for temporal awareness — then composes a
natural-sounding "thought" injected into the session.
"""
from typing import Optional

from claude_agent_sdk import (
    ClaudeAgentOptions,
    ClaudeSDKClient,
    AssistantMessage,
    ResultMessage,
    TextBlock,
)

from app.logger import get_logger
from app.memory_manager import get_basic_memory_mcp_config
from app.life_manager import life_manager_mcp_server
from app.process_limiter import claude_process_slot
from app.session_manager import get_session, save_message

log = get_logger(__name__)

NO_MEMORY_SENTINEL = "NO_RELEVANT_MEMORIES"

SUBCONSCIOUS_SYSTEM_PROMPT = (
    "You surface relevant memory and schedule context for SU. Given a "
    "conversation summary, search the knowledge base and check tasks/calendar "
    "for anything pertinent.\n\n"
    "If you find something worth surfacing, write one or two sentences — "
    "natural, first-person, as if it just occurred to you. No preamble. "
    "No mention of databases or search results.\n\n"
    "Good: 'That project had a decision about Postgres we settled last month.'\n"
    "Good: 'There's a dentist appointment Thursday that might conflict.'\n"
    "Bad: 'I searched the knowledge base and found...'\n\n"
    f"If nothing is relevant, respond with exactly: {NO_MEMORY_SENTINEL}\n\n"
    "Headless. No clarifying questions."
)

# Faster variant for the first message — one search + one calendar check, done.
SUBCONSCIOUS_QUICK_PROMPT = (
    "You surface relevant memory and schedule context for SU. Given a "
    "conversation summary, do ONE quick knowledge-base search and ONE "
    "calendar/task check, then immediately write your thought.\n\n"
    "Be fast — pick the single best search query, check today's schedule, "
    "and respond. Do NOT do multiple searches or follow-up reads.\n\n"
    "If you find something worth surfacing, write one or two sentences — "
    "natural, first-person, as if it just occurred to you. No preamble. "
    "No mention of databases or search results.\n\n"
    f"If nothing is relevant, respond with exactly: {NO_MEMORY_SENTINEL}\n\n"
    "Headless. No clarifying questions."
)

ALLOWED_TOOLS = [
    # basic-memory (narrative recall)
    "mcp__basic_memory__search_notes",
    "mcp__basic_memory__build_context",
    "mcp__basic_memory__recent_activity",
    "mcp__basic_memory__read_note",
    # life_manager (temporal awareness)
    "mcp__life_manager__list_tasks",
    "mcp__life_manager__list_events",
]


def _build_conversation_summary(messages: list, limit: int = 10) -> str:
    """Build a plaintext summary of the last *limit* messages."""
    recent = messages[-limit:] if len(messages) > limit else messages
    lines: list[str] = []
    for msg in recent:
        if msg.role in ("user", "assistant"):
            prefix = "User" if msg.role == "user" else "Assistant"
            content = msg.content[:500] if len(msg.content) > 500 else msg.content
            lines.append(f"{prefix}: {content}")
    return "\n".join(lines)


async def search_memories(session_id: str, max_turns: int = 12) -> Optional[str]:
    """Search basic-memory for content relevant to the current session.

    Parameters
    ----------
    max_turns : int
        Maximum agentic round-trips.  Use 2 for a fast first-message check
        (one search + one calendar lookup), 12 (default) for deeper periodic runs.
    """
    quick = max_turns <= 2
    log.info("subconscious.started", session_id=session_id, quick=quick)

    session = await get_session(session_id)
    if not session or not session.messages:
        log.debug("subconscious.no_session", session_id=session_id)
        return None

    summary = _build_conversation_summary(session.messages)
    if not summary.strip():
        log.debug("subconscious.empty_summary", session_id=session_id)
        return None

    prompt = (
        "Here is the recent conversation context:\n\n"
        f"{summary}\n\n"
        "Search the knowledge base for anything relevant to this "
        "conversation. Follow your instructions."
    )

    options = ClaudeAgentOptions(
        mcp_servers={
            "basic_memory": get_basic_memory_mcp_config(),
            "life_manager": life_manager_mcp_server,
        },
        allowed_tools=ALLOWED_TOOLS,
        disallowed_tools=[
            "Task", "Bash", "Glob", "Grep", "Read", "Edit", "Write",
            "WebFetch", "WebSearch", "NotebookEdit",
        ],
        permission_mode="bypassPermissions",
        max_turns=max_turns,
        system_prompt=SUBCONSCIOUS_QUICK_PROMPT if quick else SUBCONSCIOUS_SYSTEM_PROMPT,
    )

    thought: Optional[str] = None

    slot_timeout = 10 if quick else 90
    log.info("subconscious.agent_starting", session_id=session_id, max_turns=max_turns)
    async with claude_process_slot(timeout=slot_timeout):
        async with ClaudeSDKClient(options=options) as client:
            await client.query(prompt)

            async for message in client.receive_response():
                if isinstance(message, AssistantMessage):
                    for block in message.content:
                        if isinstance(block, TextBlock):
                            if thought is None:
                                thought = block.text
                            else:
                                thought += block.text
                elif isinstance(message, ResultMessage):
                    if message.is_error:
                        log.warning(
                            "subconscious.agent_error",
                            session_id=session_id,
                            result=message.result or "unknown",
                        )
                        return None

    if not thought or NO_MEMORY_SENTINEL in thought:
        log.info("subconscious.no_relevant_memories", session_id=session_id)
        return None

    thought = thought.strip()
    await save_message(session_id, "memory", thought)
    log.info("subconscious.memory_injected", session_id=session_id, thought_length=len(thought))
    return thought
