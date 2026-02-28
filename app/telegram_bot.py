"""
Telegram bot integration for SU.

Handles:
  - /start registration (captures chat_id)
  - Inbound text messages → Claude session → response back via Telegram
  - Outbound message delivery (interjections, direct sends)
  - Webhook setup with FastAPI
"""
import asyncio
import json
import secrets
from typing import Optional

import httpx
from fastapi import FastAPI, Request, Response

from app.config import settings
from app.logger import get_logger
from app.telegram_users import TelegramUserRepo
from app.telegram_session import (
    get_or_create_session,
    inject_previous_context,
    touch_session,
)
from app.session_manager import save_message, update_session_activity
from app.agent_registry import get_or_create_agent, get_lock, touch
from app.memory_manager import on_first_message, on_user_message

log = get_logger(__name__)

# Telegram Bot API base URL
_API_BASE: Optional[str] = None

# Max message length for Telegram
_MAX_MSG_LEN = 4096


def _api_base() -> str:
    global _API_BASE
    if _API_BASE is None:
        _API_BASE = f"https://api.telegram.org/bot{settings.telegram_bot_token}"
    return _API_BASE


# ---------------------------------------------------------------------------
# Outbound: send messages to Telegram
# ---------------------------------------------------------------------------

async def send_message(chat_id: int, text: str, reply_markup: Optional[dict] = None) -> bool:
    """Send a text message to a Telegram chat. Splits long messages."""
    if not settings.telegram_bot_token:
        return False

    chunks = _split_message(text)
    for i, chunk in enumerate(chunks):
        payload: dict = {
            "chat_id": chat_id,
            "text": chunk,
            "parse_mode": "Markdown",
        }
        # Only attach reply_markup to the last chunk
        if reply_markup and i == len(chunks) - 1:
            payload["reply_markup"] = reply_markup

        async with httpx.AsyncClient(timeout=15.0) as client:
            try:
                resp = await client.post(f"{_api_base()}/sendMessage", json=payload)
                if resp.status_code != 200:
                    # Retry without Markdown if parsing failed
                    if "can't parse" in resp.text.lower() or "bad request" in resp.text.lower():
                        payload["parse_mode"] = None
                        resp = await client.post(f"{_api_base()}/sendMessage", json=payload)
                resp.raise_for_status()
            except Exception:
                log.exception("telegram.send_failed", chat_id=chat_id)
                return False

    return True


async def send_call_notification(chat_id: int, summary: str, call_url: str) -> bool:
    """Send a Telegram message with an inline 'Answer Call' button."""
    text = f"*SU is calling...*\n\n{summary[:200]}"
    markup = {
        "inline_keyboard": [[
            {"text": "Answer Call", "url": call_url}
        ]]
    }
    return await send_message(chat_id, text, reply_markup=markup)


def _split_message(text: str) -> list[str]:
    """Split text into Telegram-safe chunks."""
    if len(text) <= _MAX_MSG_LEN:
        return [text]
    chunks = []
    while text:
        if len(text) <= _MAX_MSG_LEN:
            chunks.append(text)
            break
        # Try to split at a newline near the limit
        split_at = text.rfind("\n", 0, _MAX_MSG_LEN)
        if split_at < _MAX_MSG_LEN // 2:
            split_at = _MAX_MSG_LEN
        chunks.append(text[:split_at])
        text = text[split_at:].lstrip("\n")
    return chunks


# ---------------------------------------------------------------------------
# Inbound: process messages from Telegram
# ---------------------------------------------------------------------------

async def _handle_start(chat_id: int, user_id: int, username: Optional[str]) -> None:
    """Handle /start command — register the user."""
    await TelegramUserRepo.upsert(chat_id, user_id, username)
    log.info("telegram.user_registered", chat_id=chat_id, username=username)
    await send_message(
        chat_id,
        f"Hey. {settings.su_name} here. You can text me anytime.",
    )


