"""
REM agent: post-session memory consolidation.

Named after REM sleep — the phase when the brain consolidates short-term
experiences into long-term memory.  When a chat session ends, this agent
reviews the full conversation and selectively writes noteworthy
information to basic-memory for future recall by the Subconscious agent.
"""
from claude_agent_sdk import (
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
)

from app.logger import get_logger
from app.memory_manager import get_basic_memory_mcp_config
from app.session_manager import get_session

log = get_logger(__name__)

REM_SYSTEM_PROMPT = (
    "You are a memory consolidation system. You will receive a complete "
    "conversation transcript. Your job is to identify noteworthy "
    "information and store it in the knowledge base for future recall.\n\n"
    "BE SELECTIVE. Not everything is worth storing. Focus on:\n"
    "- User preferences, opinions, and personal details they shared\n"
    "- Decisions made or conclusions reached\n"
    "- Technical approaches or solutions discussed\n"
    "- Project names, goals, or context established\n"
    "- Recurring themes or interests\n"
    "- Action items or commitments mentioned\n\n"
    "DO NOT store:\n"
    "- Generic pleasantries or small talk\n"
    "- Information that is easily searchable online\n"
    "- Temporary or ephemeral details (e.g. 'I'm tired today')\n"
    "- Verbatim conversation transcripts\n\n"
    "WORKFLOW:\n"
    "1. First, use search_notes to check if related notes already exist.\n"
    "2. If a related note exists, use edit_note to append new observations "
    "rather than creating a duplicate.\n"
    "3. If no related note exists, use write_note to create a new one.\n\n"
    "ORGANIZATION:\n"
    "- 'people/' — user preferences, personal info, communication style\n"
    "- 'projects/' — project context, goals, technical decisions\n"
    "- 'decisions/' — conclusions reached, choices made\n"
    "- 'knowledge/' — technical knowledge, solutions, patterns\n\n"
    "NOTE FORMAT — use the observation syntax:\n"
    "  - [preference] User prefers dark mode #ui #preferences\n"
    "  - [decision] Chose PostgreSQL over MySQL for the backend #database\n"
    "  - [fact] Project deadline is March 15 #timeline\n"
    "  - [goal] Wants to migrate to microservices by Q3 #architecture\n"
    "  - [context] Working on an e-commerce platform called ShopFlow #project\n\n"
    "Use relations to link related concepts:\n"
    "  - part_of [[ShopFlow Project]]\n"
    "  - requires [[PostgreSQL Setup]]\n\n"
    "If the conversation had nothing noteworthy, simply do nothing — "
    "do not create empty or trivial notes.\n\n"
    "You are running headless. Do not ask for clarification."
)

BASIC_MEMORY_ALLOWED_TOOLS = [
    "mcp__basic_memory__write_note",
    "mcp__basic_memory__edit_note",
    "mcp__basic_memory__search_notes",
    "mcp__basic_memory__read_note",
]


def _build_transcript(messages: list) -> str:
    """Build a plaintext transcript from session messages."""
    lines: list[str] = []
    for msg in messages:
        if msg.role not in ("user", "assistant"):
            continue
        prefix = "User" if msg.role == "user" else "Assistant"
        lines.append(f"{prefix}: {msg.content}")
    return "\n\n".join(lines)


async def consolidate_memories(session_id: str) -> None:
    """Review a completed session and write noteworthy memories."""
    log.info("rem.started", session_id=session_id)

    session = await get_session(session_id)
    if not session or not session.messages:
        log.info("rem.no_messages", session_id=session_id)
        return

    transcript = _build_transcript(session.messages)
    if not transcript.strip():
        log.info("rem.empty_transcript", session_id=session_id)
        return

    message_count = len([m for m in session.messages if m.role in ("user", "assistant")])
    log.info("rem.agent_starting", session_id=session_id, message_count=message_count)

    prompt = (
        "Here is the complete conversation transcript to review:\n\n"
        f"{transcript}\n\n"
        "Analyze this conversation and store any noteworthy information "
        "in the knowledge base. Follow your instructions."
    )

    options = ClaudeAgentOptions(
        mcp_servers={"basic_memory": get_basic_memory_mcp_config()},
        allowed_tools=BASIC_MEMORY_ALLOWED_TOOLS,
        disallowed_tools=[
            "Task", "Bash", "Glob", "Grep", "Read", "Edit", "Write",
            "WebFetch", "WebSearch", "NotebookEdit",
        ],
        permission_mode="bypassPermissions",
        max_turns=15,
        system_prompt=REM_SYSTEM_PROMPT,
    )

    async with ClaudeSDKClient(options=options) as client:
        await client.query(prompt)

        async for message in client.receive_response():
            if isinstance(message, ResultMessage):
                if message.is_error:
                    log.warning(
                        "rem.agent_error",
                        session_id=session_id,
                        result=message.result or "unknown",
                    )
                    return

    log.info("rem.completed", session_id=session_id)
