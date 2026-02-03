"""
Claude SDK client wrapper for chat functionality.
"""
import os
from typing import AsyncGenerator
from claude_agent_sdk import ClaudeSDKClient, ClaudeAgentOptions, UserMessage, TextBlock


class ClaudeChat:
    """Wrapper for Claude SDK client."""

    def __init__(self):
        print("Initializing ClaudeChat with ClaudeAgentOptions")
        try:
            self.options = ClaudeAgentOptions(
                allowed_tools=["Read", "Glob", "Grep", "WebSearch", "WebFetch"],
                permission_mode="default",
                max_turns=20,
            )
            print("ClaudeAgentOptions initialized successfully")
        except Exception as e:
            print(f"Failed to initialize ClaudeAgentOptions: {str(e)}")
            raise

    async def send_message(
        self,
        message: str,
        conversation_history: list = None
    ) -> AsyncGenerator[str, None]:
        try:
            print(f"Creating ClaudeSDKClient with message: {message[:50]}...")
            async with ClaudeSDKClient(options=self.options) as client:
                print("ClaudeSDKClient created successfully, sending query")
                await client.query(message)
                print("Query sent, waiting for response")

                async for msg in client.receive_response():
                    if hasattr(msg, 'content'):
                        for block in msg.content:
                            if hasattr(block, 'text'):
                                yield block.text

        except Exception as e:
            error_msg = f"\n\n[Error communicating with Claude: {str(e)}]"
            print(f"Error in send_message: {error_msg}")
            import traceback
            traceback.print_exc()
            yield error_msg

    def build_conversation_history(self, messages: list) -> list:
        claude_messages = []

        for msg in messages:
            if msg.role == "user":
                claude_messages.append(
                    UserMessage(content=[TextBlock(text=msg.content)])
                )

        return claude_messages
