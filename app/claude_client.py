"""
Claude SDK client wrapper for chat functionality.
"""
import logging
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
from app.website_agent import website_mcp_server
from app.website_models import WEBSITE_REGISTRY

logger = logging.getLogger(__name__)


def _build_system_prompt() -> str:
    """Build the system prompt with dynamic website registry info."""
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


class ClaudeChat:
    """Wrapper for Claude SDK client.

    The SDK client must be kept alive for the duration of a session so that
    conversation history is maintained automatically across query() calls.
    Use as an async context manager to ensure proper connect/disconnect.
    """

    def __init__(self, oauth_token: Optional[str] = None):
        if oauth_token:
            os.environ["CLAUDE_CODE_OAUTH_TOKEN"] = oauth_token

        self.options = ClaudeAgentOptions(
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
            system_prompt=_build_system_prompt(),
        )
        self._client: Optional[ClaudeSDKClient] = None

    async def connect(self):
        """Open and connect the SDK client. Must be called before send_message."""
        self._client = ClaudeSDKClient(options=self.options)
        await self._client.connect()

    async def disconnect(self):
        """Disconnect the SDK client."""
        if self._client:
            await self._client.disconnect()
            self._client = None

    async def __aenter__(self):
        await self.connect()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.disconnect()

    async def send_message(
        self,
        message: str,
    ) -> AsyncGenerator[dict[str, Any], None]:
        """Yield structured events: text chunks, tool calls, and tool results.

        The underlying SDK client persists across calls, so conversation
        history is maintained automatically between messages.
        """
        if not self._client:
            raise RuntimeError("ClaudeChat client is not connected. Call connect() or use as async context manager.")

        try:
            await self._client.query(message)

            async for msg in self._client.receive_response():
                logger.info("SDK message: type=%s", type(msg).__name__)

                if isinstance(msg, SystemMessage):
                    logger.info(
                        "SystemMessage subtype=%s data=%s",
                        msg.subtype,
                        msg.data,
                    )
                    if msg.subtype == "init":
                        mcp_servers = msg.data.get("mcp_servers", [])
                        for srv in mcp_servers:
                            if srv.get("status") != "connected":
                                logger.error(
                                    "MCP server failed to connect: %s", srv
                                )

                elif isinstance(msg, UserMessage):
                    # Tool results come back as UserMessage in the Claude API.
                    content = msg.content
                    if isinstance(content, list):
                        for block in content:
                            if isinstance(block, ToolResultBlock):
                                logger.info(
                                    "  ToolResult via UserMessage tool_use_id=%s is_error=%s",
                                    block.tool_use_id,
                                    block.is_error,
                                )
                                yield {
                                    "type": "tool_result",
                                    "tool_use_id": block.tool_use_id,
                                    "content": block.content,
                                    "is_error": block.is_error or False,
                                }
                            else:
                                logger.info(
                                    "  UserMessage block type=%s",
                                    type(block).__name__,
                                )
                    else:
                        logger.info("  UserMessage content=%s", str(content)[:200])

                elif isinstance(msg, AssistantMessage):
                    for block in msg.content:
                        logger.info(
                            "  block type=%s", type(block).__name__
                        )
                        if isinstance(block, TextBlock):
                            yield {"type": "text", "content": block.text}
                        elif isinstance(block, ToolUseBlock):
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
                    logger.info(
                        "ResultMessage subtype=%s is_error=%s result=%s",
                        getattr(msg, "subtype", None),
                        msg.is_error,
                        (msg.result or "")[:500],
                    )
                    if msg.is_error:
                        yield {
                            "type": "error",
                            "content": msg.result or "Unknown error",
                        }

        except Exception as e:
            logger.exception("Error in send_message")
            yield {"type": "error", "content": f"Error communicating with Claude: {str(e)}"}
