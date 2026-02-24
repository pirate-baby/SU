"""
FastAPI application with Claude chat functionality and life management.
"""
import asyncio
import json
import uuid
from datetime import datetime
from typing import Any

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
    session_exists,
    save_message,
    update_session_activity,
    end_session,
)
from app.claude_client import ClaudeChat
from app.memory_manager import on_user_message, on_session_end
from app.models import SessionCreateResponse
from app.website_agent import subagent_event_queue
from app.agent_registry import (
    get_or_create_agent,
    get_lock,
    touch,
    release_agent,
    cleanup_idle_agents,
)
from app.scheduler import scheduler

log = get_logger(__name__)

# ---------------------------------------------------------------------------
# Active WebSocket connections (for interjection push delivery)
# ---------------------------------------------------------------------------
_active_connections: dict[str, WebSocket] = {}


async def push_interjection_to_clients(interjection: dict[str, Any]) -> None:
    """Push an interjection to all connected WebSocket clients."""
    dead: list[str] = []
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
        except Exception:
            dead.append(sid)
    for sid in dead:
        _active_connections.pop(sid, None)


async def deliver_pending_interjections(websocket: WebSocket) -> None:
    """Deliver any pending interjections when a client connects."""
    async with get_db() as db:
        cursor = await db.execute(
            "SELECT * FROM interjections WHERE status = 'pending' ORDER BY created_at ASC"
        )
        rows = await cursor.fetchall()
        now = datetime.utcnow().isoformat()
        for row in rows:
            item = dict(row)
            try:
                await websocket.send_json({
                    "type": "interjection",
                    "id": item["id"],
                    "content": item["content"],
                    "urgency": item.get("urgency", "normal"),
                    "source": item.get("source"),
                    "created_at": item.get("created_at"),
                })
                await db.execute(
                    "UPDATE interjections SET status = 'delivered', delivered_at = ? WHERE id = ?",
                    (now, item["id"]),
                )
            except Exception:
                break
        await db.commit()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize database, log writer, and scheduler on startup."""
    await init_database()
    await start_log_writer()
    cleanup_task = asyncio.create_task(cleanup_idle_agents())
    await scheduler.start(push_interjection_to_clients)
    log.info("app.startup", version="3.0.0")
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
    await release_agent(session_id)
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
    clauses: list[str] = []
    params: list[Any] = []
    if status:
        clauses.append("status = ?")
        params.append(status)
    if category:
        clauses.append("category = ?")
        params.append(category)
    if due_before:
        clauses.append("due_date <= ?")
        params.append(due_before)
    if due_after:
        clauses.append("due_date >= ?")
        params.append(due_after)
    if priority is not None:
        clauses.append("priority = ?")
        params.append(priority)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    params.append(limit)

    async with get_db() as db:
        cursor = await db.execute(
            f"SELECT * FROM tasks {where} ORDER BY priority ASC, due_date ASC LIMIT ?",
            params,
        )
        return [dict(row) for row in await cursor.fetchall()]


@app.post("/api/tasks", status_code=201)
async def api_create_task(body: TaskCreate):
    task_id = str(uuid.uuid4())
    now = datetime.utcnow().isoformat()
    async with get_db() as db:
        await db.execute(
            """INSERT INTO tasks
               (id, title, description, priority, category, due_date, due_time,
                created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (task_id, body.title, body.description, body.priority,
             body.category, body.due_date, body.due_time, now, now),
        )
        await db.commit()
    return {"id": task_id, "title": body.title, "status": "pending"}


@app.put("/api/tasks/{task_id}")
async def api_update_task(task_id: str, body: TaskUpdate):
    sets: list[str] = []
    vals: list[Any] = []
    for field in ["title", "description", "status", "priority", "category",
                  "due_date", "due_time"]:
        val = getattr(body, field, None)
        if val is not None:
            sets.append(f"{field} = ?")
            vals.append(val)
    if not sets:
        raise HTTPException(400, "No fields to update")
    if body.status == "done":
        sets.append("completed_at = ?")
        vals.append(datetime.utcnow().isoformat())
    sets.append("updated_at = ?")
    vals.append(datetime.utcnow().isoformat())
    vals.append(task_id)
    async with get_db() as db:
        await db.execute(f"UPDATE tasks SET {', '.join(sets)} WHERE id = ?", vals)
        await db.commit()
    return {"updated": task_id}


@app.delete("/api/tasks/{task_id}")
async def api_delete_task(task_id: str):
    async with get_db() as db:
        await db.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
        await db.commit()
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
    clauses: list[str] = []
    params: list[Any] = []
    if start_after:
        clauses.append("start_time >= ?")
        params.append(start_after)
    if start_before:
        clauses.append("start_time <= ?")
        params.append(start_before)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    params.append(limit)
    async with get_db() as db:
        cursor = await db.execute(
            f"SELECT * FROM events {where} ORDER BY start_time ASC LIMIT ?",
            params,
        )
        return [dict(row) for row in await cursor.fetchall()]


@app.post("/api/events", status_code=201)
async def api_create_event(body: EventCreate):
    event_id = str(uuid.uuid4())
    now = datetime.utcnow().isoformat()
    async with get_db() as db:
        await db.execute(
            """INSERT INTO events
               (id, title, description, start_time, end_time, all_day,
                location, reminder_minutes, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (event_id, body.title, body.description, body.start_time,
             body.end_time, 1 if body.all_day else 0, body.location,
             body.reminder_minutes, now, now),
        )
        await db.commit()
    return {"id": event_id, "title": body.title}


@app.put("/api/events/{event_id}")
async def api_update_event(event_id: str, body: EventUpdate):
    sets: list[str] = []
    vals: list[Any] = []
    for field in ["title", "description", "start_time", "end_time",
                  "all_day", "location", "reminder_minutes"]:
        val = getattr(body, field, None)
        if val is not None:
            if field == "all_day":
                val = 1 if val else 0
            sets.append(f"{field} = ?")
            vals.append(val)
    if not sets:
        raise HTTPException(400, "No fields to update")
    sets.append("updated_at = ?")
    vals.append(datetime.utcnow().isoformat())
    vals.append(event_id)
    async with get_db() as db:
        await db.execute(f"UPDATE events SET {', '.join(sets)} WHERE id = ?", vals)
        await db.commit()
    return {"updated": event_id}


@app.delete("/api/events/{event_id}")
async def api_delete_event(event_id: str):
    async with get_db() as db:
        await db.execute("DELETE FROM events WHERE id = ?", (event_id,))
        await db.commit()
    return {"deleted": event_id}


# -- Interjections REST API --

@app.get("/api/interjections")
async def api_list_interjections(status: str = "pending", limit: int = 20):
    async with get_db() as db:
        cursor = await db.execute(
            "SELECT * FROM interjections WHERE status = ? ORDER BY created_at DESC LIMIT ?",
            (status, limit),
        )
        return [dict(row) for row in await cursor.fetchall()]


@app.post("/api/interjections/{interjection_id}/dismiss")
async def api_dismiss_interjection(interjection_id: str):
    async with get_db() as db:
        await db.execute(
            "UPDATE interjections SET status = 'dismissed' WHERE id = ?",
            (interjection_id,),
        )
        await db.commit()
    return {"dismissed": interjection_id}


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
    await deliver_pending_interjections(websocket)

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
