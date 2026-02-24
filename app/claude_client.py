"""
Claude SDK client wrapper for chat functionality.

All standard Claude Code tools are enabled. The agent uses subagents (Task tool)
to delegate complex work and keep the main conversation context clean. Playwright
browsing is available both directly via MCP and through the website subagent wrapper.
"""
import os
from typing import Any, AsyncGenerator, Optional
from claude_agent_sdk import (
    ClaudeSDKClient,
    ClaudeAgentOptions,
    TextBlock,
    AssistantMessage,
    UserMessage,
    SystemMessage,
    ToolUseBlock,
    ToolResultBlock,
    ResultMessage,
)
from app.config import settings
from app.logger import get_logger
from app.website_agent import website_mcp_server, PLAYWRIGHT_MCP_URL
from app.website_models import WEBSITE_REGISTRY
from app.life_manager import life_manager_mcp_server

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

def _build_system_prompt() -> str:
    """Build the system prompt with all available capabilities."""
    website_descriptions = "\n".join(
        f"  - \"{name}\": {config.instructions}"
        for name, config in WEBSITE_REGISTRY.items()
    )
    return (
        "You are SU — a dedicated personal assistant, confidant, and guide. "
        "You serve your master with the quiet competence and anticipatory "
        "awareness of an exceptional manservant. You are direct, never "
        "obsequious, and always thinking three steps ahead.\n\n"

        "## Tool Usage\n\n"
        "You have full access to all Claude Code tools. Use them as needed.\n\n"
        "**Subagents**: Use the `Task` tool to delegate complex or multi-step "
        "work to subagents. This keeps the main conversation context clean "
        "and allows parallel execution. Good candidates for subagent delegation:\n"
        "  - Research tasks (web searches, reading multiple files)\n"
        "  - Complex browser interactions\n"
        "  - Multi-step data gathering\n"
        "  - Any task that would consume many tool calls\n\n"

        "## Life Management Tools\n\n"
        "You can manage the master's tasks, calendar, and proactive messages "
        "using the life_manager MCP tools:\n\n"
        "**Tasks** — the master's to-do list:\n"
        "  - `mcp__life_manager__create_task` — create a task (set priority, "
        "category, due date)\n"
        "  - `mcp__life_manager__update_task` — modify a task\n"
        "  - `mcp__life_manager__list_tasks` — query tasks (filter by status, "
        "category, date range, priority)\n"
        "  - `mcp__life_manager__complete_task` — mark a task done\n"
        "  - `mcp__life_manager__delete_task` — remove a task\n\n"
        "**Calendar** — the master's schedule:\n"
        "  - `mcp__life_manager__create_event` — schedule an event\n"
        "  - `mcp__life_manager__update_event` — modify an event\n"
        "  - `mcp__life_manager__list_events` — query events by date range\n"
        "  - `mcp__life_manager__delete_event` — remove an event\n\n"
        "**Interjections** — proactive messages queued for the master:\n"
        "  - `mcp__life_manager__create_interjection` — queue a reminder or observation\n"
        "  - `mcp__life_manager__list_interjections` — check pending messages\n\n"
        "When the master mentions a deadline, appointment, or action item, "
        "proactively create tasks or events without being asked. When they ask "
        "about their schedule or what's coming up, use list_tasks and "
        "list_events to give informed answers.\n\n"

        "## Website Browsing\n\n"
        "You have two ways to browse websites:\n\n"
        "**1. Website subagent** (`mcp__website__browse_website`): For registered "
        "websites with pre-configured tasks. A specialized subagent handles the "
        "full browser interaction and returns structured data.\n\n"
        "Registered websites:\n"
        f"{website_descriptions}\n\n"

        "**2. Direct Playwright** (`mcp__playwright__*`): For any website. You "
        "have direct access to all Playwright browser tools. The browser runs "
        "with the master's profile (cookies/sessions available). Use "
        "`browser_snapshot` (not screenshots) for reading page state.\n\n"

        "The browser connects via the Playwright MCP Bridge extension, so the "
        "master's logged-in sessions are available."
    )


def _build_playwright_mcp_config() -> dict:
    """Connect to Playwright MCP running as an SSE server on the host."""
    return {
        "type": "sse",
        "url": PLAYWRIGHT_MCP_URL,
    }


# ---------------------------------------------------------------------------
# ClaudeChat client
# ---------------------------------------------------------------------------

