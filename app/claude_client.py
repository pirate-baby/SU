"""
Claude SDK client wrapper for chat functionality.
"""
import os
from typing import Any, AsyncGenerator, Optional
from claude_agent_sdk import (
    ClaudeSDKClient,
    ClaudeAgentOptions,
    UserMessage,
    TextBlock,
    AssistantMessage,
    ToolUseBlock,
    ToolResultBlock,
    ResultMessage,
)
from app.config import settings
from app.website_agent import website_mcp_server
from app.website_models import WEBSITE_REGISTRY


def _build_system_prompt() -> str:
    """Build the system prompt with dynamic website registry info."""
    website_descriptions = "\n".join(
        f"  - \"{name}\": {config.instructions}"
        for name, config in WEBSITE_REGISTRY.items()
    )
    return (
        "You are a helpful personal assistant with the ability to browse "
        "websites on the user's behalf.\n\n"
        "You have a special tool called `browse_website` that launches an "
        "autonomous browser sub-agent to interact with pre-registered websites "
        "and return structured data. When the user asks about any of the "
        "registered websites below, proactively use this tool.\n\n"
        "Registered websites:\n"
        f"{website_descriptions}\n\n"
        "Usage: call the `browse_website` tool with the website name and any "
        "additional instructions. The sub-agent will navigate the site using "
        "the user's browser profile (logged-in sessions) and return structured "
        "results.\n\n"
        "You do not have any other tools besides browse_website."
    )


class ClaudeChat:
    """Wrapper for Claude SDK client."""

    def __init__(self, oauth_token: Optional[str] = None):
        # If OAuth token is provided, set it in the environment
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
            ],
            permission_mode="bypassPermissions",
            max_turns=20,
            system_prompt=_build_system_prompt(),
        )

    async def send_message(
        self,
        message: str,
        conversation_history: list = None
    ) -> AsyncGenerator[dict[str, Any], None]:
        """Yield structured events: text chunks, tool calls, and tool results."""
        try:
            async with ClaudeSDKClient(options=self.options) as client:
                await client.query(message)

                async for msg in client.receive_response():
                    if isinstance(msg, AssistantMessage):
                        for block in msg.content:
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
                        if msg.is_error:
                            yield {
                                "type": "error",
                                "content": msg.result or "Unknown error",
                            }

        except Exception as e:
            yield {"type": "error", "content": f"Error communicating with Claude: {str(e)}"}

    def build_conversation_history(self, messages: list) -> list:
        claude_messages = []

        for msg in messages:
            if msg.role == "user":
                claude_messages.append(
                    UserMessage(content=[TextBlock(text=msg.content)])
                )

        return claude_messages
