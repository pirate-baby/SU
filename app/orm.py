"""
SQLAlchemy ORM models for tasks, events, and interjections.

Maps to existing tables created by database.init_database().
"""
from datetime import datetime
from typing import Optional

from sqlalchemy import String, Integer, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class TaskRow(Base):
    __tablename__ = "tasks"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    title: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, default=None)
    status: Mapped[str] = mapped_column(String, default="pending")
    priority: Mapped[int] = mapped_column(Integer, default=3)
    category: Mapped[Optional[str]] = mapped_column(String, default=None)
    due_date: Mapped[Optional[str]] = mapped_column(String, default=None)
    due_time: Mapped[Optional[str]] = mapped_column(String, default=None)
    recurrence: Mapped[Optional[str]] = mapped_column(String, default=None)
    source: Mapped[str] = mapped_column(String, default="manual")
    source_ref: Mapped[Optional[str]] = mapped_column(String, default=None)
    parent_task_id: Mapped[Optional[str]] = mapped_column(String, default=None)
    created_at: Mapped[Optional[str]] = mapped_column(String, default=None)
    updated_at: Mapped[Optional[str]] = mapped_column(String, default=None)
    completed_at: Mapped[Optional[str]] = mapped_column(String, default=None)

    def to_dict(self) -> dict:
        return {c.key: getattr(self, c.key) for c in self.__table__.columns}


class EventRow(Base):
    __tablename__ = "events"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    title: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, default=None)
    start_time: Mapped[str] = mapped_column(String, nullable=False)
    end_time: Mapped[Optional[str]] = mapped_column(String, default=None)
    all_day: Mapped[int] = mapped_column(Integer, default=0)
    location: Mapped[Optional[str]] = mapped_column(String, default=None)
    recurrence: Mapped[Optional[str]] = mapped_column(String, default=None)
    source: Mapped[str] = mapped_column(String, default="manual")
    source_ref: Mapped[Optional[str]] = mapped_column(String, default=None)
    reminder_minutes: Mapped[int] = mapped_column(Integer, default=30)
    created_at: Mapped[Optional[str]] = mapped_column(String, default=None)
    updated_at: Mapped[Optional[str]] = mapped_column(String, default=None)

    def to_dict(self) -> dict:
        return {c.key: getattr(self, c.key) for c in self.__table__.columns}



class InterjectionRow(Base):
    __tablename__ = "interjections"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    urgency: Mapped[str] = mapped_column(String, default="normal")
    source: Mapped[Optional[str]] = mapped_column(String, default=None)
    related_task_id: Mapped[Optional[str]] = mapped_column(String, default=None)
    related_event_id: Mapped[Optional[str]] = mapped_column(String, default=None)
    related_su_note_id: Mapped[Optional[str]] = mapped_column(String, default=None)
    session_id: Mapped[Optional[str]] = mapped_column(String, default=None)
    status: Mapped[str] = mapped_column(String, default="pending")
    created_at: Mapped[Optional[str]] = mapped_column(String, default=None)
    delivered_at: Mapped[Optional[str]] = mapped_column(String, default=None)

    def to_dict(self) -> dict:
        return {c.key: getattr(self, c.key) for c in self.__table__.columns}


class TelegramUserRow(Base):
    __tablename__ = "telegram_users"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    telegram_chat_id: Mapped[int] = mapped_column(Integer, nullable=False, unique=True)
    telegram_user_id: Mapped[int] = mapped_column(Integer, nullable=False)
    telegram_username: Mapped[Optional[str]] = mapped_column(String, default=None)
    registered_at: Mapped[Optional[str]] = mapped_column(String, default=None)

    def to_dict(self) -> dict:
        return {c.key: getattr(self, c.key) for c in self.__table__.columns}


class UnsubscribedSenderRow(Base):
    __tablename__ = "unsubscribed_senders"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    sender_email: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    sender_domain: Mapped[str] = mapped_column(String, nullable=False)
    unsubscribe_method: Mapped[str] = mapped_column(String, nullable=False)
    unsubscribe_target: Mapped[Optional[str]] = mapped_column(String, default=None)
    status: Mapped[str] = mapped_column(String, default="completed")
    error: Mapped[Optional[str]] = mapped_column(Text, default=None)
    created_at: Mapped[Optional[str]] = mapped_column(String, default=None)

    def to_dict(self) -> dict:
        return {c.key: getattr(self, c.key) for c in self.__table__.columns}


class SuNoteRow(Base):
    __tablename__ = "su_notes"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    note_type: Mapped[str] = mapped_column(String, default="todo")
    status: Mapped[str] = mapped_column(String, default="active")
    priority: Mapped[str] = mapped_column(String, default="normal")
    activate_after: Mapped[Optional[str]] = mapped_column(String, default=None)
    related_task_id: Mapped[Optional[str]] = mapped_column(String, default=None)
    related_interjection_id: Mapped[Optional[str]] = mapped_column(String, default=None)
    source: Mapped[Optional[str]] = mapped_column(String, default=None)
    context_json: Mapped[Optional[str]] = mapped_column(Text, default=None)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[Optional[str]] = mapped_column(String, default=None)
    updated_at: Mapped[Optional[str]] = mapped_column(String, default=None)
    completed_at: Mapped[Optional[str]] = mapped_column(String, default=None)

    def to_dict(self) -> dict:
        return {c.key: getattr(self, c.key) for c in self.__table__.columns}
