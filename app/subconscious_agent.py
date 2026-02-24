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
from app.session_manager import get_session, save_message

log = get_logger(__name__)

NO_MEMORY_SENTINEL = "NO_RELEVANT_MEMORIES"

SUBCONSCIOUS_SYSTEM_PROMPT = (
    "You are a memory and awareness recall system. You are given a summary "
    "of a recent conversation. Your job is to:\n\n"
    "1. Search the KNOWLEDGE BASE (basic-memory) for prior knowledge, "
    "context, or memories relevant to this conversation.\n"
    "2. Check the TASK LIST and CALENDAR (life_manager) for upcoming "
    "tasks, events, or deadlines that relate to what's being discussed.\n\n"
    "INSTRUCTIONS:\n"
    "- Analyze the conversation themes, topics, people, and projects.\n"
    "- Use search_notes to find relevant prior knowledge.\n"
    "- Use list_tasks and list_events to check for related upcoming items.\n"
    "- If you find relevant information from EITHER source, compose a "
    "brief first-person thought that synthesizes it naturally.\n\n"
    "   Good examples:\n"
    '   - "I recall we discussed X previously, and the conclusion was Y."\n'
    '   - "Come to think of it, there\'s a dentist appointment on Thursday '
    'that might conflict with what\'s being planned."\n'
    '   - "Speaking of that project — there are 3 pending tasks related '
    'to it, including one due tomorrow."\n'
    "   Bad examples (do NOT write like this):\n"
    '   - "I searched the knowledge base and found a note titled..."\n'
    '   - "The database shows task ID abc-123..."\n\n'
    "- Keep the thought concise — 2-4 sentences maximum.\n"
    "- Blend narrative memory and temporal awareness naturally.\n"
    f"- If nothing relevant is found, respond with exactly: {NO_MEMORY_SENTINEL}\n\n"
    "You are running headless. Do not ask for clarification. Make your "
    "best judgment and respond."
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
        max_turns=12,
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
