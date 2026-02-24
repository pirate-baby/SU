"""
Background scheduler: periodic tasks for proactive SU behavior.

Phase 1 jobs (pure SQLite, no subagent):
  - calendar_check: remind about upcoming events
  - interjection_delivery: push pending interjections to connected clients

Phase 2 jobs (future, spawn subagents):
  - morning_brief, evening_review, email_scan
"""
import asyncio
from datetime import datetime, timedelta
from typing import Any, Callable, Coroutine, Optional

from app.database import get_db
from app.logger import get_logger

log = get_logger(__name__)

# Track which events we've already reminded about (in-memory; resets on restart)
_reminded_event_ids: set[str] = set()


class Scheduler:
    """Manages periodic background tasks for proactive behavior."""

    def __init__(self):
        self._tasks: dict[str, asyncio.Task] = {}
        self._running = False
        self._push_fn: Optional[Callable[[dict[str, Any]], Coroutine]] = None

    async def start(
        self,
        push_interjection_fn: Callable[[dict[str, Any]], Coroutine],
    ) -> None:
        """Start all periodic jobs. Called from FastAPI lifespan."""
        self._running = True
        self._push_fn = push_interjection_fn

        self._tasks["calendar_check"] = asyncio.create_task(
            self._periodic("calendar_check", self._calendar_check, interval=1800),
            name="sched-calendar-check",
        )
        self._tasks["interjection_delivery"] = asyncio.create_task(
            self._periodic("interjection_delivery", self._deliver_interjections, interval=60),
            name="sched-interjection-delivery",
        )

        log.info("scheduler.started", jobs=list(self._tasks.keys()))

    async def stop(self) -> None:
        """Cancel all periodic jobs. Called from FastAPI lifespan shutdown."""
        self._running = False
        for name, task in self._tasks.items():
            task.cancel()
            log.debug("scheduler.job_cancelled", job=name)
        self._tasks.clear()
        log.info("scheduler.stopped")

    async def _periodic(
        self,
        name: str,
        func: Callable[[], Coroutine],
        interval: int,
    ) -> None:
        """Run *func* every *interval* seconds until stopped."""
        # Initial delay: let the app finish starting up
        await asyncio.sleep(5)

        while self._running:
            try:
                await func()
            except asyncio.CancelledError:
                break
            except Exception:
                log.exception("scheduler.job_error", job=name)

            try:
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                break

    # ------------------------------------------------------------------
    # Phase 1 Jobs
    # ------------------------------------------------------------------

    async def _calendar_check(self) -> None:
        """Check for upcoming events and create reminder interjections."""
        now = datetime.utcnow()

        async with get_db() as db:
            # Find events starting within their reminder window
            cursor = await db.execute(
                """SELECT * FROM events
                   WHERE start_time > ?
                   ORDER BY start_time ASC
                   LIMIT 50""",
                (now.isoformat(),),
            )
            rows = await cursor.fetchall()

            created = 0
            for row in rows:
                event = dict(row)
                event_id = event["id"]
                if event_id in _reminded_event_ids:
                    continue

                start = datetime.fromisoformat(event["start_time"])
                reminder_min = event.get("reminder_minutes") or 30
                remind_at = start - timedelta(minutes=reminder_min)

                if now >= remind_at:
                    # Time to remind
                    minutes_until = max(0, int((start - now).total_seconds() / 60))
                    if minutes_until <= 0:
                        time_str = "starting now"
                    elif minutes_until < 60:
                        time_str = f"in {minutes_until} minutes"
                    else:
                        hours = minutes_until // 60
                        time_str = f"in {hours} hour{'s' if hours > 1 else ''}"

                    location = f" at {event['location']}" if event.get("location") else ""
                    content = (
                        f"Sir, a reminder: \"{event['title']}\"{location} "
                        f"is {time_str}."
                    )

                    import uuid
                    interjection_id = str(uuid.uuid4())
                    await db.execute(
                        """INSERT INTO interjections
                           (id, content, urgency, source, related_event_id, status, created_at)
                           VALUES (?, ?, ?, 'calendar_check', ?, 'pending', ?)""",
                        (interjection_id, content, "high", event_id, now.isoformat()),
                    )
                    _reminded_event_ids.add(event_id)
                    created += 1

            if created:
                await db.commit()
                log.info("scheduler.calendar_reminders_created", count=created)

    async def _deliver_interjections(self) -> None:
        """Push pending interjections to connected WebSocket clients."""
        if not self._push_fn:
            return

        async with get_db() as db:
            cursor = await db.execute(
                "SELECT * FROM interjections WHERE status = 'pending' ORDER BY created_at ASC LIMIT 10"
            )
            rows = await cursor.fetchall()

            if not rows:
                return

            now = datetime.utcnow().isoformat()
            for row in rows:
                item = dict(row)
                try:
                    await self._push_fn(item)
                    await db.execute(
                        "UPDATE interjections SET status = 'delivered', delivered_at = ? WHERE id = ?",
                        (now, item["id"]),
                    )
                    log.info("scheduler.interjection_delivered", id=item["id"])
                except Exception:
                    log.exception("scheduler.interjection_push_failed", id=item["id"])
                    break

            await db.commit()


# Module-level singleton
scheduler = Scheduler()
