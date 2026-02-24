"""
Repository layer: SQLAlchemy ORM-based CRUD for tasks, events, and interjections.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import select, update, delete

from app.database import async_session
from app.models import Task, Event, Interjection
from app.orm import TaskRow, EventRow, InterjectionRow, PushSubscriptionRow


def _now() -> str:
    return datetime.utcnow().isoformat()


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
    ) -> Interjection:
        interjection_id = str(uuid.uuid4())
        now = _now()
        row = InterjectionRow(
            id=interjection_id, content=content, urgency=urgency,
            source=source, related_task_id=related_task_id,
            related_event_id=related_event_id, status="pending",
            created_at=now,
        )
        async with async_session() as session:
            session.add(row)
            await session.commit()
        return Interjection(
            id=interjection_id, content=content, urgency=urgency,
            source=source, related_task_id=related_task_id,
            related_event_id=related_event_id,
        )

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
# Push Subscriptions
# ---------------------------------------------------------------------------

class PushSubscriptionRepo:
    """CRUD operations for push_subscriptions (Web Push / VAPID)."""

    @staticmethod
    async def upsert(endpoint: str, subscription_json: str) -> str:
        """Insert or update a push subscription. Returns the subscription id."""
        async with async_session() as session:
            # Check if this endpoint already exists
            stmt = select(PushSubscriptionRow).where(PushSubscriptionRow.endpoint == endpoint)
            result = await session.execute(stmt)
            existing = result.scalar_one_or_none()

            if existing:
                existing.subscription_json = subscription_json
                await session.commit()
                return existing.id

            sub_id = str(uuid.uuid4())
            row = PushSubscriptionRow(
                id=sub_id,
                endpoint=endpoint,
                subscription_json=subscription_json,
                created_at=_now(),
            )
            session.add(row)
            await session.commit()
            return sub_id

    @staticmethod
    async def list_all() -> list[dict[str, Any]]:
        async with async_session() as session:
            result = await session.execute(select(PushSubscriptionRow))
            return [row.to_dict() for row in result.scalars().all()]

    @staticmethod
    async def delete(sub_id: str) -> None:
        async with async_session() as session:
            await session.execute(
                delete(PushSubscriptionRow).where(PushSubscriptionRow.id == sub_id)
            )
            await session.commit()
