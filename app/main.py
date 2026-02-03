"""
FastAPI application with Claude chat functionality.
"""
import json
from pathlib import Path
from typing import Optional
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from contextlib import asynccontextmanager

from app.database import init_database
from app.session_manager import (
    create_session,
    get_session,
    session_exists,
    save_message,
    update_session_activity,
    end_session,
)
from app.claude_client import ClaudeChat
from app.models import SessionCreateResponse


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize database on startup."""
    await init_database()
    yield


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
    return {"status": "ended"}


async def send_message_history(websocket: WebSocket, session_id: str):
    session = await get_session(session_id)
    if session and session.messages:
        await websocket.send_json({
            "type": "history",
            "messages": [
                {"role": msg.role, "content": msg.content}
                for msg in session.messages
            ]
        })


async def stream_claude_response(websocket: WebSocket, session_id: str, user_message: str, claude: ClaudeChat):
    session = await get_session(session_id)
    conversation_history = claude.build_conversation_history(
        session.messages if session else []
    )

    await websocket.send_json({"type": "assistant_start"})

    full_response = ""
    async for chunk in claude.send_message(user_message, conversation_history):
        full_response += chunk
        await websocket.send_json({
            "type": "assistant_chunk",
            "content": chunk
        })

    await save_message(session_id, "assistant", full_response)
    await websocket.send_json({"type": "assistant_end"})


async def handle_user_message(websocket: WebSocket, session_id: str, user_message: str, claude: ClaudeChat):
    await save_message(session_id, "user", user_message)
    await websocket.send_json({
        "type": "user_message",
        "content": user_message
    })

    try:
        await stream_claude_response(websocket, session_id, user_message, claude)
    except Exception as e:
        await websocket.send_json({
            "type": "error",
            "content": f"Error generating response: {str(e)}"
        })

    await update_session_activity(session_id)


@app.websocket("/ws/chat/{session_id}")
async def websocket_chat(websocket: WebSocket, session_id: str):
    """WebSocket endpoint for chat."""
    await websocket.accept()

    if not await session_exists(session_id):
        await websocket.send_json({
            "type": "error",
            "content": "Session not found"
        })
        await websocket.close()
        return

    await send_message_history(websocket, session_id)
    await update_session_activity(session_id)

    claude = ClaudeChat()

    try:
        while True:
            data = await websocket.receive_text()
            message_data = json.loads(data)

            if message_data.get("type") == "user_message":
                user_message = message_data.get("content", "").strip()
                if user_message:
                    await handle_user_message(websocket, session_id, user_message, claude)

    except WebSocketDisconnect:
        print(f"WebSocket disconnected for session {session_id}")
    except Exception as e:
        print(f"WebSocket error: {str(e)}")
        try:
            await websocket.send_json({
                "type": "error",
                "content": f"Connection error: {str(e)}"
            })
        except:
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
