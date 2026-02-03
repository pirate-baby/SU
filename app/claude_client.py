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
            permission_mode="prompt",
            max_turns=20,
        )

    async def send_message(
        self,
        message: str,
        conversation_history: list = None
    ) -> AsyncGenerator[str, None]:
        client = ClaudeSDKClient(options=self.options)

        messages = conversation_history if conversation_history else []
        messages.append(
            UserMessage(content=[TextBlock(text=message)])
        )

        try:
            async for chunk in client.stream(messages):
                if hasattr(chunk, 'content'):
                    for block in chunk.content:
                        if hasattr(block, 'text'):
                            yield block.text
                elif hasattr(chunk, 'delta') and hasattr(chunk.delta, 'text'):
                    yield chunk.delta.text

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
