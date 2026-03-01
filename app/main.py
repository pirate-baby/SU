"""
FastAPI application with Claude chat functionality and life management.
"""
import asyncio
import json
import random as _random
import uuid
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import HTMLResponse, JSONResponse
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
from app.daemon_registry import (
    daemon_registry, DaemonInfo, DaemonCategory,
    cleanup_stale_runs, get_last_completed_run, get_runs,
)
from app.process_limiter import get_slot_status
from app.repositories import TaskRepo, EventRepo, InterjectionRepo, SuNoteRepo
from app.deep_learning import (
    DocRepo as DLDocRepo,
    RunRepo as DLRunRepo,
    run_deep_learning,
    cancel_run as dl_cancel_run,
    set_broadcast_fn as dl_set_broadcast_fn,
    ensure_staging_dir,
)

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


async def push_incoming_call_to_clients(session_id: str, context: str) -> int:
    """Push an incoming call notification to all connected WebSocket clients.

    Returns the number of clients that received the message.
    """
    dead: list[str] = []
    delivered = 0
    for sid, ws_conn in _active_connections.items():
        try:
            await ws_conn.send_json({
                "type": "incoming_call",
                "session_id": session_id,
                "context": context,
            })
            delivered += 1
        except Exception:
            dead.append(sid)
    for sid in dead:
        _active_connections.pop(sid, None)
    return delivered


async def broadcast_deep_learning_progress(event: dict[str, Any]) -> None:
    """Push a deep learning progress event to all connected WebSocket clients."""
    dead: list[str] = []
    for sid, ws in _active_connections.items():
        try:
            await ws.send_json(event)
        except Exception:
            dead.append(sid)
    for sid in dead:
        _active_connections.pop(sid, None)


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
    await cleanup_stale_runs()
    await start_log_writer()

    # Register the log writer as a system daemon
    daemon_registry.register(DaemonInfo(
        name="log_writer",
        display_name="Log Writer",
        category=DaemonCategory.SYSTEM,
        description="Batches log entries into SQLite",
    ))

    cleanup_task = asyncio.create_task(cleanup_idle_agents())
    await scheduler.start(push_interjection_to_clients)
    dl_set_broadcast_fn(broadcast_deep_learning_progress)

    # Start Telegram long-polling
    if settings.telegram_bot_token:
        from app.telegram_bot import start_polling
        start_polling()

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
    if settings.telegram_bot_token:
        from app.telegram_bot import stop_polling
        await stop_polling()
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