async def _handle_text_message(chat_id: int, text: str) -> None:
    """Process an inbound text message: route to Claude, send response."""
    session_id, is_new = await get_or_create_session(chat_id)

    if is_new:
        await inject_previous_context(session_id, chat_id)

    # Save user message
    await save_message(session_id, "user", text)
    touch(session_id)
    touch_session(session_id, chat_id)
    log.info("telegram.user_message", session_id=session_id, chat_id=chat_id, length=len(text))

    # Get or create Claude agent
    try:
        claude = await get_or_create_agent(session_id)
    except Exception:
        log.exception("telegram.agent_init_failed", session_id=session_id)
        await send_message(chat_id, "Sorry, I couldn't connect right now. Try again in a moment.")
        return

    # Run subconscious memory on first message
    from app.session_manager import get_session
    session = await get_session(session_id)
    user_messages = [m for m in (session.messages if session else []) if m.role == "user"]
    if len(user_messages) == 1:
        await on_first_message(session_id)
    else:
        asyncio.ensure_future(on_user_message(session_id))

    # Collect memories and build effective message
    from app.main import _collect_and_consume_memories
    context_prefix = await _collect_and_consume_memories(session_id)
    effective_message = (context_prefix + text) if context_prefix else text

    # Stream response from Claude (accumulate — no streaming for Telegram)
    full_response = ""
    try:
        async for event in claude.send_message(effective_message):
            if event["type"] == "text":
                full_response += event["content"]
            elif event["type"] == "error":
                log.error("telegram.claude_error", session_id=session_id, content=event["content"])
    except Exception:
        log.exception("telegram.claude_stream_failed", session_id=session_id)
        await send_message(chat_id, "Something went wrong. Try again?")
        return

    if full_response.strip():
        await save_message(session_id, "assistant", full_response)
        await send_message(chat_id, full_response)
    else:
        log.warning("telegram.empty_response", session_id=session_id)

    await update_session_activity(session_id)
    touch_session(session_id, chat_id)


# ---------------------------------------------------------------------------
# Webhook: process incoming updates from Telegram
# ---------------------------------------------------------------------------

async def process_update(update: dict) -> None:
    """Process a raw Telegram update dict."""
    message = update.get("message")
    if not message:
        return

    chat = message.get("chat", {})
    chat_id = chat.get("id")
    user = message.get("from", {})
    user_id = user.get("id")
    username = user.get("username")
    text = message.get("text", "").strip()

    if not chat_id or not text:
        return

    if text.startswith("/start"):
        await _handle_start(chat_id, user_id, username)
    else:
        await _handle_text_message(chat_id, text)


# ---------------------------------------------------------------------------
# FastAPI integration
# ---------------------------------------------------------------------------

def setup_webhook_routes(app: FastAPI) -> None:
    """Register the Telegram webhook route with FastAPI."""
    if not settings.telegram_bot_token:
        return

    webhook_secret = settings.telegram_webhook_secret or secrets.token_hex(16)

    @app.post(f"/telegram/webhook/{webhook_secret}")
    async def telegram_webhook(request: Request):
        try:
            body = await request.json()
            # Process in background so we return 200 quickly
            asyncio.ensure_future(process_update(body))
        except Exception:
            log.exception("telegram.webhook_error")
        return Response(status_code=200)

    @app.on_event("startup")
    async def _set_telegram_webhook():
        if not settings.app_host or settings.app_host == "localhost":
            log.warning("telegram.webhook_skip", reason="app_host not set or localhost")
            return

        webhook_url = f"https://{settings.app_host}/telegram/webhook/{webhook_secret}"
        async with httpx.AsyncClient(timeout=15.0) as client:
            try:
                resp = await client.post(
                    f"{_api_base()}/setWebhook",
                    json={"url": webhook_url, "allowed_updates": ["message"]},
                )
                data = resp.json()
                if data.get("ok"):
                    log.info("telegram.webhook_set", url=webhook_url)
                else:
                    log.error("telegram.webhook_set_failed", response=data)
            except Exception:
                log.exception("telegram.webhook_set_error")


# ---------------------------------------------------------------------------
# Interjection delivery via Telegram
# ---------------------------------------------------------------------------

async def deliver_interjection_via_telegram(interjection: dict) -> int:
    """Send an interjection to all registered Telegram users.

    Returns the number of users notified.
    """
    if not settings.telegram_bot_token:
        return 0

    users = await TelegramUserRepo.list_all()
    if not users:
        return 0

    content = interjection.get("content", "")
    urgency = interjection.get("urgency", "normal")

    # Format based on urgency
    if urgency in ("high", "urgent"):
        text = f"*{content}*"
    else:
        text = content

    delivered = 0
    for user in users:
        chat_id = user["telegram_chat_id"]
        if await send_message(chat_id, text):
            delivered += 1

    if delivered:
        log.info(
            "telegram.interjection_delivered",
            interjection_id=interjection.get("id"),
            delivered=delivered,
        )

    return delivered
