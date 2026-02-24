"""
Life Manager MCP server: exposes task, event, and interjection CRUD as MCP
tools so Claude can manage the master's schedule and tasks during conversation.

Also used by background scheduler agents to read/write operational state.
"""
import json
import uuid
from datetime import datetime
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

from app.database import get_db
from app.logger import get_logger

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Task tools
# ---------------------------------------------------------------------------

@tool(
    "create_task",
    "Create a new task for the master. Returns the created task.",
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
    task_id = str(uuid.uuid4())
    title = args["title"]
    now = datetime.utcnow().isoformat()

    async with get_db() as db:
        await db.execute(
            """INSERT INTO tasks
               (id, title, description, priority, category, due_date, due_time,
                source, source_ref, parent_task_id, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                task_id,
                title,
                args.get("description"),
                args.get("priority", 3),
                args.get("category"),
                args.get("due_date"),
                args.get("due_time"),
                args.get("source", "manual"),
                args.get("source_ref"),
                args.get("parent_task_id"),
                now,
                now,
            ),
        )
        await db.commit()

    log.info("life_manager.task_created", task_id=task_id, title=title)
    result = {"id": task_id, "title": title, "status": "pending", **{
        k: args[k] for k in args if k != "title"
    }}
    return {"content": [{"type": "text", "text": json.dumps(result, indent=2)}]}


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
    updatable = ["title", "description", "status", "priority", "category",
                 "due_date", "due_time"]
    sets = []
    vals = []
    for field in updatable:
        if field in args:
            sets.append(f"{field} = ?")
            vals.append(args[field])

    if not sets:
        return {"content": [{"type": "text", "text": json.dumps({"error": "No fields to update"})}], "is_error": True}

    # If marking done, set completed_at
    if args.get("status") == "done":
        sets.append("completed_at = ?")
        vals.append(datetime.utcnow().isoformat())

    sets.append("updated_at = ?")
    vals.append(datetime.utcnow().isoformat())
    vals.append(task_id)

    async with get_db() as db:
        await db.execute(
            f"UPDATE tasks SET {', '.join(sets)} WHERE id = ?", vals
        )
        await db.commit()

    log.info("life_manager.task_updated", task_id=task_id)
    return {"content": [{"type": "text", "text": json.dumps({"updated": task_id})}]}


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
    clauses: list[str] = []
    params: list[Any] = []

    if "status" in args:
        clauses.append("status = ?")
        params.append(args["status"])
    if "category" in args:
        clauses.append("category = ?")
        params.append(args["category"])
    if "due_before" in args:
        clauses.append("due_date <= ?")
        params.append(args["due_before"])
    if "due_after" in args:
        clauses.append("due_date >= ?")
        params.append(args["due_after"])
    if "priority" in args:
        clauses.append("priority = ?")
        params.append(args["priority"])

    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    limit = args.get("limit", 50)
    query = f"SELECT * FROM tasks {where} ORDER BY priority ASC, due_date ASC LIMIT ?"
    params.append(limit)

    async with get_db() as db:
        cursor = await db.execute(query, params)
        rows = await cursor.fetchall()
        tasks = [dict(row) for row in rows]

    return {"content": [{"type": "text", "text": json.dumps(tasks, indent=2, default=str)}]}


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
    now = datetime.utcnow().isoformat()

    async with get_db() as db:
        await db.execute(
            "UPDATE tasks SET status = 'done', completed_at = ?, updated_at = ? WHERE id = ?",
            (now, now, task_id),
        )
        await db.commit()

    log.info("life_manager.task_completed", task_id=task_id)
    return {"content": [{"type": "text", "text": json.dumps({"completed": task_id})}]}


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

    async with get_db() as db:
        await db.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
        await db.commit()

    log.info("life_manager.task_deleted", task_id=task_id)
    return {"content": [{"type": "text", "text": json.dumps({"deleted": task_id})}]}


# ---------------------------------------------------------------------------
# Event tools
# ---------------------------------------------------------------------------

@tool(
    "create_event",
    "Create a calendar event for the master.",
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
    event_id = str(uuid.uuid4())
    now = datetime.utcnow().isoformat()

    async with get_db() as db:
        await db.execute(
            """INSERT INTO events
               (id, title, description, start_time, end_time, all_day,
                location, source, source_ref, reminder_minutes,
                created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                event_id,
                args["title"],
                args.get("description"),
                args["start_time"],
                args.get("end_time"),
                1 if args.get("all_day") else 0,
                args.get("location"),
                args.get("source", "manual"),
                args.get("source_ref"),
                args.get("reminder_minutes", 30),
                now,
                now,
            ),
        )
        await db.commit()

    log.info("life_manager.event_created", event_id=event_id, title=args["title"])
    result = {"id": event_id, "title": args["title"], "start_time": args["start_time"]}
    return {"content": [{"type": "text", "text": json.dumps(result, indent=2)}]}


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
    updatable = ["title", "description", "start_time", "end_time", "all_day",
                 "location", "reminder_minutes"]
    sets = []
    vals = []
    for field in updatable:
        if field in args:
            val = args[field]
            if field == "all_day":
                val = 1 if val else 0
            sets.append(f"{field} = ?")
            vals.append(val)

    if not sets:
        return {"content": [{"type": "text", "text": json.dumps({"error": "No fields to update"})}], "is_error": True}

    sets.append("updated_at = ?")
    vals.append(datetime.utcnow().isoformat())
    vals.append(event_id)

    async with get_db() as db:
        await db.execute(
            f"UPDATE events SET {', '.join(sets)} WHERE id = ?", vals
        )
        await db.commit()

    log.info("life_manager.event_updated", event_id=event_id)
    return {"content": [{"type": "text", "text": json.dumps({"updated": event_id})}]}


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
    clauses: list[str] = []
    params: list[Any] = []

    if "start_after" in args:
        clauses.append("start_time >= ?")
        params.append(args["start_after"])
    if "start_before" in args:
        clauses.append("start_time <= ?")
        params.append(args["start_before"])

    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    limit = args.get("limit", 50)
    query = f"SELECT * FROM events {where} ORDER BY start_time ASC LIMIT ?"
    params.append(limit)

    async with get_db() as db:
        cursor = await db.execute(query, params)
        rows = await cursor.fetchall()
        events = [dict(row) for row in rows]

    return {"content": [{"type": "text", "text": json.dumps(events, indent=2, default=str)}]}


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

    async with get_db() as db:
        await db.execute("DELETE FROM events WHERE id = ?", (event_id,))
        await db.commit()

    log.info("life_manager.event_deleted", event_id=event_id)
    return {"content": [{"type": "text", "text": json.dumps({"deleted": event_id})}]}


# ---------------------------------------------------------------------------
# Interjection tools
# ---------------------------------------------------------------------------

@tool(
    "create_interjection",
    "Queue a message from SU to the master. Used by background processes "
    "to proactively reach out — reminders, observations, coaching.",
    {
        "type": "object",
        "properties": {
            "content": {
                "type": "string",
                "description": "The message SU wants to deliver to the master",
            },
            "urgency": {
                "type": "string",
                "enum": ["low", "normal", "high", "urgent"],
                "description": "How urgently the master should see this",
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
    interjection_id = str(uuid.uuid4())
    now = datetime.utcnow().isoformat()

    async with get_db() as db:
        await db.execute(
            """INSERT INTO interjections
               (id, content, urgency, source, related_task_id, related_event_id,
                status, created_at)
               VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)""",
            (
                interjection_id,
                args["content"],
                args.get("urgency", "normal"),
                args.get("source"),
                args.get("related_task_id"),
                args.get("related_event_id"),
                now,
            ),
        )
        await db.commit()

    log.info("life_manager.interjection_created", id=interjection_id,
             urgency=args.get("urgency", "normal"), source=args.get("source"))
    return {"content": [{"type": "text", "text": json.dumps({"id": interjection_id, "status": "pending"})}]}


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
    clauses: list[str] = []
    params: list[Any] = []

    if "status" in args:
        clauses.append("status = ?")
        params.append(args["status"])

    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    limit = args.get("limit", 20)
    query = f"SELECT * FROM interjections {where} ORDER BY created_at DESC LIMIT ?"
    params.append(limit)

    async with get_db() as db:
        cursor = await db.execute(query, params)
        rows = await cursor.fetchall()
        items = [dict(row) for row in rows]

    return {"content": [{"type": "text", "text": json.dumps(items, indent=2, default=str)}]}


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
