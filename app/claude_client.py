"""
Claude SDK client wrapper for chat functionality.
"""
import os
from typing import AsyncGenerator
from claude_agent_sdk import ClaudeSDKClient, ClaudeAgentOptions, UserMessage, TextBlock


class ClaudeChat:
    """Wrapper for Claude SDK client."""

    def __init__(self):
        self.options = ClaudeAgentOptions(
            allowed_tools=["Read", "Glob", "Grep", "WebSearch", "WebFetch"],
            permission_mode="default",
            max_turns=20,
        )

    async def send_message(
        self,
        message: str,
        conversation_history: list = None
    ) -> AsyncGenerator[str, None]:
        try:
            async with ClaudeSDKClient(options=self.options) as client:
                await client.query(message)

                async for msg in client.receive_response():
                    if hasattr(msg, 'content'):
                        for block in msg.content:
                            if hasattr(block, 'text'):
                                yield block.text

        except Exception as e:
            yield f"\n\n[Error communicating with Claude: {str(e)}]"

    def build_conversation_history(self, messages: list) -> list:
        claude_messages = []

        for msg in messages:
            if msg.role == "user":
                claude_messages.append(
                    UserMessage(content=[TextBlock(text=msg.content)])
                )

        return claude_messages