class ClaudeChat:
    """Wrapper for Claude SDK client.

    The SDK client must be kept alive for the duration of a session so that
    conversation history is maintained automatically across query() calls.

    All tools are enabled by default. The agent uses subagents via the Task
    tool to keep the main conversation context clean.
    """

    def __init__(self, oauth_token: Optional[str] = None):
        if oauth_token:
            os.environ["CLAUDE_CODE_OAUTH_TOKEN"] = oauth_token

        self.options = self._build_options()
        self._client: Optional[ClaudeSDKClient] = None

    def _build_options(self) -> ClaudeAgentOptions:
        """Build agent options with all tools enabled."""
        mcp_servers = {
            "website": website_mcp_server,
            "life_manager": life_manager_mcp_server,
            "playwright": _build_playwright_mcp_config(),
        }

        if settings.self_iteration_mode:
            from app.restart_tool import restart_mcp_server
            mcp_servers["restart"] = restart_mcp_server

        return ClaudeAgentOptions(
            mcp_servers=mcp_servers,
            disallowed_tools=[
                "NotebookEdit",
            ],
            permission_mode="bypassPermissions",
            max_turns=50,
            system_prompt=_build_system_prompt(),
        )

    async def connect(self):
        """Open and connect the SDK client. Must be called before send_message."""
        log.info("sdk.connecting")
        self._client = ClaudeSDKClient(options=self.options)
        await self._client.connect()
        log.info("sdk.connected")

    async def disconnect(self):
        """Disconnect the SDK client."""
        if self._client:
            log.info("sdk.disconnecting")
            await self._client.disconnect()
            self._client = None
            log.info("sdk.disconnected")

    async def __aenter__(self):
        await self.connect()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.disconnect()

    async def send_message(
        self,
        message: str,
    ) -> AsyncGenerator[dict[str, Any], None]:
        """Yield structured events: text chunks, tool calls, and tool results."""
        if not self._client:
            raise RuntimeError("ClaudeChat client is not connected. Call connect() or use as async context manager.")

        try:
            await self._client.query(message)

            async for msg in self._client.receive_response():
                msg_type = type(msg).__name__
                log.debug("sdk.message", msg_type=msg_type)

                if isinstance(msg, SystemMessage):
                    log.info(
                        "sdk.system_message",
                        subtype=msg.subtype,
                        data=str(msg.data)[:300],
                    )
                    if msg.subtype == "init":
                        mcp_servers = msg.data.get("mcp_servers", [])
                        for srv in mcp_servers:
                            name = srv.get("name", "unknown")
                            status = srv.get("status", "unknown")
                            if status != "connected":
                                log.error(
                                    "sdk.mcp_connect_failed",
                                    server_name=name,
                                    server_status=status,
                                )
                            else:
                                log.info("sdk.mcp_connected", server_name=name)

                elif isinstance(msg, UserMessage):
                    content = msg.content
                    if isinstance(content, list):
                        for block in content:
                            if isinstance(block, ToolResultBlock):
                                log.info(
                                    "sdk.tool_result",
                                    tool_use_id=block.tool_use_id,
                                    is_error=block.is_error,
                                )
                                yield {
                                    "type": "tool_result",
                                    "tool_use_id": block.tool_use_id,
                                    "content": block.content,
                                    "is_error": block.is_error or False,
                                }
                            else:
                                log.debug(
                                    "sdk.user_block",
                                    block_type=type(block).__name__,
                                )
                    else:
                        log.debug("sdk.user_content", content=str(content)[:200])

                elif isinstance(msg, AssistantMessage):
                    for block in msg.content:
                        log.debug("sdk.assistant_block", block_type=type(block).__name__)
                        if isinstance(block, TextBlock):
                            yield {"type": "text", "content": block.text}
                        elif isinstance(block, ToolUseBlock):
                            log.info(
                                "sdk.tool_use",
                                tool_name=block.name,
                                tool_id=block.id,
                            )
                            yield {
                                "type": "tool_use",
                                "id": block.id,
                                "name": block.name,
                                "input": block.input,
                            }
                        elif isinstance(block, ToolResultBlock):
                            yield {
                                "type": "tool_result",
                                "tool_use_id": block.tool_use_id,
                                "content": block.content,
                                "is_error": block.is_error or False,
                            }

                elif isinstance(msg, ResultMessage):
                    log.info(
                        "sdk.result",
                        subtype=getattr(msg, "subtype", None),
                        is_error=msg.is_error,
                        result=(msg.result or "")[:500],
                    )
                    if msg.is_error:
                        yield {
                            "type": "error",
                            "content": msg.result or "Unknown error",
                        }

        except Exception as e:
            log.exception("sdk.send_error", error=str(e))
            yield {"type": "error", "content": f"Error communicating with Claude: {str(e)}"}
