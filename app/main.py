"""
FastAPI application with Claude chat functionality.
"""
import asyncio
import json
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from contextlib import asynccontextmanager
import httpx

from app.config import settings
from app.database import init_database
from app.logger import get_logger, start_log_writer, stop_log_writer
from app.session_manager import (
    create_session,
    get_session,
    get_all_sessions,
    session_exists,
    save_message,
    update_session_activity,
    end_session,
)
from app.claude_client import ClaudeChat
from app.memory_manager import on_user_message, on_session_end
from app.models import SessionCreateResponse
from app.website_agent import subagent_event_queue

log = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize database and log writer on startup."""
    await init_database()
    await start_log_writer()
    log.info("app.startup", version="2.0.0")
    yield
    log.info("app.shutdown")
    await stop_log_writer()


app = FastAPI(
    title="Claude Chat Service",
    version="2.0.0",
    lifespan=lifespan
)

app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")


@app.get("/", response_class=HTMLResponse)
async def landing_page(request: Request):
    """Serve landing page."""
    return templates.TemplateResponse("index.html", {"request": request})


@app.post("/api/sessions/new", response_model=SessionCreateResponse)
async def create_new_session():
    """Create a new chat session."""
    session_id = await create_session()
    log.info("session.created", session_id=session_id)
    return SessionCreateResponse(
        session_id=session_id,
        redirect_url=f"/chat/{session_id}"
    )


@app.get("/chat/{session_id}", response_class=HTMLResponse)
async def chat_page(request: Request, session_id: str):
    """Serve chat page for a session."""
    if not await session_exists(session_id):
        raise HTTPException(status_code=404, detail="Session not found")

    return templates.TemplateResponse(
        "chat.html",
        {"request": request, "session_id": session_id}
    )


@app.post("/api/sessions/{session_id}/end")
async def end_chat_session(session_id: str):
    """End a chat session."""
    if not await session_exists(session_id):
        raise HTTPException(status_code=404, detail="Session not found")

    await end_session(session_id)
    log.info("session.ended", session_id=session_id)
    asyncio.ensure_future(on_session_end(session_id))
    return {"status": "ended"}


# ---- Sessions list & log viewer API routes ----

@app.get("/sessions", response_class=HTMLResponse)
async def sessions_page(request: Request):
    """Serve the session browser / log viewer page."""
    return templates.TemplateResponse("sessions.html", {"request": request})


@app.get("/api/sessions")
async def list_sessions():
    """Return all sessions (most recent first)."""
    sessions = await get_all_sessions()
    return [
        {
            "id": s.id,
            "created_at": s.created_at.isoformat() if s.created_at else None,
            "last_activity": s.last_activity.isoformat() if s.last_activity else None,
            "status": s.status,
            "message_count": len(s.messages) if s.messages else 0,
        }
        for s in sessions
    ]


@app.get("/api/sessions/{session_id}/messages")
async def get_session_messages(session_id: str):
    """Return all messages for a session."""
    session = await get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    return [
        {
            "id": m.id,
            "role": m.role,
            "content": m.content,
            "created_at": m.created_at.isoformat() if m.created_at else None,
        }
        for m in (session.messages or [])
    ]


@app.get("/api/logs")
async def get_logs(
    session_id: str | None = None,
    level: str | None = None,
    since: str | None = None,
    until: str | None = None,
    limit: int = 500,
):
    """Query persisted log entries with optional filters."""
    from app.database import get_db

    clauses: list[str] = []
    params: list[str | int] = []

    if session_id:
        clauses.append("session_id = ?")
        params.append(session_id)
    if level:
        clauses.append("level = ?")
        params.append(level.lower())
    if since:
        clauses.append("timestamp >= ?")
        params.append(since)
    if until:
        clauses.append("timestamp <= ?")
        params.append(until)

    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    query = f"SELECT * FROM logs {where} ORDER BY timestamp ASC LIMIT ?"
    params.append(limit)

    async with get_db() as db:
        cursor = await db.execute(query, params)
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]


# ---- Voice mode API routes ----

@app.get("/api/voice/config")
async def get_voice_config():
    """Return voice mode configuration."""
    return {"enabled": bool(settings.elevenlabs_api_key and settings.elevenlabs_voice_id)}


@app.get("/api/voice/token/{token_type}")
async def get_voice_token(token_type: str):
    """Mint a single-use token for client-side ElevenLabs access."""
    if not settings.elevenlabs_api_key:
        raise HTTPException(status_code=503, detail="Voice mode not configured")
    if token_type not in ("stt", "tts"):
        raise HTTPException(status_code=400, detail="Invalid token type")

    el_token_type = "realtime_scribe" if token_type == "stt" else "tts_websocket"
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"https://api.elevenlabs.io/v1/single-use-token/{el_token_type}",
            headers={"xi-api-key": settings.elevenlabs_api_key},
        )
        resp.raise_for_status()
        log.info("voice.token_minted", token_type=token_type)
        return resp.json()


# ---- WebSocket chat ----

async def send_message_history(websocket: WebSocket, session_id: str):
    session = await get_session(session_id)
    if session and session.messages:
        await websocket.send_json({
            "type": "history",
            "messages": [
                {"role": msg.role, "content": msg.content}
                for msg in session.messages
                if msg.role in ("user", "assistant")
            ]
        })


async def _drain_subagent_events(websocket: WebSocket, stop: asyncio.Event):
    """Forward subagent progress events to the websocket until stop is set."""
    log.debug("drain.started")
    while not stop.is_set():
        try:
            event = await asyncio.wait_for(subagent_event_queue.get(), timeout=0.25)
            log.debug("drain.event", event_type=event.get("type"))
            await websocket.send_json({
                "type": "subagent_event",
                "subtype": event.get("type"),
                "data": event,
            })
        except asyncio.TimeoutError:
            continue
        except Exception:
            log.exception("drain.error")
            break
    log.debug("drain.stopped")


async def _forward_tts_audio(tts, websocket: WebSocket, session_id: str):
    """Read audio chunks from TTS and forward over the app WebSocket."""
    chunks_forwarded = 0
    try:
        async for audio_b64 in tts.audio_chunks():
            await websocket.send_json({
                "type": "audio_chunk",
                "audio": audio_b64,
            })
            chunks_forwarded += 1
    except Exception:
        log.exception("tts.forward_error", session_id=session_id, chunks_forwarded=chunks_forwarded)
    log.info("tts.forward_done", session_id=session_id, chunks_forwarded=chunks_forwarded)
    await websocket.send_json({"type": "audio_end"})


async def stream_claude_response(websocket: WebSocket, session_id: str, user_message: str, claude: ClaudeChat, voice_mode: bool = False):
    await websocket.send_json({"type": "assistant_start"})

    # Drain any stale events from a previous call
    while not subagent_event_queue.empty():
        try:
            subagent_event_queue.get_nowait()
        except asyncio.QueueEmpty:
            break

    stop_drain = asyncio.Event()
    drain_task = asyncio.create_task(_drain_subagent_events(websocket, stop_drain))

    # Set up TTS if voice mode is active
    tts = None
    tts_forwarder = None
    if voice_mode and settings.elevenlabs_api_key:
        from app.elevenlabs_tts import ElevenLabsTTS
        tts = ElevenLabsTTS()
        try:
            await tts.connect()
            tts_forwarder = asyncio.create_task(_forward_tts_audio(tts, websocket, session_id))
        except Exception:
            log.exception("tts.connect_failed", session_id=session_id)
            tts = None

    full_response = ""
    try:
        async for event in claude.send_message(user_message):
            event_type = event["type"]

            if event_type == "text":
                full_response += event["content"]
                await websocket.send_json({
                    "type": "assistant_chunk",
                    "content": event["content"]
                })
                if tts:
                    await tts.send_text(event["content"])
            elif event_type == "tool_use":
                log.info(
                    "chat.tool_use",
                    session_id=session_id,
                    tool_name=event["name"],
                    tool_id=event["id"],
                )
                await websocket.send_json({
                    "type": "tool_use",
                    "id": event["id"],
                    "name": event["name"],
                    "input": event["input"],
                })
            elif event_type == "tool_result":
                log.info(
                    "chat.tool_result",
                    session_id=session_id,
                    tool_use_id=event["tool_use_id"],
                    is_error=event["is_error"],
                )
                await websocket.send_json({
                    "type": "tool_result",
                    "tool_use_id": event["tool_use_id"],
                    "content": event["content"],
                    "is_error": event["is_error"],
                })
            elif event_type == "error":
                log.error("chat.stream_error", session_id=session_id, content=event["content"])
                await websocket.send_json({
                    "type": "error",
                    "content": event["content"]
                })
    finally:
        stop_drain.set()
        await drain_task

    # Finalize TTS
    if tts:
        try:
            await tts.flush()
            await tts.close()
        except Exception:
            log.exception("tts.close_error", session_id=session_id)
        if tts_forwarder:
            await tts_forwarder

    await save_message(session_id, "assistant", full_response)
    log.info("chat.assistant_response", session_id=session_id, length=len(full_response))
    await websocket.send_json({"type": "assistant_end"})


async def _inject_pending_memories(session_id: str, claude: ClaudeChat) -> None:
    """Feed any pending memory thoughts into the SDK client's internal history."""
    session = await get_session(session_id)
    if not session or not session.messages:
        return

    pending = [m for m in session.messages if m.role == "memory"]
    if not pending:
        return

    thoughts = "\n\n".join(m.content for m in pending)
    context_msg = f"<context>\n{thoughts}\n</context>"

    log.info("memory.injecting", session_id=session_id, count=len(pending))

    async for _ in claude.send_message(context_msg):
        pass

    from app.session_manager import mark_memories_consumed
    for m in pending:
        if m.id is not None:
            await mark_memories_consumed(m.id)

    log.info("memory.injected", session_id=session_id, count=len(pending))


