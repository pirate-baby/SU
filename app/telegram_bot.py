"""
Telegram bot integration for SU.

Handles:
  - /start registration (captures chat_id)
  - Inbound text messages → Claude session → response back via Telegram
  - Inbound photos/documents → downloaded, passed to Claude for analysis
  - Outbound message delivery (interjections, direct sends)
  - Long-polling for updates (no public URL required)
"""
import asyncio
import json
import os
import tempfile
from pathlib import Path
from typing import Optional

import httpx

from app.config import settings
from app.logger import get_logger
from app.telegram_users import TelegramUserRepo
from app.telegram_session import (
    get_or_create_session,
    inject_previous_context,
    touch_session,
)
from app.session_manager import save_message, update_session_activity
from app.agent_registry import get_or_create_session as get_or_create_agent, get_lock, touch
from app.memory_manager import on_first_message, on_user_message

log = get_logger(__name__)

# Telegram Bot API base URL
_API_BASE: Optional[str] = None

# Max message length for Telegram
_MAX_MSG_LEN = 4096

# Background polling task handle
_poll_task: Optional[asyncio.Task] = None


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


# ---------------------------------------------------------------------------
# File downloads from Telegram
# ---------------------------------------------------------------------------

# Persistent temp dir for downloaded files (survives across messages in a session)
_UPLOAD_DIR: Optional[Path] = None


def _get_upload_dir() -> Path:
    """Return (and lazily create) the persistent upload directory."""
    global _UPLOAD_DIR
    if _UPLOAD_DIR is None:
        _UPLOAD_DIR = Path(tempfile.mkdtemp(prefix="su_telegram_uploads_"))
    return _UPLOAD_DIR


async def _download_telegram_file(file_id: str, filename_hint: Optional[str] = None) -> Optional[Path]:
    """Download a file from Telegram by file_id. Returns local path or None."""
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            # Step 1: get file path from Telegram
            resp = await client.get(
                f"{_api_base()}/getFile",
                params={"file_id": file_id},
            )
            data = resp.json()
            if not data.get("ok"):
                log.error("telegram.get_file_failed", file_id=file_id, response=data)
                return None

            file_path = data["result"]["file_path"]
            file_size = data["result"].get("file_size", 0)

            # Reject files over 20MB (Telegram's limit)
            if file_size > 20 * 1024 * 1024:
                log.warning("telegram.file_too_large", file_id=file_id, size=file_size)
                return None

            # Step 2: determine local filename
            if filename_hint:
                local_name = filename_hint
            else:
                local_name = os.path.basename(file_path)
            local_path = _get_upload_dir() / f"{file_id}_{local_name}"

            # Step 3: download the file
            download_url = f"https://api.telegram.org/file/bot{settings.telegram_bot_token}/{file_path}"
            resp = await client.get(download_url)
            resp.raise_for_status()

            local_path.write_bytes(resp.content)
            log.info("telegram.file_downloaded", file_id=file_id, path=str(local_path), size=len(resp.content))
            return local_path

    except Exception:
        log.exception("telegram.file_download_error", file_id=file_id)
        return None


def _mime_for_extension(path: Path) -> str:
    """Guess a human-readable file type from extension."""
    ext = path.suffix.lower()
    types = {
        ".jpg": "photo", ".jpeg": "photo", ".png": "photo", ".gif": "image",
        ".webp": "photo", ".heic": "photo",
        ".pdf": "PDF document", ".doc": "Word document", ".docx": "Word document",
        ".xls": "spreadsheet", ".xlsx": "spreadsheet", ".csv": "CSV file",
        ".mp4": "video", ".mov": "video", ".avi": "video",
        ".mp3": "audio", ".ogg": "voice message", ".oga": "voice message",
        ".txt": "text file", ".json": "JSON file", ".py": "Python file",
        ".zip": "zip archive", ".tar": "archive", ".gz": "archive",
    }
    return types.get(ext, "file")


