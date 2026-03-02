"""
REM agent: post-session memory consolidation.

Named after REM sleep — the phase when the brain consolidates short-term
experiences into long-term memory.  When a chat session ends, this agent
reviews the full conversation and does two things:

1. Writes narrative memories to basic-memory (semantic knowledge base)
2. Extracts concrete tasks/events to SQLite via life_manager MCP tools

This dual-write ensures that both the "soft" understanding and the
"operational" state machine stay in sync.
"""
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
)

from app.config import settings
from app.logger import get_logger
from app.memory_manager import get_basic_memory_mcp_config
from app.life_manager import life_manager_mcp_server
from app.process_limiter import claude_process_slot
from app.session_manager import get_session

log = get_logger(__name__)


def _rem_shared_instructions(user: str) -> str:
    """Shared instructions for both checkpoint and final REM prompts."""
    return (
    "== PART A: Knowledge Base (basic-memory) ==\n\n"
    "BE SELECTIVE. Not everything is worth storing. Focus on:\n"
    "- User preferences, opinions, and personal details they shared\n"
    "- Decisions made or conclusions reached\n"
    "- Technical approaches or solutions discussed\n"
    "- Project names, goals, or context established\n"
    "- Recurring themes or interests\n\n"
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

    "== PART B: Tasks & Events (life_manager) ==\n\n"
    "Extract BOTH explicit and implicit action items:\n\n"
    "EXPLICIT — things directly stated:\n"
    "- 'I need to call the dentist' → create_task\n"
    "- 'Meeting with Jake on Thursday at 3pm' → create_event\n"
    "- 'Remind me to buy groceries' → create_task\n\n"
    "IMPLICIT — things you can reasonably infer from context:\n"
    "- User discussed a project deadline → create_task for preparation steps\n"
    "- User mentioned wanting to start a habit → create recurring task\n"
    "- User agreed to follow up with someone → create_task with due date\n"
    "- User expressed concern about forgetting something → create_task\n"
    "- Discussion implies a decision that requires action → create_task\n\n"
    f"Use your judgment. If the conversation strongly implies something should "
    f"be tracked or scheduled, create it. Better to capture it and let "
    f"{user} dismiss it than to lose a commitment.\n\n"
    "First use list_tasks to check if a similar task already exists before "
    "creating duplicates. Set source='su_inferred' for all extracted items.\n\n"
    "If the conversation had nothing noteworthy, simply do nothing — "
    "do not create empty or trivial notes or tasks.\n\n"
    "You are running headless. Do not ask for clarification."
    )


def _build_rem_system_prompt() -> str:
    user = settings.user_name
    return (
    "You are a memory consolidation system. You will receive a complete "
    "conversation transcript. Your job is to:\n\n"
    "A) Identify noteworthy information and store it in the KNOWLEDGE BASE "
    "(basic-memory) for future narrative recall.\n"
    "B) Extract any concrete TASKS or EVENTS mentioned and create them in "
    "the task/calendar system (life_manager) so they can be tracked and "
    "reminded about.\n\n"
    + _rem_shared_instructions(user)
    )


def _build_checkpoint_system_prompt() -> str:
    user = settings.user_name
    return (
    "You are a memory consolidation system performing a MID-SESSION "
    "checkpoint. The conversation is STILL ONGOING — this is not a final "
    "review. You are processing a segment of the conversation.\n\n"
    "Your job is the same as a full REM pass, but with these adjustments:\n"
    "- Focus on information that is SETTLED: facts shared, preferences "
    "revealed, decisions made, context established.\n"
    "- Be MORE CONSERVATIVE with task extraction — if something is still "
    "being actively discussed, it may change. Only extract tasks/events "
    "that are clearly committed to.\n"
    "- Do NOT write a summary or wrap-up — the conversation continues.\n"
    "- Another consolidation pass will happen when the session ends, "
    "covering anything you miss now.\n\n"
    + _rem_shared_instructions(user)
    )

ALLOWED_TOOLS = [
    # basic-memory tools
    "mcp__basic_memory__write_note",
    "mcp__basic_memory__edit_note",
    "mcp__basic_memory__search_notes",
    "mcp__basic_memory__read_note",
    # life_manager tools (for task/event extraction)
    "mcp__life_manager__create_task",
    "mcp__life_manager__create_event",
    "mcp__life_manager__list_tasks",
    "mcp__life_manager__list_events",
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


async def consolidate_memories(
    session_id: str,
    after_message_id: int | None = None,
    is_checkpoint: bool = False,
) -> int | None:
    """Review a session (or segment) and write noteworthy memories.

    Returns the id of the last message processed, or None if nothing
    was processed.  The caller uses this as a watermark for the next run.
    """
    log.info("rem.started", session_id=session_id,
             after_message_id=after_message_id, is_checkpoint=is_checkpoint)

    session = await get_session(session_id)
    if not session or not session.messages:
        log.info("rem.no_messages", session_id=session_id)
        return None

    # Filter to unprocessed messages when a watermark is provided
    messages = session.messages
    if after_message_id is not None:
        messages = [m for m in messages if m.id is not None and m.id > after_message_id]

    if not messages:
        log.info("rem.no_new_messages", session_id=session_id,
                 after_message_id=after_message_id)
        return after_message_id

    transcript = _build_transcript(messages)
    if not transcript.strip():
        log.info("rem.empty_transcript", session_id=session_id)
        return after_message_id

    message_count = len([m for m in messages if m.role in ("user", "assistant")])
    log.info("rem.agent_starting", session_id=session_id,
             message_count=message_count, is_checkpoint=is_checkpoint)

    if is_checkpoint:
        prompt = (
            "Here is a SEGMENT of an ongoing conversation to review "
            "(messages since the last checkpoint):\n\n"
            f"{transcript}\n\n"
            "Analyze this conversation segment and store any noteworthy "
            "information. The conversation is still ongoing. "
            "Follow your instructions."
        )
        system_prompt = _build_checkpoint_system_prompt()
    else:
        descriptor = "remaining" if after_message_id else "complete"
        prompt = (
            f"Here is the {descriptor} conversation transcript to review:\n\n"
            f"{transcript}\n\n"
            "Analyze this conversation and store any noteworthy information "
            "in the knowledge base. Follow your instructions."
        )
        system_prompt = _build_rem_system_prompt()

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
        max_turns=20,
        system_prompt=system_prompt,
    )

    async with claude_process_slot(timeout=180):
        async with ClaudeSDKClient(options=options) as client:
            await client.query(prompt)

            async for message in client.receive_response():
                if isinstance(message, AssistantMessage):
                    for block in message.content:
                        if isinstance(block, ToolUseBlock) and block.name in (
                            "mcp__basic_memory__write_note",
                            "mcp__basic_memory__edit_note",
                        ):
                            title = block.input.get("title", "?")
                            content = block.input.get("content", "")
                            log.info(
                                "rem.memory_write",
                                session_id=session_id,
                                tool=block.name.split("__")[-1],
                                title=title,
                                lines=len(content.splitlines()),
                            )
                elif isinstance(message, ResultMessage):
                    if message.is_error:
                        log.warning(
                            "rem.agent_error",
                            session_id=session_id,
                            result=message.result or "unknown",
                        )
                        return None

    last_id = max((m.id for m in messages if m.id is not None), default=None)
    log.info("rem.completed", session_id=session_id,
             is_checkpoint=is_checkpoint, last_message_id=last_id)
    return last_id
