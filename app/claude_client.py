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
                "Read", "Glob", "Grep", "WebSearch", "WebFetch",
                "mcp__website__browse_website",
            ],
            permission_mode="default",
            max_turns=20,
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
