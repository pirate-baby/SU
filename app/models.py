"""
Pydantic models for sessions and messages.
"""
from pydantic import BaseModel
from typing import Optional, List
from datetime import datetime


class Message(BaseModel):
    id: Optional[int] = None
    session_id: str
    role: str
    content: str
    created_at: Optional[datetime] = None


class Session(BaseModel):
    id: str
    created_at: Optional[datetime] = None
    last_activity: Optional[datetime] = None
    status: str = "active"
    claude_state: Optional[str] = None
    messages: Optional[List[Message]] = None


class ChatMessage(BaseModel):
    type: str
    content: str
    session_id: Optional[str] = None


class SessionCreateResponse(BaseModel):
    session_id: str
    redirect_url: str