@app.get("/call/{session_id}", response_class=HTMLResponse)
async def call_page(request: Request, session_id: str):
    """Serve the call landing page — auto-enters call mode.

    This is the destination for Telegram "Answer Call" deep links.
    """
    if not await session_exists(session_id):
        raise HTTPException(status_code=404, detail="Session not found")

    return templates.TemplateResponse(
        "call.html",
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


# ---- Daemon process index ----

@app.get("/daemons", response_class=HTMLResponse)
async def daemons_page(request: Request):
    """Serve the daemon process index page."""
    return templates.TemplateResponse("daemons.html", {"request": request})


@app.get("/deep-learning", response_class=HTMLResponse)
async def deep_learning_page(request: Request):
    """Serve the Deep Learning mode page."""
    return templates.TemplateResponse("deep-learning.html", {"request": request})


@app.get("/api/daemons")
async def api_daemon_index():
    """Return the daemon process index with current state."""
    from datetime import datetime, timedelta, timezone

    daemons = []
    for info in daemon_registry.list_daemons():
        current_runs = daemon_registry.get_current_runs(info.name)
        last_run = await get_last_completed_run(info.name)

        # Calculate next scheduled run
        next_run_at = None
        if info.interval_seconds and last_run:
            last_start = datetime.fromisoformat(last_run["started_at"])
            next_run_at = (last_start + timedelta(seconds=info.interval_seconds)).isoformat()

        daemons.append({
            "name": info.name,
            "display_name": info.display_name,
            "category": info.category.value,
            "interval_seconds": info.interval_seconds,
            "condition": info.condition,
            "description": info.description,
            "is_running": len(current_runs) > 0,
            "current_runs": [
                {"id": r.id, "started_at": r.started_at, "metadata": r.metadata}
                for r in current_runs
            ],
            "last_run": last_run,
            "next_scheduled_at": next_run_at,
        })

    return {
        "daemons": daemons,
        "process_limiter": get_slot_status(),
    }


@app.get("/api/daemons/{daemon_name}/runs")
async def api_daemon_runs(daemon_name: str, limit: int = 50, offset: int = 0):
    """Return run history for a daemon."""
    if not daemon_registry.get_daemon(daemon_name):
        raise HTTPException(404, f"Unknown daemon: {daemon_name}")
    return await get_runs(daemon_name, limit=limit, offset=offset)


@app.get("/api/daemons/{daemon_name}/logs")
async def api_daemon_logs(daemon_name: str, limit: int = 200, since: str | None = None):
    """Return logs for a specific daemon, filtered by event prefix."""
    prefix_map = {
        "calendar_check": "scheduler.calendar",
        "interjection_delivery": "scheduler.interjection",
        "note_processor": "scheduler.note_processor",
        "email_scanner": "scheduler.email_scanner",
        "daily_review": "scheduler.daily_review",
        "subconscious": "memory.subconscious",
        "rem": "memory.rem",
        "agent_cleanup": "registry.cleanup",
        "log_writer": "logger.",
        "deep_learning": "deep_learning.",
    }
    prefix = prefix_map.get(daemon_name)
    if not prefix:
        raise HTTPException(404, f"Unknown daemon: {daemon_name}")

    clauses = ["event LIKE ?"]
    params: list = [f"{prefix}%"]
    if since:
        clauses.append("timestamp >= ?")
        params.append(since)
    where = "WHERE " + " AND ".join(clauses)
    query = f"SELECT * FROM logs {where} ORDER BY timestamp DESC LIMIT ?"
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
            from datetime import timedelta
            from app.tz import now as local_now
            now = local_now()
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


# ---- Deep Learning API ----

@app.post("/api/deep-learning/upload")
async def api_deep_learning_upload(request: Request):
    """Accept file uploads for deep learning ingestion.

    Expects multipart/form-data with one or more 'files' fields.
    """
    try:
        form = await request.form()
        files = form.getlist("files")
        if not files:
            # Try singular 'file' field too
            single = form.get("file")
            if single:
                files = [single]
        if not files:
            return JSONResponse({"detail": "No files provided. Use 'files' or 'file' field."}, status_code=400)

        inbox = ensure_staging_dir()
        uploaded: list[dict] = []

        for upload in files:
            if not hasattr(upload, "filename"):
                continue
            filename = upload.filename or "unnamed"
            content = await upload.read()
            dest = inbox / f"{uuid.uuid4().hex[:8]}_{filename}"
            dest.write_bytes(content)

            doc_id = await DLDocRepo.create(
                filename=filename,
                file_path=str(dest),
                file_size=len(content),
            )
            uploaded.append({"id": doc_id, "filename": filename, "size": len(content)})
            log.info("deep_learning.file_uploaded", filename=filename, size=len(content))

        return {"uploaded": uploaded, "count": len(uploaded)}
    except Exception as exc:
        log.error("deep_learning.upload_failed", error=str(exc))
        return JSONResponse({"detail": f"Upload failed: {exc}"}, status_code=500)


class DeepLearningStartBody(BaseModel):
    audit_only: bool = False


@app.post("/api/deep-learning/start")
async def api_deep_learning_start(body: DeepLearningStartBody | None = None):
    """Start a deep learning run. Set audit_only=true to skip document ingestion."""
    audit_only = body.audit_only if body else False

    pending_docs = await DLDocRepo.list(status="pending")
    if not audit_only and not pending_docs:
        raise HTTPException(400, "No pending documents to ingest. Upload files first, "
                           "or use audit_only=true to just audit existing memory.")

    run_id = await DLRunRepo.create(total_documents=len(pending_docs))
    log.info("deep_learning.run_created", run_id=run_id, audit_only=audit_only,
             doc_count=len(pending_docs))

    asyncio.create_task(
        run_deep_learning(run_id, audit_only=audit_only),
        name=f"deep-learning-{run_id[:8]}",
    )

    return {"run_id": run_id, "status": "started", "audit_only": audit_only,
            "documents": len(pending_docs)}


@app.get("/api/deep-learning/runs")
async def api_deep_learning_runs():
    """List all deep learning runs."""
    return await DLRunRepo.list_all()


@app.get("/api/deep-learning/runs/{run_id}")
async def api_deep_learning_run(run_id: str):
    """Get detailed status of a deep learning run."""
    run = await DLRunRepo.get(run_id)
    if not run:
        raise HTTPException(404, "Run not found")
    return run


@app.post("/api/deep-learning/runs/{run_id}/cancel")
async def api_deep_learning_cancel(run_id: str):
    """Cancel a running deep learning session."""
    run = await DLRunRepo.get(run_id)
    if not run:
        raise HTTPException(404, "Run not found")
    if run["status"] != "running":
        raise HTTPException(400, f"Run is not running (status: {run['status']})")
    dl_cancel_run(run_id)
    return {"status": "cancel_requested", "run_id": run_id}


@app.get("/api/deep-learning/documents")
async def api_deep_learning_documents(status: str | None = None):
    """List staged deep learning documents."""
    return await DLDocRepo.list(status=status)


@app.get("/api/telegram/status")
async def api_telegram_status():
    """Return Telegram bot configuration status."""
    return {"configured": bool(settings.telegram_bot_token)}


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


VOICE_MODE_INSTRUCTION = (
    "<voice_mode>\n"
    "Your response will be spoken aloud via text-to-speech, not read on screen. "
    "Write plain conversational sentences. No markdown, no bullet lists, no "
    "code blocks, no asterisks, no headers, no special formatting. "
    "Avoid parenthetical asides. Keep it natural and spoken. "
    "Numbers: write them as words when short (e.g. 'three' not '3'). "
    "Don't use abbreviations like 'e.g.' — say 'for example'. "
    "Keep responses concise — a few sentences at most unless the user asked for detail.\n"
    "</voice_mode>\n\n"
)


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


def _voice_filler_phrases() -> list[str]:
    user = settings.user_name
    return [
        "Hmm, hang on, let me think.",
        f"Sure thing {user}, let me noodle on that for a sec.",
        "One moment, just pulling my thoughts together.",
        "Let me think on that.",
        "Give me a sec.",
        "On it, one moment.",
        f"Hang on {user}.",
    ]


async def _forward_filler_audio(tts, websocket: WebSocket, session_id: str):
    """Forward filler TTS audio. Sends audio_end with filler=true so client doesn't treat it as final."""
    try:
        async for audio_b64 in tts.audio_chunks():
            await websocket.send_json({
                "type": "audio_chunk",
                "audio": audio_b64,
            })
    except Exception:
        log.exception("tts.filler_forward_error", session_id=session_id)
    # Signal end of filler audio (not the real end)
    await websocket.send_json({"type": "audio_end", "filler": True})


async def _send_voice_filler(websocket: WebSocket, session_id: str):
    """Send a quick spoken filler phrase via TTS so there's no awkward silence."""
    phrase = _random.choice(_voice_filler_phrases())
    if not settings.elevenlabs_api_key:
        return
    try:
        from app.elevenlabs_tts import ElevenLabsTTS
        tts = ElevenLabsTTS()
        await tts.connect()
        forwarder = asyncio.create_task(_forward_filler_audio(tts, websocket, session_id))
        await tts.send_text(phrase)
        await tts.flush()
        await tts.close()
        await forwarder
    except Exception:
        log.exception("voice_filler.error", session_id=session_id)


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
        if voice_mode:
            # Send a spoken filler while memory search runs to avoid awkward silence
            filler_task = asyncio.create_task(_send_voice_filler(websocket, session_id))
        await _ws_send(websocket, {"type": "status", "content": "...", "persist": True})
        await on_first_message(session_id)
        if voice_mode:
            await filler_task
        await _ws_send(websocket, {"type": "status", "content": ""})
    else:
        asyncio.ensure_future(on_user_message(session_id))

    try:
        context_prefix = await _collect_and_consume_memories(session_id)
        effective_message = (context_prefix + user_message) if context_prefix else user_message
        if voice_mode:
            effective_message = VOICE_MODE_INSTRUCTION + effective_message
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
            elif msg_type == "call_action":
                action = message_data.get("action")
                log.info("ws.call_action", session_id=session_id, action=action)

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


# ---- Health & Monitoring dashboard ----

@app.get("/health/dashboard", response_class=HTMLResponse)
async def health_dashboard_page(request: Request):
    """Serve the health monitoring dashboard."""
    return templates.TemplateResponse("health.html", {"request": request})


@app.get("/api/health/detailed")
async def health_detailed():
    """Return comprehensive health metrics."""
    from app.health import collect_health_snapshot
    return await collect_health_snapshot()


@app.get("/api/health/history")
async def health_history(hours: int = 24, limit: int = 288):
    """Return historical health snapshots for trend charts."""
    from app.health import get_health_history
    return await get_health_history(hours=hours, limit=limit)


@app.post("/api/health/cleanup")
async def health_cleanup():
    """Manually trigger data retention cleanup."""
    from app.health import run_retention_cleanup
    result = await run_retention_cleanup()
    log.info("health.manual_cleanup", **result)
    return {"status": "completed", "deleted": result}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
