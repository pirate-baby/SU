"""
Claude SDK client wrapper for chat functionality.

Supports two modes:
  - "chat" (default): website browsing only, code tools disabled.
  - "self_iteration": full code tools + restart tool for self-editing.
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
from app.website_agent import website_mcp_server
from app.website_models import WEBSITE_REGISTRY

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# System prompts
# ---------------------------------------------------------------------------

def _build_chat_prompt() -> str:
    """System prompt for normal chat mode (website browsing only)."""
    website_descriptions = "\n".join(
        f"  - \"{name}\": {config.instructions}"
        for name, config in WEBSITE_REGISTRY.items()
    )
    return (
        "You are a helpful personal assistant with the ability to browse "
        "websites on the user's behalf.\n\n"
        "You have a tool called `mcp__website__browse_website` that launches an "
        "autonomous browser sub-agent to interact with pre-registered websites "
        "and return structured data. When the user asks about any of the "
        "registered websites below, proactively use this tool.\n\n"
        "Registered websites:\n"
        f"{website_descriptions}\n\n"
        "Usage: call the `mcp__website__browse_website` tool with the website "
        "name and any additional instructions. The sub-agent will navigate the "
        "site using the user's browser profile (logged-in sessions) and return "
        "structured results.\n\n"
        "You do not have any other tools besides mcp__website__browse_website."
    )


def _build_self_iteration_prompt() -> str:
    """System prompt for self-iteration mode (code editing + restart)."""
    website_descriptions = "\n".join(
        f"  - \"{name}\": {config.instructions}"
        for name, config in WEBSITE_REGISTRY.items()
    )
    return (
        "You are SU, a self-improving AI assistant. You can browse websites on "
        "the user's behalf AND edit your own source code.\n\n"
        "## Website Browsing\n"
        "You have `mcp__website__browse_website` for autonomous website interaction.\n"
        "Registered websites:\n"
        f"{website_descriptions}\n\n"
        "## Self-Iteration\n"
        "Your source code is mounted at `/src`. You can read, edit, and write files "
        "in this codebase using your standard tools (Read, Edit, Write, Bash, Grep, "
        "Glob, etc.).\n\n"
        "When asked to add functionality, fix bugs, or improve yourself:\n"
        "1. `cd /src && git checkout main` — start from main branch\n"
        "2. `git checkout -b feature/<short-description>` — create a feature branch\n"
        "3. Make your code changes using Read/Edit/Write tools\n"
        "4. Run tests: `cd /src && uv run pytest` — fix any failures\n"
        "5. Commit: `git add -A && git commit -m '<description>'`\n"
        "6. Merge: `git checkout main && git merge feature/<short-description>`\n"
        "7. Call `mcp__restart__restart_self` to restart with the new code\n\n"
        "**Important:**\n"
        "- Always run tests before merging.\n"
        "- The running server uses code copied at build time (`/app`), NOT `/src`. "
        "Your edits in `/src` only take effect after restart.\n"
        "- After calling restart_self, the session will resume automatically in "
        "a new WebSocket connection. The conversation history is preserved.\n\n"
        "## Codebase Structure\n"
        "```\n"
        "/src/\n"
        "  app/\n"
        "    main.py             # FastAPI app, WebSocket endpoints\n"
        "    claude_client.py    # Claude SDK wrapper (this file controls your tools)\n"
        "    agent_registry.py   # Agent lifecycle (survives WS disconnects)\n"
        "    session_manager.py  # Session/message CRUD (SQLite)\n"
        "    config.py           # Pydantic settings\n"
        "    database.py         # SQLite init/connection\n"
        "    logger.py           # Structured JSON logging\n"
        "    memory_manager.py   # Background memory agents\n"
        "    website_agent.py    # Playwright subagent MCP tool\n"
        "    restart_tool.py     # Self-restart MCP tool\n"
        "    static/js/chat.js   # WebSocket client UI\n"
        "    templates/          # Jinja2 HTML templates\n"
        "  tests/\n"
        "  Dockerfile\n"
        "  docker-compose.yml\n"
        "  startup.sh\n"
        "  pyproject.toml\n"
        "```\n"
    )


# ---------------------------------------------------------------------------
# ClaudeChat client
# ---------------------------------------------------------------------------

class ClaudeChat:
    """Wrapper for Claude SDK client.

    The SDK client must be kept alive for the duration of a session so that
    conversation history is maintained automatically across query() calls.

    Modes:
      - "chat": website browsing only, all code tools blocked.
      - "self_iteration": full code tools + restart tool.
    """

    def __init__(self, oauth_token: Optional[str] = None, mode: str = "chat"):
        if oauth_token:
            os.environ["CLAUDE_CODE_OAUTH_TOKEN"] = oauth_token

        self.mode = mode

        if mode == "self_iteration":
            self.options = self._build_self_iteration_options()
        else:
            self.options = self._build_chat_options()

        self._client: Optional[ClaudeSDKClient] = None

    def _build_chat_options(self) -> ClaudeAgentOptions:
        """Build options for normal chat mode."""
        return ClaudeAgentOptions(
            mcp_servers={
                "website": website_mcp_server,
            },
            allowed_tools=[
                "mcp__website__browse_website",
            ],
            disallowed_tools=[
                "Task",
                "Bash",
                "Glob",
                "Grep",
                "Read",
                "Edit",
                "Write",
                "WebFetch",
                "WebSearch",
                "NotebookEdit",
                "Skill",
                "TodoWrite",
                "EnterPlanMode",
                "ExitPlanMode",
                "TaskOutput",
                "TaskStop",
            ],
            permission_mode="bypassPermissions",
            max_turns=20,
            system_prompt=_build_chat_prompt(),
        )

    def _build_self_iteration_options(self) -> ClaudeAgentOptions:
        """Build options for self-iteration mode (code editing + restart)."""
        from app.restart_tool import restart_mcp_server

        return ClaudeAgentOptions(
            mcp_servers={
                "website": website_mcp_server,
                "restart": restart_mcp_server,
            },
            disallowed_tools=[
                "NotebookEdit",
                "Skill",
                "EnterPlanMode",
                "ExitPlanMode",
            ],
            permission_mode="bypassPermissions",
            max_turns=100,
            system_prompt=_build_self_iteration_prompt(),
        )

    async def connect(self):
        """Open and connect the SDK client. Must be called before send_message."""
        log.info("sdk.connecting", mode=self.mode)
        self._client = ClaudeSDKClient(options=self.options)
        await self._client.connect()
        log.info("sdk.connected", mode=self.mode)

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
