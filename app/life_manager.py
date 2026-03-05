"""
Life Manager: task, event, and interjection CRUD exposed as plain async
functions that are registered on pydantic-ai agents via @agent.tool_plain.
"""
import json
from typing import Any

from app.config import settings
from app.logger import get_logger
from app.repositories import TaskRepo, EventRepo, InterjectionRepo

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Task tools
# ---------------------------------------------------------------------------

async def create_task(
    title: str,
    description: str | None = None,
    priority: int = 3,
    category: str | None = None,
    due_date: str | None = None,
    due_time: str | None = None,
    source: str = "manual",
    source_ref: str | None = None,
    parent_task_id: str | None = None,
) -> str:
    """Create a new task. Returns the created task as JSON.

    Args:
        title: Task title.
        description: Optional details.
        priority: 1=urgent, 2=high, 3=normal, 4=low.
        category: Category: work, personal, health, finance, errands, etc.
        due_date: Due date in ISO-8601 format (YYYY-MM-DD).
        due_time: Due time in HH:MM format.
        source: Where this task came from: manual, email, su_inferred.
        source_ref: Reference URL or identifier for the source.
        parent_task_id: Parent task ID for subtasks.
    """
    task = await TaskRepo.create(
        title=title, description=description, priority=priority,
        category=category, due_date=due_date, due_time=due_time,
        source=source, source_ref=source_ref, parent_task_id=parent_task_id,
    )
    log.info("life_manager.task_created", task_id=task.id, title=task.title)
    return json.dumps(task.model_dump(exclude_none=True), indent=2, default=str)


async def update_task(
    id: str,
    title: str | None = None,
    description: str | None = None,
    status: str | None = None,
    priority: int | None = None,
    category: str | None = None,
    due_date: str | None = None,
    due_time: str | None = None,
) -> str:
    """Update an existing task. Only provided fields are changed.

    Args:
        id: Task ID to update.
        title: New title.
        description: New description.
        status: New status: pending, in_progress, done, cancelled.
        priority: New priority: 1=urgent, 2=high, 3=normal, 4=low.
        category: New category.
        due_date: New due date (YYYY-MM-DD).
        due_time: New due time (HH:MM).
    """
    fields = {k: v for k, v in dict(
        title=title, description=description, status=status,
        priority=priority, category=category, due_date=due_date,
        due_time=due_time,
    ).items() if v is not None}
    if not fields:
        return json.dumps({"error": "No fields to update"})
    await TaskRepo.update(id, **fields)
    log.info("life_manager.task_updated", task_id=id)
    return json.dumps({"updated": id})


async def list_tasks(
    status: str | None = None,
    category: str | None = None,
    due_before: str | None = None,
    due_after: str | None = None,
    priority: int | None = None,
    limit: int = 50,
) -> str:
    """List tasks with optional filters. Returns matching tasks sorted by priority then due date.

    Args:
        status: Filter by status: pending, in_progress, done, cancelled.
        category: Filter by category.
        due_before: Only tasks due on or before this date (YYYY-MM-DD).
        due_after: Only tasks due on or after this date (YYYY-MM-DD).
        priority: Filter by exact priority.
        limit: Max results (default 50).
    """
    tasks = await TaskRepo.list(
        status=status, category=category, due_before=due_before,
        due_after=due_after, priority=priority, limit=limit,
    )
    return json.dumps(tasks, indent=2, default=str)


async def complete_task(id: str) -> str:
    """Mark a task as done.

    Args:
        id: Task ID to complete.
    """
    await TaskRepo.complete(id)
    log.info("life_manager.task_completed", task_id=id)
    return json.dumps({"completed": id})


async def delete_task(id: str) -> str:
    """Delete a task permanently.

    Args:
        id: Task ID to delete.
    """
    await TaskRepo.delete(id)
    log.info("life_manager.task_deleted", task_id=id)
    return json.dumps({"deleted": id})


# ---------------------------------------------------------------------------
# Event tools
# ---------------------------------------------------------------------------

