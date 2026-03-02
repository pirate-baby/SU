"""
Unsubscribe Manager MCP server: tracks which senders SU has unsubscribed from.

Used by the email scanner (to skip already-handled senders) and the email
unsubscriber daemon (to record completed unsubscribe actions).
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

from app.logger import get_logger
from app.repositories import UnsubscribedSenderRepo

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
# Tools
# ---------------------------------------------------------------------------

@tool(
    "check_unsubscribed",
    "Check if we've already unsubscribed from a sender email address.",
    {
        "type": "object",
        "properties": {
            "sender_email": {
                "type": "string",
                "description": "The sender email address to check",
            },
        },
        "required": ["sender_email"],
    },
)
async def check_unsubscribed(args: dict[str, Any]) -> dict[str, Any]:
    result = await UnsubscribedSenderRepo.is_unsubscribed(args["sender_email"])
    return _json_response({"already_unsubscribed": result, "sender_email": args["sender_email"]})


@tool(
    "record_unsubscribe",
    "Record that we unsubscribed (or attempted to unsubscribe) from a sender.",
    {
        "type": "object",
        "properties": {
            "sender_email": {
                "type": "string",
                "description": "The sender email address",
            },
            "sender_domain": {
                "type": "string",
                "description": "The sender's domain (e.g. 'newsletter.example.com')",
            },
            "unsubscribe_method": {
                "type": "string",
                "enum": ["mailto", "https", "body_link"],
                "description": "How the unsubscribe was performed",
            },
            "unsubscribe_target": {
                "type": "string",
                "description": "The URL or email address used to unsubscribe",
            },
            "status": {
                "type": "string",
                "enum": ["completed", "failed"],
                "description": "Whether the unsubscribe succeeded or failed",
            },
            "error": {
                "type": "string",
                "description": "Error message if the unsubscribe failed",
            },
        },
        "required": ["sender_email", "sender_domain", "unsubscribe_method"],
    },
)
async def record_unsubscribe(args: dict[str, Any]) -> dict[str, Any]:
    await UnsubscribedSenderRepo.record(
        sender_email=args["sender_email"],
        sender_domain=args["sender_domain"],
        unsubscribe_method=args["unsubscribe_method"],
        unsubscribe_target=args.get("unsubscribe_target"),
        status=args.get("status", "completed"),
        error=args.get("error"),
    )
    log.info(
        "unsubscribe.recorded",
        sender=args["sender_email"],
        method=args["unsubscribe_method"],
        status=args.get("status", "completed"),
    )
    return _json_response({"recorded": args["sender_email"]})


@tool(
    "list_unsubscribed",
    "List all senders we've unsubscribed from.",
    {
        "type": "object",
        "properties": {
            "limit": {
                "type": "integer",
                "description": "Max results (default 200)",
            },
        },
    },
)
async def list_unsubscribed(args: dict[str, Any]) -> dict[str, Any]:
    results = await UnsubscribedSenderRepo.list_all(limit=args.get("limit", 200))
    return _json_response(results)


# ---------------------------------------------------------------------------
# MCP server instance
# ---------------------------------------------------------------------------

unsubscribe_manager_mcp_server = create_sdk_mcp_server(
    name="unsubscribe_manager",
    tools=[check_unsubscribed, record_unsubscribe, list_unsubscribed],
)
