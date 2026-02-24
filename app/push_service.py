"""
Web Push notification delivery.

Sends push notifications to subscribed browsers when interjections
cannot be delivered via WebSocket (i.e. no active connection).
"""
from __future__ import annotations

import json
from typing import Any

from pywebpush import webpush, WebPushException

from app.config import settings
from app.logger import get_logger
from app.repositories import PushSubscriptionRepo

log = get_logger(__name__)


async def send_push_notification(interjection: dict[str, Any]) -> int:
    """Send a push notification for an interjection to all subscribed browsers.

    Returns the number of successful deliveries.
    """
    if not settings.vapid_private_key or not settings.vapid_claims_email:
        log.warning("push.not_configured")
        return 0

    subscriptions = await PushSubscriptionRepo.list_all()
    if not subscriptions:
        return 0

    payload = json.dumps({
        "title": "SU",
        "body": interjection["content"],
        "tag": f"interjection-{interjection['id']}",
        "interjection_id": interjection["id"],
        "url": "/",
    })

    vapid_claims = {"sub": f"mailto:{settings.vapid_claims_email}"}
    delivered = 0
    expired: list[str] = []

    for sub in subscriptions:
        subscription_info = json.loads(sub["subscription_json"])
        try:
            webpush(
                subscription_info=subscription_info,
                data=payload,
                vapid_private_key=settings.vapid_private_key,
                vapid_claims=vapid_claims,
            )
            delivered += 1
        except WebPushException as e:
            status_code = e.response.status_code if e.response is not None else None
            if status_code in (404, 410):
                # Subscription expired or unsubscribed — clean up
                expired.append(sub["id"])
                log.info("push.subscription_expired", sub_id=sub["id"])
            else:
                log.warning("push.send_failed", sub_id=sub["id"], status=status_code, error=str(e))
        except Exception:
            log.exception("push.send_error", sub_id=sub["id"])

    for sub_id in expired:
        await PushSubscriptionRepo.delete(sub_id)

    if delivered:
        log.info("push.sent", interjection_id=interjection["id"], delivered=delivered)

    return delivered