async def handle_user_message(websocket: WebSocket, session_id: str, user_message: str, claude: ClaudeChat, voice_mode: bool = False):
    await save_message(session_id, "user", user_message)
    log.info("chat.user_message", session_id=session_id, length=len(user_message), voice_mode=voice_mode)
    asyncio.ensure_future(on_user_message(session_id))
    await websocket.send_json({
        "type": "user_message",
        "content": user_message
    })

    try:
        await _inject_pending_memories(session_id, claude)
        await stream_claude_response(websocket, session_id, user_message, claude, voice_mode=voice_mode)
    except Exception as e:
        log.exception("chat.handle_error", session_id=session_id, error=str(e))
        await websocket.send_json({
            "type": "error",
            "content": f"Error generating response: {str(e)}"
        })

    await update_session_activity(session_id)


@app.websocket("/ws/chat/{session_id}")
async def websocket_chat(websocket: WebSocket, session_id: str):
    """WebSocket endpoint for chat."""
    log.info("ws.connect_attempt", session_id=session_id)

    try:
        await websocket.accept()
        log.info("ws.accepted", session_id=session_id)
    except Exception as e:
        log.error("ws.accept_failed", session_id=session_id, error=str(e))
        return

    if not await session_exists(session_id):
        log.warning("ws.session_not_found", session_id=session_id)
        await websocket.send_json({
            "type": "error",
            "content": "Session not found"
        })
        await websocket.close()
        return

    await send_message_history(websocket, session_id)
    await update_session_activity(session_id)

    try:
        if settings.claude_code_oauth_token:
            claude = ClaudeChat(oauth_token=settings.claude_code_oauth_token)
        else:
            claude = ClaudeChat()
        log.info("ws.claude_initialized", session_id=session_id)
    except Exception as e:
        log.exception("ws.claude_init_failed", session_id=session_id, error=str(e))
        await websocket.send_json({
            "type": "error",
            "content": f"Failed to initialize Claude client: {str(e)}"
        })
        await websocket.close()
        return

    try:
        async with claude:
            while True:
                data = await websocket.receive_text()
                message_data = json.loads(data)

                msg_type = message_data.get("type")
                if msg_type == "user_message":
                    user_message = message_data.get("content", "").strip()
                    if user_message:
                        await handle_user_message(websocket, session_id, user_message, claude)
                elif msg_type == "voice_message":
                    user_message = message_data.get("content", "").strip()
                    if user_message:
                        await handle_user_message(websocket, session_id, user_message, claude, voice_mode=True)

    except WebSocketDisconnect:
        log.info("ws.disconnected", session_id=session_id)
    except Exception as e:
        log.exception("ws.error", session_id=session_id, error=str(e))
        try:
            await websocket.send_json({
                "type": "error",
                "content": f"Connection error: {str(e)}"
            })
        except Exception:
            pass


@app.get("/health")
async def health_check():
    """Health check endpoint."""
    return {
        "status": "healthy",
        "service": "claude-chat-service",
        "version": "2.0.0"
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
