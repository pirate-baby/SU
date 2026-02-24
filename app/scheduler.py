"""
Background scheduler: periodic tasks for proactive SU behavior.

Jobs:
  - calendar_check: detect upcoming events and spawn a subagent to compose
    contextual reminders (interjections) for delivery.
  - interjection_delivery: push pending interjections to connected clients.
"""
import asyncio
from datetime import datetime, timedelta
from typing import Any, Callable, Coroutine, Optional

from app.config import settings
from app.logger import get_logger
from app.repositories import EventRepo, InterjectionRepo

log = get_logger(__name__)

# Track which events we've already reminded about (in-memory; resets on restart)
_reminded_event_ids: set[str] = set()


class Scheduler:
    """Manages periodic background tasks for proactive behavior."""

    def __init__(self):
        self._tasks: dict[str, asyncio.Task] = {}
        self._running = False
        self._push_fn: Optional[Callable[[dict[str, Any]], Coroutine]] = None
        self._ready = asyncio.Event()

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

        # Signal periodic loops that startup is complete and they may begin
        self._ready.set()
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
        # Wait until start() signals that the app is fully initialized
        await self._ready.wait()

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
    # Jobs
    # ------------------------------------------------------------------

    async def _calendar_check(self) -> None:
        """Check for upcoming events, spawn a subagent to compose reminders."""
        now = datetime.utcnow()
        rows = await EventRepo.upcoming_within_window(now)

        events_to_remind: list[dict[str, Any]] = []
        for event in rows:
            event_id = event["id"]
            if event_id in _reminded_event_ids:
                continue

            start = datetime.fromisoformat(event["start_time"])
            reminder_min = event.get("reminder_minutes") or 30
            remind_at = start - timedelta(minutes=reminder_min)

            if now >= remind_at:
                events_to_remind.append(event)
                _reminded_event_ids.add(event_id)

        if not events_to_remind:
            return

        log.info("scheduler.calendar_events_due", count=len(events_to_remind))
        await self._compose_calendar_interjections(events_to_remind, now)

    async def _compose_calendar_interjections(
        self,
        events: list[dict[str, Any]],
        now: datetime,
    ) -> None:
        """Spawn a subagent to compose natural-language reminders for upcoming events."""
        from claude_agent_sdk import (
            ClaudeAgentOptions,
            ClaudeSDKClient,
            AssistantMessage,
            ResultMessage,
            TextBlock,
        )
        from app.memory_manager import get_basic_memory_mcp_config
        from app.life_manager import life_manager_mcp_server
        from app.process_limiter import claude_process_slot

        # Build a summary of the events needing reminders
        event_summaries: list[str] = []
        for event in events:
            start = datetime.fromisoformat(event["start_time"])
            minutes_until = max(0, int((start - now).total_seconds() / 60))
            if minutes_until <= 0:
                time_str = "starting now"
            elif minutes_until < 60:
                time_str = f"in {minutes_until} minutes"
            else:
                hours = minutes_until // 60
                remaining = minutes_until % 60
                time_str = f"in {hours}h{remaining}m"

            location = f" at {event['location']}" if event.get("location") else ""
            desc = f" — {event['description']}" if event.get("description") else ""
            event_summaries.append(
                f"- \"{event['title']}\"{location}{desc} ({time_str})"
            )

        prompt = (
            "Upcoming events:\n\n"
            + "\n".join(event_summaries) + "\n\n"
            "Check the knowledge base for relevant context on any of these. "
            "Queue a reminder for each via create_interjection (urgency='high', "
            "source='calendar_check'). Keep reminders short and useful — "
            "one or two sentences, no fluff."
        )

        system_prompt = (
            "You compose calendar reminders for SU. Look up context in the "
            "knowledge base when it's useful, then queue each reminder with "
            "create_interjection. Be terse. Headless — no clarifying questions."
        )

        options = ClaudeAgentOptions(
            mcp_servers={
                "basic_memory": get_basic_memory_mcp_config(),
                "life_manager": life_manager_mcp_server,
            },
            allowed_tools=[
                "mcp__basic_memory__search_notes",
                "mcp__basic_memory__read_note",
                "mcp__life_manager__create_interjection",
                "mcp__life_manager__list_tasks",
            ],
            disallowed_tools=[
                "Task", "Bash", "Glob", "Grep", "Read", "Edit", "Write",
                "WebFetch", "WebSearch", "NotebookEdit",
            ],
            permission_mode="bypassPermissions",
            max_turns=10,
            system_prompt=system_prompt,
        )

        try:
            async with claude_process_slot(timeout=90), ClaudeSDKClient(options=options) as client:
                await client.query(prompt)
                async for message in client.receive_response():
                    if isinstance(message, ResultMessage) and message.is_error:
                        log.warning(
                            "scheduler.calendar_agent_error",
                            result=message.result or "unknown",
                        )
                        return

            log.info("scheduler.calendar_agent_completed", event_count=len(events))
        except Exception:
            log.exception("scheduler.calendar_agent_failed")
            # Fallback: create basic text interjections so reminders aren't lost
            for event in events:
                start = datetime.fromisoformat(event["start_time"])
                minutes_until = max(0, int((start - now).total_seconds() / 60))
                if minutes_until <= 0:
                    time_str = "starting now"
                elif minutes_until < 60:
                    time_str = f"in {minutes_until} minutes"
                else:
                    hours = minutes_until // 60
                    time_str = f"in about {hours} hour{'s' if hours > 1 else ''}"
                location = f" at {event['location']}" if event.get("location") else ""
                await InterjectionRepo.create(
                    content=f"{settings.user_name}, a reminder: \"{event['title']}\"{location} is {time_str}.",
                    urgency="high",
                    source="calendar_check",
                    related_event_id=event["id"],
                )
            log.info("scheduler.calendar_fallback_created", count=len(events))

    async def _deliver_interjections(self) -> None:
        """Push pending interjections to connected WebSocket clients.

        Falls back to Web Push notifications when no WebSocket clients
        are connected.
        """
        if not self._push_fn:
            return

        items = await InterjectionRepo.pending()
        if not items:
            return

        for item in items:
            try:
                ws_delivered = await self._push_fn(item)

                # Fall back to Web Push if nobody got it via WebSocket
                if ws_delivered == 0:
                    try:
                        from app.push_service import send_push_notification
                        push_delivered = await send_push_notification(item)
                        log.info(
                            "scheduler.interjection_push_fallback",
                            id=item["id"],
                            push_delivered=push_delivered,
                        )
                    except Exception:
                        log.exception("scheduler.web_push_failed", id=item["id"])

                await InterjectionRepo.mark_delivered(item["id"])
                log.info(
                    "scheduler.interjection_delivered",
                    id=item["id"],
                    ws_delivered=ws_delivered,
                )
            except Exception:
                log.exception("scheduler.interjection_push_failed", id=item["id"])
                break


# Module-level singleton
scheduler = Scheduler()
