"""
Repository layer: SQLAlchemy ORM-based CRUD for tasks, events, interjections, and documents.
"""
from __future__ import annotations

import re as _re
import uuid
from pathlib import Path
from typing import Any, Optional

from sqlalchemy import select, update, delete

from app.config import settings
from app.database import async_session
from app.models import Task, Event, Interjection, SuNote
from app.orm import TaskRow, EventRow, InterjectionRow, SuNoteRow, UnsubscribedSenderRow, DocumentRow
from app.tz import now_iso


def _now() -> str:
    return now_iso()


# ---------------------------------------------------------------------------
# Tasks
# ---------------------------------------------------------------------------

class TaskRepo:
    """CRUD operations for the tasks table."""

    @staticmethod
    async def create(
        title: str,
        description: Optional[str] = None,
        priority: int = 3,
        category: Optional[str] = None,
        due_date: Optional[str] = None,
        due_time: Optional[str] = None,
        source: str = "manual",
        source_ref: Optional[str] = None,
        parent_task_id: Optional[str] = None,
    ) -> Task:
        task_id = str(uuid.uuid4())
        now = _now()
        row = TaskRow(
            id=task_id, title=title, description=description,
            priority=priority, category=category, due_date=due_date,
            due_time=due_time, source=source, source_ref=source_ref,
            parent_task_id=parent_task_id, created_at=now, updated_at=now,
        )
        async with async_session() as session:
            session.add(row)
            await session.commit()
        return Task(
            id=task_id, title=title, description=description,
            priority=priority, category=category, due_date=due_date,
            due_time=due_time, source=source, source_ref=source_ref,
            parent_task_id=parent_task_id,
        )

    @staticmethod
    async def update(task_id: str, **fields: Any) -> None:
        allowed = {"title", "description", "status", "priority", "category",
                    "due_date", "due_time"}
        values: dict[str, Any] = {}
        for key, val in fields.items():
            if key in allowed and val is not None:
                values[key] = val
        if not values:
            return
        if fields.get("status") == "done":
            values["completed_at"] = _now()
        values["updated_at"] = _now()
        async with async_session() as session:
            await session.execute(
                update(TaskRow).where(TaskRow.id == task_id).values(**values)
            )
            await session.commit()

    @staticmethod
    async def complete(task_id: str) -> None:
        now = _now()
        async with async_session() as session:
            await session.execute(
                update(TaskRow)
                .where(TaskRow.id == task_id)
                .values(status="done", completed_at=now, updated_at=now)
            )
            await session.commit()

    @staticmethod
    async def delete(task_id: str) -> None:
        async with async_session() as session:
            await session.execute(
                delete(TaskRow).where(TaskRow.id == task_id)
            )
            await session.commit()

    @staticmethod
    async def list(
        *,
        status: Optional[str] = None,
        category: Optional[str] = None,
        due_before: Optional[str] = None,
        due_after: Optional[str] = None,
        priority: Optional[int] = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        stmt = select(TaskRow)
        if status:
            stmt = stmt.where(TaskRow.status == status)
        if category:
            stmt = stmt.where(TaskRow.category == category)
        if due_before:
            stmt = stmt.where(TaskRow.due_date <= due_before)
        if due_after:
            stmt = stmt.where(TaskRow.due_date >= due_after)
        if priority is not None:
            stmt = stmt.where(TaskRow.priority == priority)
        stmt = stmt.order_by(TaskRow.priority.asc(), TaskRow.due_date.asc()).limit(limit)
        async with async_session() as session:
            result = await session.execute(stmt)
            return [row.to_dict() for row in result.scalars().all()]


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------

class EventRepo:
    """CRUD operations for the events table."""

    @staticmethod
    async def create(
        title: str,
        start_time: str,
        end_time: Optional[str] = None,
        description: Optional[str] = None,
        all_day: bool = False,
        location: Optional[str] = None,
        reminder_minutes: int = 30,
        source: str = "manual",
        source_ref: Optional[str] = None,
    ) -> Event:
        event_id = str(uuid.uuid4())
        now = _now()
        row = EventRow(
            id=event_id, title=title, description=description,
            start_time=start_time, end_time=end_time,
            all_day=1 if all_day else 0, location=location,
            source=source, source_ref=source_ref,
            reminder_minutes=reminder_minutes, created_at=now, updated_at=now,
        )
        async with async_session() as session:
            session.add(row)
            await session.commit()
        return Event(
            id=event_id, title=title, description=description,
            start_time=start_time, end_time=end_time, all_day=all_day,
            location=location, source=source, source_ref=source_ref,
            reminder_minutes=reminder_minutes,
        )

    @staticmethod
    async def update(event_id: str, **fields: Any) -> None:
        allowed = {"title", "description", "start_time", "end_time",
                    "all_day", "location", "reminder_minutes"}
        values: dict[str, Any] = {}
        for key, val in fields.items():
            if key in allowed and val is not None:
                if key == "all_day":
                    val = 1 if val else 0
                values[key] = val
        if not values:
            return
        values["updated_at"] = _now()
        async with async_session() as session:
            await session.execute(
                update(EventRow).where(EventRow.id == event_id).values(**values)
            )
            await session.commit()

    @staticmethod
    async def delete(event_id: str) -> None:
        async with async_session() as session:
            await session.execute(
                delete(EventRow).where(EventRow.id == event_id)
            )
            await session.commit()

    @staticmethod
    async def list(
        *,
        start_after: Optional[str] = None,
        start_before: Optional[str] = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        stmt = select(EventRow)
        if start_after:
            stmt = stmt.where(EventRow.start_time >= start_after)
        if start_before:
            stmt = stmt.where(EventRow.start_time <= start_before)
        stmt = stmt.order_by(EventRow.start_time.asc()).limit(limit)
        async with async_session() as session:
            result = await session.execute(stmt)
            return [row.to_dict() for row in result.scalars().all()]

    @staticmethod
    async def upcoming_within_window(now: datetime) -> list[dict[str, Any]]:
        """Return future events (for calendar reminder checks)."""
        stmt = (
            select(EventRow)
            .where(EventRow.start_time > now.isoformat())
            .order_by(EventRow.start_time.asc())
            .limit(50)
        )
        async with async_session() as session:
            result = await session.execute(stmt)
            return [row.to_dict() for row in result.scalars().all()]


# ---------------------------------------------------------------------------
# Interjections
# ---------------------------------------------------------------------------

class InterjectionRepo:
    """CRUD operations for the interjections table."""

    @staticmethod
    async def create(
        content: str,
        urgency: str = "normal",
        source: Optional[str] = None,
        related_task_id: Optional[str] = None,
        related_event_id: Optional[str] = None,
        related_su_note_id: Optional[str] = None,
    ) -> Interjection:
        interjection_id = str(uuid.uuid4())
        now = _now()
        row = InterjectionRow(
            id=interjection_id, content=content, urgency=urgency,
            source=source, related_task_id=related_task_id,
            related_event_id=related_event_id,
            related_su_note_id=related_su_note_id,
            status="pending", created_at=now,
        )
        async with async_session() as session:
            session.add(row)
            await session.commit()
        return Interjection(
            id=interjection_id, content=content, urgency=urgency,
            source=source, related_task_id=related_task_id,
            related_event_id=related_event_id,
            related_su_note_id=related_su_note_id,
        )

    @staticmethod
    async def link_session(interjection_id: str, session_id: str) -> None:
        """Link an interjection to a chat session."""
        async with async_session() as session:
            await session.execute(
                update(InterjectionRow)
                .where(InterjectionRow.id == interjection_id)
                .values(session_id=session_id)
            )
            await session.commit()

    @staticmethod
    async def get(interjection_id: str) -> Optional[dict[str, Any]]:
        """Get a single interjection by ID."""
        async with async_session() as session:
            result = await session.execute(
                select(InterjectionRow).where(InterjectionRow.id == interjection_id)
            )
            row = result.scalar_one_or_none()
            return row.to_dict() if row else None

    @staticmethod
    async def list(
        *,
        status: Optional[str] = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        stmt = select(InterjectionRow)
        if status:
            stmt = stmt.where(InterjectionRow.status == status)
        stmt = stmt.order_by(InterjectionRow.created_at.desc()).limit(limit)
        async with async_session() as session:
            result = await session.execute(stmt)
            return [row.to_dict() for row in result.scalars().all()]

    @staticmethod
    async def pending(limit: int = 10) -> list[dict[str, Any]]:
        """Return pending interjections ordered by creation time (oldest first)."""
        stmt = (
            select(InterjectionRow)
            .where(InterjectionRow.status == "pending")
            .order_by(InterjectionRow.created_at.asc())
            .limit(limit)
        )
        async with async_session() as session:
            result = await session.execute(stmt)
            return [row.to_dict() for row in result.scalars().all()]

    @staticmethod
    async def mark_delivered(interjection_id: str) -> None:
        async with async_session() as session:
            await session.execute(
                update(InterjectionRow)
                .where(InterjectionRow.id == interjection_id)
                .values(status="delivered", delivered_at=_now())
            )
            await session.commit()

    @staticmethod
    async def dismiss(interjection_id: str) -> None:
        async with async_session() as session:
            await session.execute(
                update(InterjectionRow)
                .where(InterjectionRow.id == interjection_id)
                .values(status="dismissed")
            )
            await session.commit()


# ---------------------------------------------------------------------------
# SU Notes (internal notes-to-self for daemon coordination)
# ---------------------------------------------------------------------------

class SuNoteRepo:
    """CRUD operations for the su_notes table."""

    @staticmethod
    async def create(
        content: str,
        note_type: str = "todo",
        priority: str = "normal",
        activate_after: Optional[str] = None,
        related_task_id: Optional[str] = None,
        related_interjection_id: Optional[str] = None,
        source: Optional[str] = None,
        context_json: Optional[str] = None,
    ) -> SuNote:
        note_id = str(uuid.uuid4())
        now = _now()
        row = SuNoteRow(
            id=note_id, content=content, note_type=note_type,
            status="active", priority=priority,
            activate_after=activate_after,
            related_task_id=related_task_id,
            related_interjection_id=related_interjection_id,
            source=source, context_json=context_json,
            attempts=0, created_at=now, updated_at=now,
        )
        async with async_session() as session:
            session.add(row)
            await session.commit()
        return SuNote(
            id=note_id, content=content, note_type=note_type,
            priority=priority, activate_after=activate_after,
            related_task_id=related_task_id,
            related_interjection_id=related_interjection_id,
            source=source, context_json=context_json,
        )

    @staticmethod
    async def get(note_id: str) -> Optional[dict[str, Any]]:
        """Get a single SU note by ID."""
        async with async_session() as session:
            result = await session.execute(
                select(SuNoteRow).where(SuNoteRow.id == note_id)
            )
            row = result.scalar_one_or_none()
            return row.to_dict() if row else None

    @staticmethod
    async def update(note_id: str, **fields: Any) -> None:
        allowed = {"content", "note_type", "status", "priority", "activate_after",
                    "related_task_id", "related_interjection_id", "context_json", "attempts"}
        values: dict[str, Any] = {}
        for key, val in fields.items():
            if key in allowed:
                values[key] = val
        if not values:
            return
        if fields.get("status") == "done":
            values["completed_at"] = _now()
        values["updated_at"] = _now()
        async with async_session() as session:
            await session.execute(
                update(SuNoteRow).where(SuNoteRow.id == note_id).values(**values)
            )
            await session.commit()

    @staticmethod
    async def complete(note_id: str) -> None:
        now = _now()
        async with async_session() as session:
            await session.execute(
                update(SuNoteRow)
                .where(SuNoteRow.id == note_id)
                .values(status="done", completed_at=now, updated_at=now)
            )
            await session.commit()

    @staticmethod
    async def delete(note_id: str) -> None:
        async with async_session() as session:
            await session.execute(
                delete(SuNoteRow).where(SuNoteRow.id == note_id)
            )
            await session.commit()

    @staticmethod
    async def list_active_due(
        now_iso: Optional[str] = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Return active notes whose activate_after has passed (or is NULL)."""
        if now_iso is None:
            now_iso = _now()
        stmt = (
            select(SuNoteRow)
            .where(SuNoteRow.status == "active")
            .where(
                (SuNoteRow.activate_after.is_(None)) |
                (SuNoteRow.activate_after <= now_iso)
            )
            .order_by(SuNoteRow.priority.desc(), SuNoteRow.created_at.asc())
            .limit(limit)
        )
        async with async_session() as session:
            result = await session.execute(stmt)
            return [row.to_dict() for row in result.scalars().all()]

    @staticmethod
    async def list(
        *,
        status: Optional[str] = None,
        note_type: Optional[str] = None,
        source: Optional[str] = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        stmt = select(SuNoteRow)
        if status:
            stmt = stmt.where(SuNoteRow.status == status)
        if note_type:
            stmt = stmt.where(SuNoteRow.note_type == note_type)
        if source:
            stmt = stmt.where(SuNoteRow.source == source)
        stmt = stmt.order_by(SuNoteRow.created_at.desc()).limit(limit)
        async with async_session() as session:
            result = await session.execute(stmt)
            return [row.to_dict() for row in result.scalars().all()]

    @staticmethod
    async def increment_attempts(note_id: str) -> None:
        """Increment the attempts counter and update timestamp."""
        async with async_session() as session:
            await session.execute(
                update(SuNoteRow)
                .where(SuNoteRow.id == note_id)
                .values(
                    attempts=SuNoteRow.attempts + 1,
                    updated_at=_now(),
                )
            )
            await session.commit()


# ---------------------------------------------------------------------------
# Unsubscribed Senders (tracks email unsubscribe actions)
# ---------------------------------------------------------------------------

class UnsubscribedSenderRepo:
    """CRUD operations for the unsubscribed_senders table."""

    @staticmethod
    async def is_unsubscribed(sender_email: str) -> bool:
        """Check if we've already unsubscribed from this sender."""
        async with async_session() as session:
            result = await session.execute(
                select(UnsubscribedSenderRow)
                .where(UnsubscribedSenderRow.sender_email == sender_email.lower())
            )
            return result.scalar_one_or_none() is not None

    @staticmethod
    async def record(
        sender_email: str,
        sender_domain: str,
        unsubscribe_method: str,
        unsubscribe_target: Optional[str] = None,
        status: str = "completed",
        error: Optional[str] = None,
    ) -> None:
        """Record an unsubscribe action."""
        row = UnsubscribedSenderRow(
            id=str(uuid.uuid4()),
            sender_email=sender_email.lower(),
            sender_domain=sender_domain.lower(),
            unsubscribe_method=unsubscribe_method,
            unsubscribe_target=unsubscribe_target,
            status=status,
            error=error,
            created_at=_now(),
        )
        async with async_session() as session:
            session.add(row)
            await session.commit()

    @staticmethod
    async def list_all(limit: int = 200) -> list[dict[str, Any]]:
        """List all unsubscribed senders."""
        async with async_session() as session:
            result = await session.execute(
                select(UnsubscribedSenderRow)
                .order_by(UnsubscribedSenderRow.created_at.desc())
                .limit(limit)
            )
            return [row.to_dict() for row in result.scalars().all()]


# ---------------------------------------------------------------------------
# Documents (markdown files authored in the web editor)
# ---------------------------------------------------------------------------

def _slugify(text: str) -> str:
    """Convert a title to a filesystem-safe slug."""
    slug = text.lower().strip()
    slug = _re.sub(r'[^\w\s-]', '', slug)
    slug = _re.sub(r'[\s_]+', '-', slug)
    slug = slug.strip('-')
    return slug[:80] or "untitled"


class DocumentRepo:
    """CRUD operations for documents table + file I/O on disk."""

    DOCS_DIR = Path(settings.documents_dir)

    @classmethod
    def ensure_dir(cls) -> Path:
        cls.DOCS_DIR.mkdir(parents=True, exist_ok=True)
        return cls.DOCS_DIR

    @staticmethod
    async def create(title: str, content: str = "") -> dict[str, Any]:
        doc_id = str(uuid.uuid4())
        now = _now()
        slug = _slugify(title)
        filename = f"{slug}.md"
        docs_dir = DocumentRepo.ensure_dir()
        file_path = docs_dir / f"{doc_id[:8]}_{filename}"
        file_path.write_text(content, encoding="utf-8")
        file_size = len(content.encode("utf-8"))

        row = DocumentRow(
            id=doc_id, title=title, filename=filename,
            file_path=str(file_path), file_size=file_size,
            created_at=now, updated_at=now,
        )
        async with async_session() as session:
            session.add(row)
            await session.commit()
        return row.to_dict()

    @staticmethod
    async def get(doc_id: str) -> Optional[dict[str, Any]]:
        async with async_session() as session:
            result = await session.execute(
                select(DocumentRow).where(DocumentRow.id == doc_id)
            )
            row = result.scalar_one_or_none()
            return row.to_dict() if row else None

    @staticmethod
    async def get_content(doc_id: str) -> Optional[str]:
        doc = await DocumentRepo.get(doc_id)
        if not doc:
            return None
        file_path = Path(doc["file_path"])
        if not file_path.exists():
            return ""
        return file_path.read_text(encoding="utf-8")

    @staticmethod
    async def update_content(doc_id: str, title: Optional[str] = None,
                             content: Optional[str] = None) -> None:
        values: dict[str, Any] = {"updated_at": _now()}
        if title is not None:
            values["title"] = title

        if content is not None:
            doc = await DocumentRepo.get(doc_id)
            if doc:
                file_path = Path(doc["file_path"])
                file_path.write_text(content, encoding="utf-8")
                values["file_size"] = len(content.encode("utf-8"))

        async with async_session() as session:
            await session.execute(
                update(DocumentRow).where(DocumentRow.id == doc_id).values(**values)
            )
            await session.commit()

    @staticmethod
    async def delete(doc_id: str) -> None:
        doc = await DocumentRepo.get(doc_id)
        if doc:
            file_path = Path(doc["file_path"])
            if file_path.exists():
                file_path.unlink()
        async with async_session() as session:
            await session.execute(
                delete(DocumentRow).where(DocumentRow.id == doc_id)
            )
            await session.commit()

    @staticmethod
    async def list(limit: int = 100) -> list[dict[str, Any]]:
        stmt = (
            select(DocumentRow)
            .order_by(DocumentRow.updated_at.desc())
            .limit(limit)
        )
        async with async_session() as session:
            result = await session.execute(stmt)
            return [row.to_dict() for row in result.scalars().all()]

