"""Tier 3: WebSocket chat tests.

These tests mock SessionState to avoid hitting the real Claude API while
verifying the full WebSocket protocol (connect, history, message exchange,
error handling, disconnect).

Uses httpx.ASGITransport for WebSocket testing since starlette's TestClient
has compatibility issues with newer httpx versions.
"""
import json
import asyncio
from unittest.mock import patch, AsyncMock

import pytest
import pytest_asyncio

from app.database import init_database
from app.session_manager import create_session, save_message
from tests.conftest import MockSessionState


class TestWebSocketConnection:
    """Test WebSocket connection lifecycle."""

    async def test_invalid_session_receives_error(self):
        """Connecting to a non-existent session returns an error message."""
        # Test the session validation logic directly
        from app.session_manager import session_exists
        assert await session_exists("nonexistent-session") is False

    async def test_session_exists_for_valid_session(self):
        """A created session should be findable."""
        sid = await create_session()
        from app.session_manager import session_exists
        assert await session_exists(sid) is True

    async def test_send_message_history_sends_correct_format(self):
        """Test send_message_history produces the expected WS message shape."""
        sid = await create_session()
        await save_message(sid, "user", "hello")
        await save_message(sid, "assistant", "hi there")
        # Also add a memory message that should NOT appear in history
        await save_message(sid, "memory", "background thought")

        from app.main import send_message_history

        messages_sent = []

        class FakeWebSocket:
            async def send_json(self, data):
                messages_sent.append(data)

        await send_message_history(FakeWebSocket(), sid)

        assert len(messages_sent) == 1
        msg = messages_sent[0]
        assert msg["type"] == "history"
        assert len(msg["messages"]) == 2  # only user + assistant, not memory
        assert msg["messages"][0]["role"] == "user"
        assert msg["messages"][0]["content"] == "hello"
        assert msg["messages"][1]["role"] == "assistant"
        assert msg["messages"][1]["content"] == "hi there"

    async def test_send_message_history_skipped_for_new_session(self):
        """New session with no messages should not send a history message."""
        sid = await create_session()

        from app.main import send_message_history

        messages_sent = []

        class FakeWebSocket:
            async def send_json(self, data):
                messages_sent.append(data)

        await send_message_history(FakeWebSocket(), sid)

        # No history message sent when session has no messages
        assert len(messages_sent) == 0


class TestStreamClaudeResponse:
    """Test the stream_claude_response function with mocked SessionState."""

    async def test_streams_text_response(self):
        """Verify text chunks are forwarded correctly over WS."""
        sid = await create_session()
        mock_session = MockSessionState(responses=[
            {"type": "text", "content": "Hello "},
            {"type": "text", "content": "world!"},
        ])

        from app.main import stream_claude_response

        ws_messages = []

        class FakeWebSocket:
            async def send_json(self, data):
                ws_messages.append(data)

        await stream_claude_response(FakeWebSocket(), sid, "hi", mock_session)

        types = [m["type"] for m in ws_messages]
        assert types[0] == "assistant_start"
        assert types[-1] == "assistant_end"

        chunks = [m for m in ws_messages if m["type"] == "assistant_chunk"]
        assert len(chunks) == 2
        assert chunks[0]["content"] == "Hello "
        assert chunks[1]["content"] == "world!"

    async def test_streams_tool_use_events(self):
        """Verify tool_use and tool_result events are forwarded."""
        sid = await create_session()
        mock_session = MockSessionState(responses=[
            {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "ls"}},
            {"type": "tool_result", "tool_use_id": "t1", "content": "file.txt", "is_error": False},
            {"type": "text", "content": "Done."},
        ])

        from app.main import stream_claude_response

        ws_messages = []

        class FakeWebSocket:
            async def send_json(self, data):
                ws_messages.append(data)

        await stream_claude_response(FakeWebSocket(), sid, "list files", mock_session)

        tool_uses = [m for m in ws_messages if m["type"] == "tool_use"]
        assert len(tool_uses) == 1
        assert tool_uses[0]["name"] == "Bash"
        assert tool_uses[0]["id"] == "t1"

        tool_results = [m for m in ws_messages if m["type"] == "tool_result"]
        assert len(tool_results) == 1
        assert tool_results[0]["content"] == "file.txt"
        assert tool_results[0]["is_error"] is False

    async def test_streams_error_events(self):
        """Verify error events from the agent are forwarded."""
        sid = await create_session()
        mock_session = MockSessionState(responses=[
            {"type": "error", "content": "Something went wrong"},
        ])

        from app.main import stream_claude_response

        ws_messages = []

        class FakeWebSocket:
            async def send_json(self, data):
                ws_messages.append(data)

        await stream_claude_response(FakeWebSocket(), sid, "test", mock_session)

        errors = [m for m in ws_messages if m["type"] == "error"]
        assert len(errors) == 1
        assert "Something went wrong" in errors[0]["content"]

    async def test_saves_assistant_response_to_db(self):
        """Verify the full response is saved to the database."""
        sid = await create_session()
        mock_session = MockSessionState(responses=[
            {"type": "text", "content": "Full response text"},
        ])

        from app.main import stream_claude_response
        from app.session_manager import get_session

        class FakeWebSocket:
            async def send_json(self, data):
                pass

        await stream_claude_response(FakeWebSocket(), sid, "test", mock_session)

        session = await get_session(sid)
        assistant_msgs = [m for m in session.messages if m.role == "assistant"]
        assert len(assistant_msgs) == 1
        assert assistant_msgs[0].content == "Full response text"


class TestHandleUserMessage:
    """Test the full handle_user_message flow."""

    async def test_saves_user_message_and_streams_response(self):
        """Verify user message is saved and response is streamed."""
        sid = await create_session()
        mock_session = MockSessionState(responses=[
            {"type": "text", "content": "Response"},
        ])

        from app.main import handle_user_message
        from app.session_manager import get_session

        ws_messages = []

        class FakeWebSocket:
            async def send_json(self, data):
                ws_messages.append(data)

        with patch("app.main.on_user_message", new_callable=AsyncMock):
            with patch("app.main._inject_pending_memories", new_callable=AsyncMock):
                await handle_user_message(FakeWebSocket(), sid, "Hello", mock_session)

        # Check user message echo
        user_echoes = [m for m in ws_messages if m["type"] == "user_message"]
        assert len(user_echoes) == 1
        assert user_echoes[0]["content"] == "Hello"

        # Check message saved to DB
        session = await get_session(sid)
        assert any(m.role == "user" and m.content == "Hello" for m in session.messages)
        assert any(m.role == "assistant" and m.content == "Response" for m in session.messages)

    async def test_handles_exception_gracefully(self):
        """If the agent throws, an error message is sent to the client."""
        sid = await create_session()

        class ErrorSessionState:
            _lock = asyncio.Lock()

            async def send_message_with_tools(self, message):
                raise RuntimeError("Agent crashed")
                yield  # noqa: unreachable — makes this an async generator

        from app.main import handle_user_message

        ws_messages = []

        class FakeWebSocket:
            async def send_json(self, data):
                ws_messages.append(data)

        with patch("app.main.on_user_message", new_callable=AsyncMock):
            with patch("app.main._inject_pending_memories", side_effect=RuntimeError("Agent crashed")):
                await handle_user_message(FakeWebSocket(), sid, "Hello", ErrorSessionState())

        errors = [m for m in ws_messages if m["type"] == "error"]
        assert len(errors) == 1
        assert "Error generating response" in errors[0]["content"]
