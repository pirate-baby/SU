"""
Telegram Messenger: send messages to the user via Telegram, exposed as a
plain async function registered on pydantic-ai agents via @agent.tool_plain.
"""
import json
from typing import Any

from app.config import settings
from app.logger import get_logger

log = get_logger(__name__)


async def send_telegram_message(content: str) -> str:
    """Send a Telegram message to the user. Use for quick reminders, questions, or status updates.

    Args:
        content: The message text to send.
    """
    from app.telegram_bot import send_message
    from app.telegram_users import TelegramUserRepo

    users = await TelegramUserRepo.list_all()
    if not users:
        return json.dumps({
            "error": "No Telegram users registered. User needs to /start the bot first.",
        })

    sent = 0
    for user in users:
        chat_id = user["telegram_chat_id"]
        if await send_message(chat_id, content):
            sent += 1

    return json.dumps({"sent": sent, "total_users": len(users)})
