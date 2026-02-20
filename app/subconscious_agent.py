"""
Subconscious agent: background memory recall.

Every N user messages the memory manager fires this agent. It reads the
recent conversation context, searches basic-memory for anything relevant,
and — if a match is found — stores a natural-sounding "thought" in the DB
as a ``role='memory'`` message.
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
from app.session_manager import get_session, save_message

log = get_logger(__name__)

NO_MEMORY_SENTINEL = "NO_RELEVANT_MEMORIES"

SUBCONSCIOUS_SYSTEM_PROMPT = (
    "You are a memory recall system. You are given a summary of a recent "
    "conversation. Your ONLY job is to search the knowledge base for prior "
    "knowledge, context, or memories that would be useful to someone "
    "continuing this conversation.\n\n"
    "INSTRUCTIONS:\n"
    "1. Analyze the conversation themes, topics, people, and projects "
    "mentioned.\n"
    "2. Use search_notes to look for relevant prior knowledge. Try "
    "multiple queries if the first yields nothing.\n"
    "3. If you find relevant information, compose a brief first-person "
    "thought that synthesizes what you found. Write it as a natural "
    "internal recollection — as if you are naturally recalling something "
    "related.\n"
    "   Good examples:\n"
    '   - "I recall that we discussed X previously, and the conclusion was Y."\n'
    '   - "Come to think of it, the user mentioned they prefer Z over W."\n'
    '   - "This relates to the project we talked about before — the one where..."\n'
    "   Bad examples (do NOT write like this):\n"
    '   - "I searched the knowledge base and found a note titled..."\n'
    '   - "According to memory entry #42..."\n'
    "4. Keep the thought concise — 2-4 sentences maximum. Focus on the "
    "single most relevant connection.\n"
    f"5. If nothing relevant is found, respond with exactly: {NO_MEMORY_SENTINEL}\n\n"
    "You are running headless. Do not ask for clarification. Make your "
    "best judgment and respond."
)

BASIC_MEMORY_ALLOWED_TOOLS = [
    "mcp__basic_memory__search_notes",
    "mcp__basic_memory__build_context",
    "mcp__basic_memory__recent_activity",
    "mcp__basic_memory__read_note",
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


async def search_memories(session_id: str) -> Optional[str]:
    """Search basic-memory for content relevant to the current session."""
    log.info("subconscious.started", session_id=session_id)

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
        mcp_servers={"basic_memory": get_basic_memory_mcp_config()},
        allowed_tools=BASIC_MEMORY_ALLOWED_TOOLS,
        disallowed_tools=[
            "Task", "Bash", "Glob", "Grep", "Read", "Edit", "Write",
            "WebFetch", "WebSearch", "NotebookEdit",
        ],
        permission_mode="bypassPermissions",
        max_turns=10,
        system_prompt=SUBCONSCIOUS_SYSTEM_PROMPT,
    )

    thought: Optional[str] = None

    log.info("subconscious.agent_starting", session_id=session_id)
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
