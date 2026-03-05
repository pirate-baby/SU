"""
Unsubscribe Manager: tracks which senders SU has unsubscribed from, exposed
as plain async functions registered on pydantic-ai agents via @agent.tool_plain.
"""
import json
from typing import Any

from app.logger import get_logger
from app.repositories import UnsubscribedSenderRepo

log = get_logger(__name__)


async def check_unsubscribed(sender_email: str) -> str:
    """Check if we've already unsubscribed from a sender email address.

    Args:
        sender_email: The sender email address to check.
    """
    result = await UnsubscribedSenderRepo.is_unsubscribed(sender_email)
    return json.dumps({"already_unsubscribed": result, "sender_email": sender_email})


async def record_unsubscribe(
    sender_email: str,
    sender_domain: str,
    unsubscribe_method: str,
    unsubscribe_target: str | None = None,
    status: str = "completed",
    error: str | None = None,
) -> str:
    """Record that we unsubscribed (or attempted to unsubscribe) from a sender.

    Args:
        sender_email: The sender email address.
        sender_domain: The sender's domain (e.g. 'newsletter.example.com').
        unsubscribe_method: How: mailto, https, body_link.
        unsubscribe_target: The URL or email address used to unsubscribe.
        status: Whether it succeeded: completed or failed.
        error: Error message if the unsubscribe failed.
    """
    await UnsubscribedSenderRepo.record(
        sender_email=sender_email,
        sender_domain=sender_domain,
        unsubscribe_method=unsubscribe_method,
        unsubscribe_target=unsubscribe_target,
        status=status,
        error=error,
    )
    log.info(
        "unsubscribe.recorded",
        sender=sender_email,
        method=unsubscribe_method,
        status=status,
    )
    return json.dumps({"recorded": sender_email})


async def list_unsubscribed(limit: int = 200) -> str:
    """List all senders we've unsubscribed from.

    Args:
        limit: Max results (default 200).
    """
    results = await UnsubscribedSenderRepo.list_all(limit=limit)
    return json.dumps(results, indent=2, default=str)
