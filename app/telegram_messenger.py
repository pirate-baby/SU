"""
Telegram Messenger MCP server: exposes Telegram messaging as MCP tools
so Claude and daemon agents can text the user directly.
"""
import json
from typing import Any

import inspect
from mcp.server import Server as _McpServer

# Monkey-patch: same fix as life_manager.py for mcp 0.9.x+
_orig_server_init = _McpServer.__init__
if "version" not in inspect.signature(_orig_server_init).parameters:
    def _patched_server_init(self, name, **kwargs):
        version = kwargs.pop("version", "1.0.0")
        _orig_server_init(self, name, **kwargs)
        self.version = version
    _McpServer.__init__ = _patched_server_init

from claude_agent_sdk import create_sdk_mcp_server, tool

from app.config import settings
from app.logger import get_logger

log = get_logger(__name__)


def _json_response(data: Any, is_error: bool = False) -> dict[str, Any]:
    """Build an MCP-formatted tool response."""
    result: dict[str, Any] = {
        "content": [{"type": "text", "text": json.dumps(data, indent=2, default=str)}],
    }
    if is_error:
        result["is_error"] = True
    return result


@tool(
    "send_telegram_message",
    f"Send a Telegram message to {settings.user_name}. Use this to text "
    "the user directly — for quick reminders, questions, status updates, "
    "or anything that doesn't need a full conversation session.",
    {
        "type": "object",
        "properties": {
            "content": {
                "type": "string",
                "description": "The message text to send.",
            },
        },
        "required": ["content"],
        "additionalProperties": False,
    },
)
async def send_telegram_message(content: str) -> dict[str, Any]:
    from app.telegram_bot import send_message
    from app.telegram_users import TelegramUserRepo

    users = await TelegramUserRepo.list_all()
    if not users:
        return _json_response(
            {"error": "No Telegram users registered. User needs to /start the bot first."},
            is_error=True,
        )

    sent = 0
    for user in users:
        chat_id = user["telegram_chat_id"]
        if await send_message(chat_id, content):
            sent += 1

    return _json_response({"sent": sent, "total_users": len(users)})


telegram_messenger_mcp_server = create_sdk_mcp_server(
    "telegram_messenger",
    tools=[send_telegram_message],
)