async def _handle_media_message(
    chat_id: int,
    file_id: str,
    caption: Optional[str],
    filename_hint: Optional[str] = None,
    media_type: str = "file",
) -> None:
    """Download a media attachment and route it (with optional caption) to Claude."""
    local_path = await _download_telegram_file(file_id, filename_hint)
    if not local_path:
        await send_message(chat_id, "Couldn't download that file. Try again?")
        return

    file_type = _mime_for_extension(local_path)

    # Build the message text that Claude will see.
    # Claude Code's Read tool can natively view images and read documents.
    parts: list[str] = []
    if caption:
        parts.append(caption)
    parts.append(f"[Attached {file_type}: {local_path}]")

    text = "\n".join(parts)
    await _handle_text_message(chat_id, text)


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

    # Run agent and accumulate response (no streaming for Telegram).
    # Use send_message_with_tools() — same path the WebSocket handler uses —
    # so tool-calling agents return text reliably.
    full_response = ""
    has_error = False
    try:
        async for event in claude.send_message_with_tools(effective_message):
            if event["type"] == "text":
                full_response += event["content"]
            elif event["type"] == "error":
                log.error("telegram.claude_error", session_id=session_id, content=event["content"])
                has_error = True
    except Exception:
        log.exception("telegram.claude_stream_failed", session_id=session_id)
        await send_message(chat_id, "Something went wrong. Try again?")
        return

    if full_response.strip():
        await save_message(session_id, "assistant", full_response)
        await send_message(chat_id, full_response)
    elif has_error:
        await send_message(chat_id, "Something went wrong. Try again?")
    else:
        log.warning("telegram.empty_response", session_id=session_id)

    await update_session_activity(session_id)
    touch_session(session_id, chat_id)


# ---------------------------------------------------------------------------
# Webhook: process incoming updates from Telegram
# ---------------------------------------------------------------------------

async def process_update(update: dict) -> None:
    """Process a raw Telegram update dict (text, photos, documents, voice, video)."""
    message = update.get("message")
    if not message:
        return

    chat = message.get("chat", {})
    chat_id = chat.get("id")
    user = message.get("from", {})
    user_id = user.get("id")
    username = user.get("username")

    if not chat_id:
        return

    text = message.get("text", "").strip()
    caption = (message.get("caption") or "").strip()

    # /start command (always text)
    if text.startswith("/start"):
        await _handle_start(chat_id, user_id, username)
        return

    # Photo — Telegram sends an array of sizes; pick the largest
    photos = message.get("photo")
    if photos:
        best = max(photos, key=lambda p: p.get("file_size", 0))
        await _handle_media_message(
            chat_id, best["file_id"], caption or None,
            filename_hint="photo.jpg", media_type="photo",
        )
        return

    # Document (PDF, zip, etc.)
    document = message.get("document")
    if document:
        await _handle_media_message(
            chat_id, document["file_id"], caption or None,
            filename_hint=document.get("file_name"),
            media_type="document",
        )
        return

    # Voice message
    voice = message.get("voice")
    if voice:
        await _handle_media_message(
            chat_id, voice["file_id"], caption or None,
            filename_hint="voice.ogg", media_type="voice",
        )
        return

    # Video
    video = message.get("video")
    if video:
        await _handle_media_message(
            chat_id, video["file_id"], caption or None,
            filename_hint=video.get("file_name", "video.mp4"),
            media_type="video",
        )
        return

    # Video note (round video messages)
    video_note = message.get("video_note")
    if video_note:
        await _handle_media_message(
            chat_id, video_note["file_id"], caption or None,
            filename_hint="video_note.mp4", media_type="video",
        )
        return

    # Sticker
    sticker = message.get("sticker")
    if sticker:
        emoji = sticker.get("emoji", "")
        sticker_text = f"[Sticker: {emoji}]" if emoji else "[Sticker]"
        await _handle_text_message(chat_id, sticker_text)
        return

    # Plain text message
    if text:
        await _handle_text_message(chat_id, text)


# ---------------------------------------------------------------------------
# Long-polling
# ---------------------------------------------------------------------------

async def _poll_updates() -> None:
    """Long-poll Telegram's getUpdates endpoint."""
    offset = 0
    log.info("telegram.polling_started")

    # Clear any stale webhook so polling works
    async with httpx.AsyncClient(timeout=15.0) as client:
        try:
            await client.post(f"{_api_base()}/deleteWebhook")
        except Exception:
            pass

    while True:
        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                resp = await client.get(
                    f"{_api_base()}/getUpdates",
                    params={
                        "offset": offset,
                        "timeout": 30,
                        "allowed_updates": json.dumps(["message"]),
                    },
                )
                data = resp.json()
                if not data.get("ok"):
                    log.error("telegram.poll_error", response=data)
                    await asyncio.sleep(5)
                    continue

                for update in data.get("result", []):
                    offset = update["update_id"] + 1
                    asyncio.ensure_future(process_update(update))

        except asyncio.CancelledError:
            log.info("telegram.polling_stopped")
            return
        except Exception:
            log.exception("telegram.poll_exception")
            await asyncio.sleep(5)


def start_polling() -> None:
    """Start the background polling task."""
    global _poll_task
    if not settings.telegram_bot_token:
        return
    if _poll_task and not _poll_task.done():
        return
    _poll_task = asyncio.create_task(_poll_updates())


async def stop_polling() -> None:
    """Cancel the background polling task."""
    global _poll_task
    if _poll_task and not _poll_task.done():
        _poll_task.cancel()
        try:
            await _poll_task
        except asyncio.CancelledError:
            pass
    _poll_task = None


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
