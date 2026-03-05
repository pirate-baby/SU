"""
SU Notes Manager: CRUD for SU's internal notes-to-self, exposed as plain async
functions registered on pydantic-ai agents via @agent.tool_plain.
"""
import json
from typing import Any

from app.config import settings
from app.logger import get_logger
from app.repositories import SuNoteRepo

log = get_logger(__name__)


async def create_su_note(
    content: str,
    note_type: str = "todo",
    priority: str = "normal",
    activate_after: str | None = None,
    related_task_id: str | None = None,
    related_interjection_id: str | None = None,
    source: str | None = None,
    context_json: str | None = None,
) -> str:
    """Create an internal note-to-self for SU. NOT user tasks — these are SU's private operational notes.

    Args:
        content: What SU wants to remember or do.
        note_type: Type: todo, reminder, observation, log.
        priority: Priority: low, normal, high, urgent.
        activate_after: ISO datetime: don't act on this note before this time.
        related_task_id: Link to a user task this note relates to.
        related_interjection_id: Link to an interjection this note relates to.
        source: Which daemon created this: email_scanner, note_processor, daily_review, etc.
        context_json: JSON string with structured context.
    """
    note = await SuNoteRepo.create(
        content=content, note_type=note_type, priority=priority,
        activate_after=activate_after, related_task_id=related_task_id,
        related_interjection_id=related_interjection_id,
        source=source, context_json=context_json,
    )
    log.info("su_notes.created", note_id=note.id, note_type=note.note_type, source=note.source)
    return json.dumps(note.model_dump(exclude_none=True), indent=2, default=str)


async def get_su_note(id: str) -> str:
    """Read a single SU note with full context.

    Args:
        id: Note ID.
    """
    note = await SuNoteRepo.get(id)
    if not note:
        return json.dumps({"error": "Note not found"})
    return json.dumps(note, indent=2, default=str)


async def list_su_notes(
    status: str | None = None,
    note_type: str | None = None,
    source: str | None = None,
    include_snoozed: bool = False,
    limit: int = 50,
) -> str:
    """List SU's internal notes. By default returns active notes ready to act on.

    Args:
        status: Filter: active, done, snoozed, cancelled.
        note_type: Filter: todo, reminder, observation, log.
        source: Filter by source daemon.
        include_snoozed: If true, return all matching notes regardless of activate_after.
        limit: Max results (default 50).
    """
    if not include_snoozed and (status or "active") == "active":
        notes = await SuNoteRepo.list_active_due(limit=limit)
    else:
        notes = await SuNoteRepo.list(
            status=status, note_type=note_type, source=source, limit=limit,
        )
    return json.dumps(notes, indent=2, default=str)


async def update_su_note(
    id: str,
    content: str | None = None,
    note_type: str | None = None,
    status: str | None = None,
    priority: str | None = None,
    activate_after: str | None = None,
    context_json: str | None = None,
) -> str:
    """Update an existing SU note. Use this to snooze, change priority, update content/context.

    Args:
        id: Note ID to update.
        content: Updated content.
        note_type: New type: todo, reminder, observation, log.
        status: New status: active, done, snoozed, cancelled.
        priority: New priority: low, normal, high, urgent.
        activate_after: Snooze until this ISO datetime.
        context_json: Updated context JSON.
    """
    fields = {k: v for k, v in dict(
        content=content, note_type=note_type, status=status,
        priority=priority, activate_after=activate_after,
        context_json=context_json,
    ).items() if v is not None}
    if not fields:
        return json.dumps({"error": "No fields to update"})
    await SuNoteRepo.update(id, **fields)
    log.info("su_notes.updated", note_id=id)
    return json.dumps({"updated": id})


async def complete_su_note(id: str) -> str:
    """Mark a SU note as done.

    Args:
        id: Note ID to complete.
    """
    await SuNoteRepo.complete(id)
    log.info("su_notes.completed", note_id=id)
    return json.dumps({"completed": id})
