"""
FastAPI application with Claude chat functionality and life management.
"""
import asyncio
import json
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from contextlib import asynccontextmanager
import httpx
from pydantic import BaseModel

from app.config import settings
from app.database import init_database, get_db
from app.logger import get_logger, start_log_writer, stop_log_writer
from app.session_manager import (
    create_session,
    get_session,
    get_all_sessions,
    get_active_session_ids,
    session_exists,
    save_message,
    update_session_activity,
    end_session,
)
from app.claude_client import ClaudeChat
from app.memory_manager import on_first_message, on_user_message, on_session_end
from app.models import SessionCreateResponse
from app.agent_registry import (
    get_or_create_agent,
    get_lock,
    touch,
    release_agent,
    cleanup_idle_agents,
    mark_ws_connected,
    mark_ws_disconnected,
)
from app.scheduler import scheduler
from app.repositories import TaskRepo, EventRepo, InterjectionRepo, PushSubscriptionRepo, SuNoteRepo

log = get_logger(__name__)

# ---------------------------------------------------------------------------
# Active WebSocket connections (for interjection push delivery)
# ---------------------------------------------------------------------------
_active_connections: dict[str, WebSocket] = {}


async def push_interjection_to_clients(interjection: dict[str, Any]) -> int:
    """Push an interjection to all connected WebSocket clients.

    Returns the number of clients that received the message.
    If zero, the caller should fall back to Web Push.
    """
    dead: list[str] = []
    delivered = 0
    for sid, ws in _active_connections.items():
        try:
            await ws.send_json({
                "type": "interjection",
                "id": interjection["id"],
                "content": interjection["content"],
                "urgency": interjection.get("urgency", "normal"),
                "source": interjection.get("source"),
                "created_at": interjection.get("created_at"),
            })
            delivered += 1
        except Exception:
            dead.append(sid)
    for sid in dead:
        _active_connections.pop(sid, None)
    return delivered


