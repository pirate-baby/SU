"""
SU Notes Manager MCP server: exposes CRUD for SU's internal notes-to-self.

These are SU's private operational notes — not user-facing tasks. Daemon agents
use them to coordinate across time: "remind him tomorrow", "I already asked and
he dismissed", "check back after 10am".

Used by: note_processor, email_scanner, daily_review daemons.
"""
import json
from typing import Any

import inspect
from mcp.server import Server as _McpServer

# Monkey-patch: mcp 0.9.x+ removed the `version` kwarg from Server.__init__,
# but claude-agent-sdk's create_sdk_mcp_server still passes it.
_orig_server_init = _McpServer.__init__
if "version" not in inspect.signature(_orig_server_init).parameters:
    def _patched_server_init(self, name, **kwargs):
        version = kwargs.pop("version", "1.0.0")
        _orig_server_init(self, name, **kwargs)
        self.version = version
    _McpServer.__init__ = _patched_server_init

from claude_agent_sdk import create_sdk_mcp_server, tool

from app.config import settings
from app.logger import get_logger
from app.repositories import SuNoteRepo

log = get_logger(__name__)


def _json_response(data: Any, is_error: bool = False) -> dict[str, Any]:
    """Build an MCP-formatted tool response."""
    result: dict[str, Any] = {
        "content": [{"type": "text", "text": json.dumps(data, indent=2, default=str)}],
    }
    if is_error:
        result["is_error"] = True
    return result


# ---------------------------------------------------------------------------
# SU Note tools
# ---------------------------------------------------------------------------

@tool(
    "create_su_note",
    (
        f"Create an internal note-to-self for {settings.su_name}. "
        "These are NOT user tasks — they are SU's private operational notes "
        "for daemon coordination. Use activate_after to schedule when the note "
        "should be picked up. Use context_json for rich structured context."
    ),
    {
        "type": "object",
        "properties": {
            "content": {
                "type": "string",
                "description": "What SU wants to remember or do",
            },
            "note_type": {
                "type": "string",
                "enum": ["todo", "reminder", "observation", "log"],
                "description": "Type of note: todo (action needed), reminder (time-sensitive alert), observation (something noticed), log (record of what happened)",
            },
            "priority": {
                "type": "string",
                "enum": ["low", "normal", "high", "urgent"],
                "description": "How important this note is",
            },
            "activate_after": {
                "type": "string",
                "description": "ISO datetime: don't act on this note before this time. Use for snoozing or scheduling future actions.",
            },
            "related_task_id": {
                "type": "string",
                "description": "Link to a user task this note relates to",
            },
            "related_interjection_id": {
                "type": "string",
                "description": "Link to an interjection this note relates to",
            },
            "source": {
                "type": "string",
                "description": "Which daemon created this: email_scanner, note_processor, daily_review, etc.",
            },
            "context_json": {
                "type": "string",
                "description": "JSON string with structured context (email subject, deadline, attempt history, etc.)",
            },
        },
        "required": ["content"],
    },
)
async def create_su_note(args: dict[str, Any]) -> dict[str, Any]:
    note = await SuNoteRepo.create(
        content=args["content"],
        note_type=args.get("note_type", "todo"),
        priority=args.get("priority", "normal"),
        activate_after=args.get("activate_after"),
        related_task_id=args.get("related_task_id"),
        related_interjection_id=args.get("related_interjection_id"),
        source=args.get("source"),
        context_json=args.get("context_json"),
    )
    log.info("su_notes.created", note_id=note.id, note_type=note.note_type, source=note.source)
    return _json_response(note.model_dump(exclude_none=True))


@tool(
    "get_su_note",
    "Read a single SU note with full context.",
    {
        "type": "object",
        "properties": {
            "id": {"type": "string", "description": "Note ID"},
        },
        "required": ["id"],
    },
)
async def get_su_note(args: dict[str, Any]) -> dict[str, Any]:
    note = await SuNoteRepo.get(args["id"])
    if not note:
        return _json_response({"error": "Note not found"}, is_error=True)
    return _json_response(note)


@tool(
    "list_su_notes",
    (
        "List SU's internal notes. By default returns active notes that are "
        "ready to act on (activate_after has passed). Set include_snoozed=true "
        "to include notes not yet due."
    ),
    {
        "type": "object",
        "properties": {
            "status": {
                "type": "string",
                "enum": ["active", "done", "snoozed", "cancelled"],
                "description": "Filter by status (default: active)",
            },
            "note_type": {
                "type": "string",
                "enum": ["todo", "reminder", "observation", "log"],
                "description": "Filter by note type",
            },
            "source": {
                "type": "string",
                "description": "Filter by source daemon",
            },
            "include_snoozed": {
                "type": "boolean",
                "description": "If false (default), only return notes whose activate_after has passed or is null. If true, return all matching notes regardless of activate_after.",
            },
            "limit": {"type": "integer", "description": "Max results (default 50)"},
        },
    },
)
async def list_su_notes(args: dict[str, Any]) -> dict[str, Any]:
    include_snoozed = args.get("include_snoozed", False)

    if not include_snoozed and args.get("status", "active") == "active":
        # Default: only notes ready to act on
        notes = await SuNoteRepo.list_active_due(limit=args.get("limit", 50))
    else:
        notes = await SuNoteRepo.list(
            status=args.get("status"),
            note_type=args.get("note_type"),
            source=args.get("source"),
            limit=args.get("limit", 50),
        )
    return _json_response(notes)


@tool(
    "update_su_note",
    (
        "Update an existing SU note. Use this to snooze (set activate_after), "
        "change priority, update content/context, or change status."
    ),
    {
        "type": "object",
        "properties": {
            "id": {"type": "string", "description": "Note ID to update"},
            "content": {"type": "string", "description": "Updated content"},
            "note_type": {
                "type": "string",
                "enum": ["todo", "reminder", "observation", "log"],
            },
            "status": {
                "type": "string",
                "enum": ["active", "done", "snoozed", "cancelled"],
            },
            "priority": {
                "type": "string",
                "enum": ["low", "normal", "high", "urgent"],
            },
            "activate_after": {
                "type": "string",
                "description": "Snooze until this ISO datetime",
            },
            "context_json": {
                "type": "string",
                "description": "Updated context JSON",
            },
        },
        "required": ["id"],
    },
)
async def update_su_note(args: dict[str, Any]) -> dict[str, Any]:
    note_id = args["id"]
    fields = {k: v for k, v in args.items() if k != "id"}
    if not fields:
        return _json_response({"error": "No fields to update"}, is_error=True)
    await SuNoteRepo.update(note_id, **fields)
    log.info("su_notes.updated", note_id=note_id)
    return _json_response({"updated": note_id})


@tool(
    "complete_su_note",
    "Mark a SU note as done.",
    {
        "type": "object",
        "properties": {
            "id": {"type": "string", "description": "Note ID to complete"},
        },
        "required": ["id"],
    },
)
async def complete_su_note(args: dict[str, Any]) -> dict[str, Any]:
    note_id = args["id"]
    await SuNoteRepo.complete(note_id)
    log.info("su_notes.completed", note_id=note_id)
    return _json_response({"completed": note_id})


# ---------------------------------------------------------------------------
# MCP server instance
# ---------------------------------------------------------------------------

su_notes_mcp_server = create_sdk_mcp_server(
    name="su_notes_manager",
    tools=[
        create_su_note, get_su_note, list_su_notes,
        update_su_note, complete_su_note,
    ],
)
