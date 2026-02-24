"""
Repository layer: model-based CRUD for tasks, events, and interjections.

Replaces scattered raw SQL with a clean data-access interface built on the
existing Pydantic models and aiosqlite connection manager.
"""
import uuid
from datetime import datetime
from typing import Any, Optional

from app.database import get_db
from app.models import Task, Event, Interjection


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
        async with get_db() as db:
            await db.execute(
                """INSERT INTO tasks
                   (id, title, description, priority, category, due_date,
                    due_time, source, source_ref, parent_task_id,
                    created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (task_id, title, description, priority, category, due_date,
                 due_time, source, source_ref, parent_task_id, now, now),
            )
            await db.commit()
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
        sets: list[str] = []
        vals: list[Any] = []
        for key, val in fields.items():
            if key in allowed and val is not None:
                sets.append(f"{key} = ?")
                vals.append(val)
        if not sets:
            return
        if fields.get("status") == "done":
            sets.append("completed_at = ?")
            vals.append(_now())
        sets.append("updated_at = ?")
        vals.append(_now())
        vals.append(task_id)
        async with get_db() as db:
            await db.execute(
                f"UPDATE tasks SET {', '.join(sets)} WHERE id = ?", vals,
            )
            await db.commit()

    @staticmethod
    async def complete(task_id: str) -> None:
        now = _now()
        async with get_db() as db:
            await db.execute(
                "UPDATE tasks SET status = 'done', completed_at = ?, updated_at = ? WHERE id = ?",
                (now, now, task_id),
            )
            await db.commit()

    @staticmethod
    async def delete(task_id: str) -> None:
        async with get_db() as db:
            await db.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
            await db.commit()

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
        async with get_db() as db:
            await db.execute(
                """INSERT INTO events
                   (id, title, description, start_time, end_time, all_day,
                    location, source, source_ref, reminder_minutes,
                    created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (event_id, title, description, start_time, end_time,
                 1 if all_day else 0, location, source, source_ref,
                 reminder_minutes, now, now),
            )
            await db.commit()
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
        sets: list[str] = []
        vals: list[Any] = []
        for key, val in fields.items():
            if key in allowed and val is not None:
                if key == "all_day":
                    val = 1 if val else 0
                sets.append(f"{key} = ?")
                vals.append(val)
        if not sets:
            return
        sets.append("updated_at = ?")
        vals.append(_now())
        vals.append(event_id)
        async with get_db() as db:
            await db.execute(
                f"UPDATE events SET {', '.join(sets)} WHERE id = ?", vals,
            )
            await db.commit()

    @staticmethod
    async def delete(event_id: str) -> None:
        async with get_db() as db:
            await db.execute("DELETE FROM events WHERE id = ?", (event_id,))
            await db.commit()

    @staticmethod
    async def list(
        *,
        start_after: Optional[str] = None,
        start_before: Optional[str] = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
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

    @staticmethod
    async def upcoming_within_window(now: datetime) -> list[dict[str, Any]]:
        """Return future events (for calendar reminder checks)."""
        async with get_db() as db:
            cursor = await db.execute(
                """SELECT * FROM events
                   WHERE start_time > ?
                   ORDER BY start_time ASC
                   LIMIT 50""",
                (now.isoformat(),),
            )
            return [dict(row) for row in await cursor.fetchall()]


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
        async with get_db() as db:
            await db.execute(
                """INSERT INTO interjections
                   (id, content, urgency, source, related_task_id,
                    related_event_id, status, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)""",
                (interjection_id, content, urgency, source,
                 related_task_id, related_event_id, now),
            )
            await db.commit()
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
        clauses: list[str] = []
        params: list[Any] = []
        if status:
            clauses.append("status = ?")
            params.append(status)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(limit)
        async with get_db() as db:
            cursor = await db.execute(
                f"SELECT * FROM interjections {where} ORDER BY created_at DESC LIMIT ?",
                params,
            )
            return [dict(row) for row in await cursor.fetchall()]

    @staticmethod
    async def pending(limit: int = 10) -> list[dict[str, Any]]:
        """Return pending interjections ordered by creation time (oldest first)."""
        async with get_db() as db:
            cursor = await db.execute(
                "SELECT * FROM interjections WHERE status = 'pending' ORDER BY created_at ASC LIMIT ?",
                (limit,),
            )
            return [dict(row) for row in await cursor.fetchall()]

    @staticmethod
    async def mark_delivered(interjection_id: str) -> None:
        async with get_db() as db:
            await db.execute(
                "UPDATE interjections SET status = 'delivered', delivered_at = ? WHERE id = ?",
                (_now(), interjection_id),
            )
            await db.commit()

    @staticmethod
    async def dismiss(interjection_id: str) -> None:
        async with get_db() as db:
            await db.execute(
                "UPDATE interjections SET status = 'dismissed' WHERE id = ?",
                (interjection_id,),
            )
            await db.commit()
