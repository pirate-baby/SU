"""
Pydantic models for sessions, messages, tasks, events, and interjections.
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


# ---------------------------------------------------------------------------
# Planner models (tasks, events, interjections)
# ---------------------------------------------------------------------------

class Task(BaseModel):
    id: Optional[str] = None
    title: str
    description: Optional[str] = None
    status: str = "pending"
    priority: int = 3
    category: Optional[str] = None
    due_date: Optional[str] = None
    due_time: Optional[str] = None
    recurrence: Optional[str] = None
    source: str = "manual"
    source_ref: Optional[str] = None
    parent_task_id: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None


class Event(BaseModel):
    id: Optional[str] = None
    title: str
    description: Optional[str] = None
    start_time: str
    end_time: Optional[str] = None
    all_day: bool = False
    location: Optional[str] = None
    recurrence: Optional[str] = None
    source: str = "manual"
    source_ref: Optional[str] = None
    reminder_minutes: int = 30
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class Interjection(BaseModel):
    id: Optional[str] = None
    content: str
    urgency: str = "normal"
    source: Optional[str] = None
    related_task_id: Optional[str] = None
    related_event_id: Optional[str] = None
    related_su_note_id: Optional[str] = None
    session_id: Optional[str] = None
    status: str = "pending"
    created_at: Optional[datetime] = None
    delivered_at: Optional[datetime] = None


class Document(BaseModel):
    id: Optional[str] = None
    title: str
    filename: Optional[str] = None
    file_path: Optional[str] = None
    file_size: int = 0
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class SuNote(BaseModel):
    id: Optional[str] = None
    content: str
    note_type: str = "todo"
    status: str = "active"
    priority: str = "normal"
    activate_after: Optional[str] = None
    related_task_id: Optional[str] = None
    related_interjection_id: Optional[str] = None
    source: Optional[str] = None
    context_json: Optional[str] = None
    attempts: int = 0
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