async def create_event(
    title: str,
    start_time: str,
    end_time: str | None = None,
    description: str | None = None,
    all_day: bool = False,
    location: str | None = None,
    reminder_minutes: int = 30,
    source: str = "manual",
    source_ref: str | None = None,
) -> str:
    """Create a calendar event. Returns the created event as JSON.

    Args:
        title: Event title.
        start_time: Start time in ISO-8601 format (YYYY-MM-DDTHH:MM).
        end_time: End time in ISO-8601 format (optional).
        description: Event description.
        all_day: True for all-day events.
        location: Event location.
        reminder_minutes: Minutes before event to remind (default 30).
        source: Source of the event.
        source_ref: Reference URL or identifier.
    """
    event = await EventRepo.create(
        title=title, start_time=start_time, end_time=end_time,
        description=description, all_day=all_day, location=location,
        reminder_minutes=reminder_minutes, source=source, source_ref=source_ref,
    )
    log.info("life_manager.event_created", event_id=event.id, title=event.title)
    return json.dumps(event.model_dump(exclude_none=True), indent=2, default=str)


async def update_event(
    id: str,
    title: str | None = None,
    description: str | None = None,
    start_time: str | None = None,
    end_time: str | None = None,
    all_day: bool | None = None,
    location: str | None = None,
    reminder_minutes: int | None = None,
) -> str:
    """Update an existing calendar event. Only provided fields are changed.

    Args:
        id: Event ID to update.
        title: New title.
        description: New description.
        start_time: New start time.
        end_time: New end time.
        all_day: All-day flag.
        location: New location.
        reminder_minutes: New reminder time.
    """
    fields = {k: v for k, v in dict(
        title=title, description=description, start_time=start_time,
        end_time=end_time, all_day=all_day, location=location,
        reminder_minutes=reminder_minutes,
    ).items() if v is not None}
    if not fields:
        return json.dumps({"error": "No fields to update"})
    await EventRepo.update(id, **fields)
    log.info("life_manager.event_updated", event_id=id)
    return json.dumps({"updated": id})


async def list_events(
    start_after: str | None = None,
    start_before: str | None = None,
    limit: int = 50,
) -> str:
    """List calendar events with optional date range filters.

    Args:
        start_after: Only events starting on or after this datetime (ISO-8601).
        start_before: Only events starting on or before this datetime (ISO-8601).
        limit: Max results (default 50).
    """
    events = await EventRepo.list(
        start_after=start_after, start_before=start_before, limit=limit,
    )
    return json.dumps(events, indent=2, default=str)


async def delete_event(id: str) -> str:
    """Delete a calendar event.

    Args:
        id: Event ID to delete.
    """
    await EventRepo.delete(id)
    log.info("life_manager.event_deleted", event_id=id)
    return json.dumps({"deleted": id})


# ---------------------------------------------------------------------------
# Interjection tools
# ---------------------------------------------------------------------------

async def create_interjection(
    content: str,
    urgency: str = "normal",
    source: str | None = None,
    related_task_id: str | None = None,
    related_event_id: str | None = None,
    related_su_note_id: str | None = None,
) -> str:
    """Queue a proactive message from SU to the user — reminders, observations, coaching.

    Args:
        content: The message to deliver.
        urgency: How urgently the user should see this: low, normal, high, urgent.
        source: Which process created this: morning_brief, calendar_check, email_scan, etc.
        related_task_id: Link to a related task.
        related_event_id: Link to a related event.
        related_su_note_id: Link to the SU note that triggered this interjection.
    """
    interjection = await InterjectionRepo.create(
        content=content, urgency=urgency, source=source,
        related_task_id=related_task_id, related_event_id=related_event_id,
        related_su_note_id=related_su_note_id,
    )
    log.info("life_manager.interjection_created", id=interjection.id,
             urgency=interjection.urgency, source=interjection.source)
    return json.dumps({"id": interjection.id, "status": "pending"})


async def list_interjections(
    status: str | None = None,
    limit: int = 20,
) -> str:
    """List interjections, optionally filtered by status.

    Args:
        status: Filter: pending, delivered, dismissed, acted_on.
        limit: Max results (default 20).
    """
    items = await InterjectionRepo.list(status=status, limit=limit)
    return json.dumps(items, indent=2, default=str)
