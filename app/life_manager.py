"""
Life Manager MCP server: exposes task, event, and interjection CRUD as MCP
tools so Claude can manage the user's schedule and tasks during conversation.

Also used by background scheduler agents to read/write operational state.
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
from app.repositories import TaskRepo, EventRepo, InterjectionRepo

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
# Task tools
# ---------------------------------------------------------------------------

@tool(
    "create_task",
    f"Create a new task for {settings.user_name}. Returns the created task.",
    {
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "Task title"},
            "description": {"type": "string", "description": "Optional details"},
            "priority": {
                "type": "integer",
                "description": "1=urgent, 2=high, 3=normal, 4=low",
                "enum": [1, 2, 3, 4],
            },
            "category": {
                "type": "string",
                "description": "Category: work, personal, health, finance, errands, etc.",
            },
            "due_date": {
                "type": "string",
                "description": "Due date in ISO-8601 format (YYYY-MM-DD)",
            },
            "due_time": {
                "type": "string",
                "description": "Due time in HH:MM format (optional)",
            },
            "source": {
                "type": "string",
                "description": "Where this task came from: manual, email, su_inferred",
            },
            "source_ref": {
                "type": "string",
                "description": "Reference URL or identifier for the source",
            },
            "parent_task_id": {
                "type": "string",
                "description": "Parent task ID for subtasks",
            },
        },
        "required": ["title"],
    },
)
async def create_task(args: dict[str, Any]) -> dict[str, Any]:
    task = await TaskRepo.create(
        title=args["title"],
        description=args.get("description"),
        priority=args.get("priority", 3),
        category=args.get("category"),
        due_date=args.get("due_date"),
        due_time=args.get("due_time"),
        source=args.get("source", "manual"),
        source_ref=args.get("source_ref"),
        parent_task_id=args.get("parent_task_id"),
    )
    log.info("life_manager.task_created", task_id=task.id, title=task.title)
    return _json_response(task.model_dump(exclude_none=True))


@tool(
    "update_task",
    "Update an existing task. Only provided fields are changed.",
    {
        "type": "object",
        "properties": {
            "id": {"type": "string", "description": "Task ID to update"},
            "title": {"type": "string"},
            "description": {"type": "string"},
            "status": {
                "type": "string",
                "enum": ["pending", "in_progress", "done", "cancelled"],
            },
            "priority": {"type": "integer", "enum": [1, 2, 3, 4]},
            "category": {"type": "string"},
            "due_date": {"type": "string"},
            "due_time": {"type": "string"},
        },
        "required": ["id"],
    },
)
async def update_task(args: dict[str, Any]) -> dict[str, Any]:
    task_id = args["id"]
    fields = {k: v for k, v in args.items() if k != "id"}
    if not fields:
        return _json_response({"error": "No fields to update"}, is_error=True)
    await TaskRepo.update(task_id, **fields)
    log.info("life_manager.task_updated", task_id=task_id)
    return _json_response({"updated": task_id})


@tool(
    "list_tasks",
    "List tasks with optional filters. Returns matching tasks sorted by priority then due date.",
    {
        "type": "object",
        "properties": {
            "status": {
                "type": "string",
                "description": "Filter by status: pending, in_progress, done, cancelled",
            },
            "category": {"type": "string", "description": "Filter by category"},
            "due_before": {
                "type": "string",
                "description": "Only tasks due on or before this date (YYYY-MM-DD)",
            },
            "due_after": {
                "type": "string",
                "description": "Only tasks due on or after this date (YYYY-MM-DD)",
            },
            "priority": {"type": "integer", "description": "Filter by exact priority"},
            "limit": {"type": "integer", "description": "Max results (default 50)"},
        },
    },
)
async def list_tasks(args: dict[str, Any]) -> dict[str, Any]:
    tasks = await TaskRepo.list(
        status=args.get("status"),
        category=args.get("category"),
        due_before=args.get("due_before"),
        due_after=args.get("due_after"),
        priority=args.get("priority"),
        limit=args.get("limit", 50),
    )
    return _json_response(tasks)


@tool(
    "complete_task",
    "Mark a task as done.",
    {
        "type": "object",
        "properties": {
            "id": {"type": "string", "description": "Task ID to complete"},
        },
        "required": ["id"],
    },
)
async def complete_task(args: dict[str, Any]) -> dict[str, Any]:
    task_id = args["id"]
    await TaskRepo.complete(task_id)
    log.info("life_manager.task_completed", task_id=task_id)
    return _json_response({"completed": task_id})


@tool(
    "delete_task",
    "Delete a task permanently.",
    {
        "type": "object",
        "properties": {
            "id": {"type": "string", "description": "Task ID to delete"},
        },
        "required": ["id"],
    },
)
async def delete_task(args: dict[str, Any]) -> dict[str, Any]:
    task_id = args["id"]
    await TaskRepo.delete(task_id)
    log.info("life_manager.task_deleted", task_id=task_id)
    return _json_response({"deleted": task_id})


# ---------------------------------------------------------------------------
# Event tools
# ---------------------------------------------------------------------------

@tool(
    "create_event",
    f"Create a calendar event for {settings.user_name}.",
    {
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "Event title"},
            "start_time": {
                "type": "string",
                "description": "Start time in ISO-8601 format (YYYY-MM-DDTHH:MM)",
            },
            "end_time": {
                "type": "string",
                "description": "End time in ISO-8601 format (optional)",
            },
            "description": {"type": "string"},
            "all_day": {"type": "boolean", "description": "True for all-day events"},
            "location": {"type": "string"},
            "reminder_minutes": {
                "type": "integer",
                "description": "Minutes before event to remind (default 30)",
            },
            "source": {"type": "string"},
            "source_ref": {"type": "string"},
        },
        "required": ["title", "start_time"],
    },
)
async def create_event(args: dict[str, Any]) -> dict[str, Any]:
    event = await EventRepo.create(
        title=args["title"],
        start_time=args["start_time"],
        end_time=args.get("end_time"),
        description=args.get("description"),
        all_day=args.get("all_day", False),
        location=args.get("location"),
        reminder_minutes=args.get("reminder_minutes", 30),
        source=args.get("source", "manual"),
        source_ref=args.get("source_ref"),
    )
    log.info("life_manager.event_created", event_id=event.id, title=event.title)
    return _json_response(event.model_dump(exclude_none=True))


@tool(
    "update_event",
    "Update an existing calendar event. Only provided fields are changed.",
    {
        "type": "object",
        "properties": {
            "id": {"type": "string", "description": "Event ID to update"},
            "title": {"type": "string"},
            "description": {"type": "string"},
            "start_time": {"type": "string"},
            "end_time": {"type": "string"},
            "all_day": {"type": "boolean"},
            "location": {"type": "string"},
            "reminder_minutes": {"type": "integer"},
        },
        "required": ["id"],
    },
)
async def update_event(args: dict[str, Any]) -> dict[str, Any]:
    event_id = args["id"]
    fields = {k: v for k, v in args.items() if k != "id"}
    if not fields:
        return _json_response({"error": "No fields to update"}, is_error=True)
    await EventRepo.update(event_id, **fields)
    log.info("life_manager.event_updated", event_id=event_id)
    return _json_response({"updated": event_id})


@tool(
    "list_events",
    "List calendar events with optional date range filters.",
    {
        "type": "object",
        "properties": {
            "start_after": {
                "type": "string",
                "description": "Only events starting on or after this datetime (ISO-8601)",
            },
            "start_before": {
                "type": "string",
                "description": "Only events starting on or before this datetime (ISO-8601)",
            },
            "limit": {"type": "integer", "description": "Max results (default 50)"},
        },
    },
)
async def list_events(args: dict[str, Any]) -> dict[str, Any]:
    events = await EventRepo.list(
        start_after=args.get("start_after"),
        start_before=args.get("start_before"),
        limit=args.get("limit", 50),
    )
    return _json_response(events)


@tool(
    "delete_event",
    "Delete a calendar event.",
    {
        "type": "object",
        "properties": {
            "id": {"type": "string", "description": "Event ID to delete"},
        },
        "required": ["id"],
    },
)
async def delete_event(args: dict[str, Any]) -> dict[str, Any]:
    event_id = args["id"]
    await EventRepo.delete(event_id)
    log.info("life_manager.event_deleted", event_id=event_id)
    return _json_response({"deleted": event_id})


# ---------------------------------------------------------------------------
# Interjection tools
# ---------------------------------------------------------------------------

@tool(
    "create_interjection",
    f"Queue a message from {settings.su_name} to {settings.user_name}. Used by background processes "
    "to proactively reach out — reminders, observations, coaching.",
    {
        "type": "object",
        "properties": {
            "content": {
                "type": "string",
                "description": f"The message {settings.su_name} wants to deliver to {settings.user_name}",
            },
            "urgency": {
                "type": "string",
                "enum": ["low", "normal", "high", "urgent"],
                "description": f"How urgently {settings.user_name} should see this",
            },
            "source": {
                "type": "string",
                "description": "Which process created this: morning_brief, calendar_check, email_scan, etc.",
            },
            "related_task_id": {"type": "string"},
            "related_event_id": {"type": "string"},
        },
        "required": ["content"],
    },
)
async def create_interjection(args: dict[str, Any]) -> dict[str, Any]:
    interjection = await InterjectionRepo.create(
        content=args["content"],
        urgency=args.get("urgency", "normal"),
        source=args.get("source"),
        related_task_id=args.get("related_task_id"),
        related_event_id=args.get("related_event_id"),
    )
    log.info("life_manager.interjection_created", id=interjection.id,
             urgency=interjection.urgency, source=interjection.source)
    return _json_response({"id": interjection.id, "status": "pending"})


@tool(
    "list_interjections",
    "List interjections, optionally filtered by status.",
    {
        "type": "object",
        "properties": {
            "status": {
                "type": "string",
                "enum": ["pending", "delivered", "dismissed", "acted_on"],
            },
            "limit": {"type": "integer", "description": "Max results (default 20)"},
        },
    },
)
async def list_interjections(args: dict[str, Any]) -> dict[str, Any]:
    items = await InterjectionRepo.list(
        status=args.get("status"),
        limit=args.get("limit", 20),
    )
    return _json_response(items)


# ---------------------------------------------------------------------------
# MCP server instance
# ---------------------------------------------------------------------------

life_manager_mcp_server = create_sdk_mcp_server(
    name="life_manager",
    tools=[
        create_task, update_task, list_tasks, complete_task, delete_task,
        create_event, update_event, list_events, delete_event,
        create_interjection, list_interjections,
    ],
)
