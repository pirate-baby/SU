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

from app.config import settings
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
    async for event in claude.send_message(user_message, conversation_history):
        event_type = event["type"]

        if event_type == "text":
            full_response += event["content"]
            await websocket.send_json({
                "type": "assistant_chunk",
                "content": event["content"]
            })
        elif event_type == "tool_use":
            await websocket.send_json({
                "type": "tool_use",
                "id": event["id"],
                "name": event["name"],
                "input": event["input"],
            })
        elif event_type == "tool_result":
            await websocket.send_json({
                "type": "tool_result",
                "tool_use_id": event["tool_use_id"],
                "content": event["content"],
                "is_error": event["is_error"],
            })
        elif event_type == "error":
            await websocket.send_json({
                "type": "error",
                "content": event["content"]
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
    print(f"WebSocket connection attempt for session {session_id}")

    try:
        await websocket.accept()
        print(f"WebSocket accepted for session {session_id}")
    except Exception as e:
        print(f"Failed to accept WebSocket: {str(e)}")
        return

    if not await session_exists(session_id):
        print(f"Session {session_id} not found")
        await websocket.send_json({
            "type": "error",
            "content": "Session not found"
        })
        await websocket.close()
        return

    await send_message_history(websocket, session_id)
    await update_session_activity(session_id)

    try:
        # Only pass token if it's actually set to avoid interfering with claude login auth
        if settings.claude_code_oauth_token:
            claude = ClaudeChat(oauth_token=settings.claude_code_oauth_token)
        else:
            claude = ClaudeChat()
    except Exception as e:
        print(f"Failed to initialize ClaudeChat: {str(e)}")
        await websocket.send_json({
            "type": "error",
            "content": f"Failed to initialize Claude client: {str(e)}"
        })
        await websocket.close()
        return

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
        print(f"WebSocket error for session {session_id}: {str(e)}")
        import traceback
        traceback.print_exc()
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