async def deliver_pending_interjections(websocket: WebSocket) -> None:
    """Deliver any pending interjections when a client connects."""
    items = await InterjectionRepo.pending()
    for item in items:
        try:
            await websocket.send_json({
                "type": "interjection",
                "id": item["id"],
                "content": item["content"],
                "urgency": item.get("urgency", "normal"),
                "source": item.get("source"),
                "created_at": item.get("created_at"),
            })
            await InterjectionRepo.mark_delivered(item["id"])
        except Exception:
            break


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize database, log writer, and scheduler on startup."""
    await init_database()
    await start_log_writer()
    cleanup_task = asyncio.create_task(cleanup_idle_agents())
    await scheduler.start(push_interjection_to_clients)
    log.info("app.startup", version="3.0.0")

    # Trigger REM for any sessions that were still active at shutdown
    # (e.g. tab closed, process killed) so their memories are not lost.
    abandoned = await get_active_session_ids()
    if abandoned:
        log.info("app.startup_rem_sweep", count=len(abandoned))
        for sid in abandoned:
            await end_session(sid)
            asyncio.ensure_future(on_session_end(sid))

    yield
    log.info("app.shutdown")
    await scheduler.stop()
    cleanup_task.cancel()
    await stop_log_writer()


app = FastAPI(
    title="SU — Personal Assistant",
    version="3.0.0",
    lifespan=lifespan
)

app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")

# Cache-busting version for static assets — updated on each deploy
import subprocess as _sp
_STATIC_VERSION = _sp.run(
    ["git", "rev-parse", "--short", "HEAD"],
    capture_output=True, text=True
).stdout.strip() or "1"


@app.get("/", response_class=HTMLResponse)
async def landing_page(request: Request):
    """Serve landing page."""
    return templates.TemplateResponse("index.html", {"request": request})


class SessionNewBody(BaseModel):
    interjection_id: str | None = None
    initial_context: str | None = None


@app.post("/api/sessions/new", response_model=SessionCreateResponse)
async def create_new_session(body: SessionNewBody | None = None):
    """Create a new chat session, optionally pre-loaded with context."""
    session_id = await create_session()
    log.info("session.created", session_id=session_id)

    # Pre-load context from interjection + related SU notes
    if body and body.interjection_id:
        context = await _build_interjection_context(body.interjection_id)
        if context:
            await save_message(session_id, "memory", context)
        await InterjectionRepo.link_session(body.interjection_id, session_id)
        log.info("session.linked_interjection",
                 session_id=session_id, interjection_id=body.interjection_id)
    elif body and body.initial_context:
        await save_message(session_id, "memory", body.initial_context)

    return SessionCreateResponse(
        session_id=session_id,
        redirect_url=f"/chat/{session_id}"
    )


async def _build_interjection_context(interjection_id: str) -> str | None:
    """Build rich context string from an interjection and its related data."""
    interjection = await InterjectionRepo.get(interjection_id)
    if not interjection:
        return None

    parts: list[str] = []
    parts.append(f"Interjection: {interjection['content']}")
    parts.append(f"Source: {interjection.get('source', 'unknown')}")
    parts.append(f"Urgency: {interjection.get('urgency', 'normal')}")

    # Load related SU note for full context (attempt history, etc.)
    su_note_id = interjection.get("related_su_note_id")
    if su_note_id:
        note = await SuNoteRepo.get(su_note_id)
        if note:
            parts.append(f"\nSU's internal note: {note['content']}")
            parts.append(f"Note priority: {note.get('priority', 'normal')}")
            parts.append(f"Previous attempts: {note.get('attempts', 0)}")
            if note.get("context_json"):
                try:
                    ctx = json.loads(note["context_json"])
                    parts.append(f"Context: {json.dumps(ctx, indent=2)}")
                except (json.JSONDecodeError, TypeError):
                    parts.append(f"Context: {note['context_json']}")

    # Load related task
    task_id = interjection.get("related_task_id")
    if task_id:
        tasks = await TaskRepo.list(limit=1)
        for t in tasks:
            if t.get("id") == task_id:
                parts.append(
                    f"\nRelated task: \"{t['title']}\" "
                    f"(status={t.get('status')}, priority={t.get('priority')}, "
                    f"due={t.get('due_date', 'none')})"
                )
                break

    parts.append(f"\nThe user clicked into this notification to engage with it.")
    return "\n".join(parts)


from fastapi.responses import RedirectResponse


@app.get("/api/sessions/from-interjection/{interjection_id}")
async def create_session_from_interjection(interjection_id: str):
    """Create a context-rich session from an interjection and redirect to chat."""
    interjection = await InterjectionRepo.get(interjection_id)
    if not interjection:
        raise HTTPException(status_code=404, detail="Interjection not found")

    session_id = await create_session()
    context = await _build_interjection_context(interjection_id)
    if context:
        await save_message(session_id, "memory", context)
    await InterjectionRepo.link_session(interjection_id, session_id)

    log.info("session.from_interjection",
             session_id=session_id, interjection_id=interjection_id)
    return RedirectResponse(url=f"/chat/{session_id}", status_code=303)


@app.get("/chat/{session_id}", response_class=HTMLResponse)
async def chat_page(request: Request, session_id: str):
    """Serve chat page for a session."""
    if not await session_exists(session_id):
        raise HTTPException(status_code=404, detail="Session not found")

    return templates.TemplateResponse(
        "chat.html",
        {"request": request, "session_id": session_id, "static_v": _STATIC_VERSION}
    )


@app.post("/api/sessions/end-all")
async def end_all_active_sessions():
    """End all active sessions at once."""
    active_ids = await get_active_session_ids()
    ended: list[str] = []
    for sid in active_ids:
        await end_session(sid)
        await release_agent(sid)
        asyncio.ensure_future(on_session_end(sid))
        ended.append(sid)
    log.info("sessions.ended_all", count=len(ended))
    return {"status": "ended", "count": len(ended), "session_ids": ended}


@app.post("/api/sessions/{session_id}/end")
async def end_chat_session(session_id: str, skip_rem: bool = False):
    """End a chat session. Pass skip_rem=true to suppress REM memory consolidation."""
    if not await session_exists(session_id):
        raise HTTPException(status_code=404, detail="Session not found")

    await end_session(session_id)
    await release_agent(session_id)
    log.info("session.ended", session_id=session_id, skip_rem=skip_rem)
    if not skip_rem:
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


# ---- Planner page & Life management API ----

@app.get("/planner", response_class=HTMLResponse)
async def planner_page(request: Request):
    """Serve the planner / calendar / task view."""
    return templates.TemplateResponse("planner.html", {"request": request})


# -- Tasks REST API --

class TaskCreate(BaseModel):
    title: str
    description: str | None = None
    priority: int = 3
    category: str | None = None
    due_date: str | None = None
    due_time: str | None = None

class TaskUpdate(BaseModel):
    title: str | None = None
    description: str | None = None
    status: str | None = None
    priority: int | None = None
    category: str | None = None
    due_date: str | None = None
    due_time: str | None = None


@app.get("/api/tasks")
async def api_list_tasks(
    status: str | None = None,
    category: str | None = None,
    due_before: str | None = None,
    due_after: str | None = None,
    priority: int | None = None,
    limit: int = 50,
):
    return await TaskRepo.list(
        status=status, category=category, due_before=due_before,
        due_after=due_after, priority=priority, limit=limit,
    )


@app.post("/api/tasks", status_code=201)
async def api_create_task(body: TaskCreate):
    task = await TaskRepo.create(
        title=body.title, description=body.description,
        priority=body.priority, category=body.category,
        due_date=body.due_date, due_time=body.due_time,
    )
    return {"id": task.id, "title": task.title, "status": "pending"}


@app.put("/api/tasks/{task_id}")
async def api_update_task(task_id: str, body: TaskUpdate):
    fields = {k: v for k, v in body.model_dump().items() if v is not None}
    if not fields:
        raise HTTPException(400, "No fields to update")
    await TaskRepo.update(task_id, **fields)
    return {"updated": task_id}


@app.delete("/api/tasks/{task_id}")
async def api_delete_task(task_id: str):
    await TaskRepo.delete(task_id)
    return {"deleted": task_id}


# -- Events REST API --

class EventCreate(BaseModel):
    title: str
    start_time: str
    end_time: str | None = None
    description: str | None = None
    all_day: bool = False
    location: str | None = None
    reminder_minutes: int = 30

class EventUpdate(BaseModel):
    title: str | None = None
    description: str | None = None
    start_time: str | None = None
    end_time: str | None = None
    all_day: bool | None = None
    location: str | None = None
    reminder_minutes: int | None = None


@app.get("/api/events")
async def api_list_events(
    start_after: str | None = None,
    start_before: str | None = None,
    limit: int = 50,
):
    return await EventRepo.list(
        start_after=start_after, start_before=start_before, limit=limit,
    )


@app.post("/api/events", status_code=201)
async def api_create_event(body: EventCreate):
    event = await EventRepo.create(
        title=body.title, start_time=body.start_time,
        end_time=body.end_time, description=body.description,
        all_day=body.all_day, location=body.location,
        reminder_minutes=body.reminder_minutes,
    )
    return {"id": event.id, "title": event.title}


@app.put("/api/events/{event_id}")
async def api_update_event(event_id: str, body: EventUpdate):
    fields = {k: v for k, v in body.model_dump().items() if v is not None}
    if not fields:
        raise HTTPException(400, "No fields to update")
    await EventRepo.update(event_id, **fields)
    return {"updated": event_id}


@app.delete("/api/events/{event_id}")
async def api_delete_event(event_id: str):
    await EventRepo.delete(event_id)
    return {"deleted": event_id}


# -- Interjections REST API --

@app.get("/api/interjections")
async def api_list_interjections(status: str = "pending", limit: int = 20):
    return await InterjectionRepo.list(status=status, limit=limit)


@app.post("/api/interjections/{interjection_id}/dismiss")
async def api_dismiss_interjection(interjection_id: str):
    # Dismiss the interjection
    await InterjectionRepo.dismiss(interjection_id)

    # If this interjection has a related SU note, record the dismissal and snooze
    interjection = await InterjectionRepo.get(interjection_id)
    if interjection and interjection.get("related_su_note_id"):
        su_note_id = interjection["related_su_note_id"]
        note = await SuNoteRepo.get(su_note_id)
        if note and note.get("status") == "active":
            # Update context with dismissal record
            from datetime import datetime, timedelta
            now = datetime.utcnow()
            ctx: dict = {}
            if note.get("context_json"):
                try:
                    ctx = json.loads(note["context_json"])
                except (json.JSONDecodeError, TypeError):
                    ctx = {}
            attempts_log = ctx.get("attempts_log", [])
            attempts_log.append({
                "date": now.isoformat(),
                "result": "dismissed",
                "interjection_id": interjection_id,
            })
            ctx["attempts_log"] = attempts_log

            # Snooze: push activate_after forward based on attempts
            attempts = note.get("attempts", 0)
            # Escalating snooze: 1 day, 2 days, then 1 day (getting more urgent)
            snooze_hours = min(48, max(24, 24 * (attempts + 1)))
            new_activate = (now + timedelta(hours=snooze_hours)).isoformat()

            await SuNoteRepo.update(
                su_note_id,
                context_json=json.dumps(ctx),
                activate_after=new_activate,
            )
            await SuNoteRepo.increment_attempts(su_note_id)
            log.info("interjection.dismiss_snoozed_note",
                     interjection_id=interjection_id, su_note_id=su_note_id,
                     snooze_until=new_activate)

    return {"dismissed": interjection_id}


# -- Web Push subscription API --

@app.get("/api/push/vapid-key")
async def api_vapid_public_key():
    """Return the VAPID public key so the browser can subscribe."""
    return {"public_key": settings.vapid_public_key or ""}


@app.post("/api/push/subscribe")
async def api_push_subscribe(request: Request):
    """Store a browser push subscription."""
    body = await request.json()
    endpoint = body.get("endpoint", "")
    if not endpoint:
        raise HTTPException(400, "Missing endpoint")
    sub_id = await PushSubscriptionRepo.upsert(
        endpoint=endpoint,
        subscription_json=json.dumps(body),
    )
    log.info("push.subscribed", sub_id=sub_id)
    return {"id": sub_id}


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


async def _ws_send(websocket: WebSocket, data: dict) -> bool:
    """Send JSON over websocket, returning False if the connection is closed."""
    try:
        await websocket.send_json(data)
        return True
    except Exception:
        return False


async def stream_claude_response(websocket: WebSocket, session_id: str, user_message: str, claude: ClaudeChat, voice_mode: bool = False):
    ws_live = await _ws_send(websocket, {"type": "assistant_start"})

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
    async for event in claude.send_message(user_message):
        event_type = event["type"]

        if event_type == "text":
            full_response += event["content"]
            if ws_live:
                ws_live = await _ws_send(websocket, {
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
            if ws_live:
                ws_live = await _ws_send(websocket, {
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
            if ws_live:
                ws_live = await _ws_send(websocket, {
                    "type": "tool_result",
                    "tool_use_id": event["tool_use_id"],
                    "content": event["content"],
                    "is_error": event["is_error"],
                })
        elif event_type == "error":
            log.error("chat.stream_error", session_id=session_id, content=event["content"])
            if ws_live:
                ws_live = await _ws_send(websocket, {
                    "type": "error",
                    "content": event["content"]
                })

    # Finalize TTS
    if tts:
        try:
            await tts.flush()
            await tts.close()
        except Exception:
            log.exception("tts.close_error", session_id=session_id)
        if tts_forwarder:
            await tts_forwarder

    # Always persist the response, even if the websocket disconnected mid-stream
    await save_message(session_id, "assistant", full_response)
    log.info("chat.assistant_response", session_id=session_id, length=len(full_response))
    if ws_live:
        await _ws_send(websocket, {"type": "assistant_end"})
    else:
        log.warning("chat.ws_disconnected_during_stream", session_id=session_id)


async def _collect_and_consume_memories(session_id: str) -> Optional[str]:
    """Collect pending memory thoughts, mark them consumed, and return as a context string.

    Returns a formatted <context>...</context> string to prepend to the user message,
    or None if there are no pending memories.
    """
    session = await get_session(session_id)
    if not session or not session.messages:
        return None

    pending = [m for m in session.messages if m.role == "memory"]
    if not pending:
        return None

    thoughts = "\n\n".join(m.content for m in pending)
    context_prefix = f"<context>\n{thoughts}\n</context>\n\n"

    log.info("memory.injecting", session_id=session_id, count=len(pending))

    from app.session_manager import mark_memories_consumed
    for m in pending:
        if m.id is not None:
            await mark_memories_consumed(m.id)

    log.info("memory.injected", session_id=session_id, count=len(pending))
    return context_prefix


async def handle_user_message(websocket: WebSocket, session_id: str, user_message: str, claude: ClaudeChat, voice_mode: bool = False):
    await save_message(session_id, "user", user_message)
    log.info("chat.user_message", session_id=session_id, length=len(user_message), voice_mode=voice_mode)
    await websocket.send_json({
        "type": "user_message",
        "content": user_message
    })

    # For the very first user message, run the subconscious immediately (await)
    # so any surfaced memories are ready before we start generating. For subsequent
    # messages, fire it in the background on the usual interval.
    session = await get_session(session_id)
    user_messages = [m for m in (session.messages if session else []) if m.role == "user"]
    if len(user_messages) == 1:
        await _ws_send(websocket, {"type": "status", "content": "...", "persist": True})
        await on_first_message(session_id)
        await _ws_send(websocket, {"type": "status", "content": ""})
    else:
        asyncio.ensure_future(on_user_message(session_id))

    try:
        context_prefix = await _collect_and_consume_memories(session_id)
        effective_message = (context_prefix + user_message) if context_prefix else user_message
        await stream_claude_response(websocket, session_id, effective_message, claude, voice_mode=voice_mode)
    except Exception as e:
        log.exception("chat.handle_error", session_id=session_id, error=str(e))
        await _ws_send(websocket, {
            "type": "error",
            "content": f"Error generating response: {str(e)}"
        })

    await update_session_activity(session_id)


@app.websocket("/ws/chat/{session_id}")
async def websocket_chat(websocket: WebSocket, session_id: str):
    """WebSocket endpoint for chat.

    The agent lives in the agent_registry and survives WebSocket disconnects.
    Each WS connection simply attaches to the existing (or newly-created) agent.
    """
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

    # Register for interjection push delivery
    _active_connections[session_id] = websocket
    mark_ws_connected(session_id)
    await deliver_pending_interjections(websocket)

    await websocket.send_json({"type": "status", "content": "Initializing..."})

    try:
        claude = await get_or_create_agent(session_id)
        log.info("ws.claude_initialized", session_id=session_id)
    except Exception as e:
        log.exception("ws.claude_init_failed", session_id=session_id, error=str(e))
        await websocket.send_json({
            "type": "error",
            "content": f"Failed to initialize Claude client: {str(e)}"
        })
        await websocket.close()
        _active_connections.pop(session_id, None)
        return

    await websocket.send_json({"type": "connection_ready"})

    try:
        while True:
            data = await websocket.receive_text()
            message_data = json.loads(data)

            msg_type = message_data.get("type")
            if msg_type == "user_message":
                user_message = message_data.get("content", "").strip()
                if user_message:
                    touch(session_id)
                    await handle_user_message(websocket, session_id, user_message, claude)
            elif msg_type == "voice_message":
                user_message = message_data.get("content", "").strip()
                if user_message:
                    touch(session_id)
                    await handle_user_message(websocket, session_id, user_message, claude, voice_mode=True)

    except WebSocketDisconnect:
        log.info("ws.disconnected", session_id=session_id)
        # Agent stays alive in the registry — will be reused on reconnect
    except Exception as e:
        log.exception("ws.error", session_id=session_id, error=str(e))
        try:
            await websocket.send_json({
                "type": "error",
                "content": f"Connection error: {str(e)}"
            })
        except Exception:
            pass
    finally:
        _active_connections.pop(session_id, None)
        mark_ws_disconnected(session_id)


@app.post("/api/admin/update")
async def admin_update():
    """Pull latest main, push to origin, then rebuild the container.

    Proxies to the host-side restart server (port 8932) which has access
    to git credentials and docker compose.
    """
    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            resp = await client.post("http://host.docker.internal:8932/update")
        except httpx.RequestError as exc:
            raise HTTPException(status_code=503, detail=f"Restart server unreachable: {exc}")
    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail=resp.text)
    log.info("admin.update_triggered")
    return {"status": "update initiated"}


@app.get("/health")
async def health_check():
    """Health check endpoint."""
    return {
        "status": "healthy",
        "service": "su-personal-assistant",
        "version": "3.0.0"
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
