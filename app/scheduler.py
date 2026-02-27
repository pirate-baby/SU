"""
Background scheduler: periodic tasks for proactive SU behavior.

Jobs:
  - calendar_check: detect upcoming events and spawn a subagent to compose
    contextual reminders (interjections) for delivery.
  - interjection_delivery: push pending interjections to connected clients.
  - note_processor: process SU's internal notes-to-self (every 10 min).
  - email_scanner: triage inbox via ProtonMail MCP (every 10 min).
  - daily_review: compose morning brief from tasks/events/notes (once/day).
"""
import asyncio
import json
from datetime import datetime, timedelta
from typing import Any, Callable, Coroutine, Optional

from app.config import settings
from app.daemon_registry import (
    daemon_registry, DaemonInfo, DaemonCategory, RunStatus,
)
from app.logger import get_logger
from app.repositories import EventRepo, InterjectionRepo, SuNoteRepo

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

        # Register all scheduler daemons with the process index
        daemon_registry.register(DaemonInfo(
            name="calendar_check",
            display_name="Calendar Check",
            category=DaemonCategory.SCHEDULER,
            interval_seconds=1800,
            description="Checks upcoming events, spawns subagent for reminders",
        ))
        daemon_registry.register(DaemonInfo(
            name="interjection_delivery",
            display_name="Interjection Delivery",
            category=DaemonCategory.SCHEDULER,
            interval_seconds=60,
            description="Pushes pending interjections to WebSocket/Web Push",
        ))
        daemon_registry.register(DaemonInfo(
            name="note_processor",
            display_name="Note Processor",
            category=DaemonCategory.SCHEDULER,
            interval_seconds=600,
            description="Processes SU notes-to-self, spawns subagent for triage",
        ))
        if settings.protonmail_username and settings.protonmail_password:
            daemon_registry.register(DaemonInfo(
                name="email_scanner",
                display_name="Email Scanner",
                category=DaemonCategory.SCHEDULER,
                interval_seconds=600,
                description="Triages inbox via ProtonMail, creates tasks and notes",
            ))
        daemon_registry.register(DaemonInfo(
            name="daily_review",
            display_name="Daily Review",
            category=DaemonCategory.SCHEDULER,
            interval_seconds=3600,
            condition="Once/day, 6-9am UTC",
            description="Composes morning brief from tasks, events, and notes",
        ))

        self._tasks["calendar_check"] = asyncio.create_task(
            self._periodic("calendar_check", self._calendar_check, interval=1800),
            name="sched-calendar-check",
        )
        self._tasks["interjection_delivery"] = asyncio.create_task(
            self._periodic("interjection_delivery", self._deliver_interjections, interval=60),
            name="sched-interjection-delivery",
        )
        self._tasks["note_processor"] = asyncio.create_task(
            self._periodic("note_processor", self._note_processor, interval=600),
            name="sched-note-processor",
        )

        # Email scanner — only if ProtonMail is configured
        if settings.protonmail_username and settings.protonmail_password:
            self._tasks["email_scanner"] = asyncio.create_task(
                self._periodic("email_scanner", self._email_scanner, interval=600),
                name="sched-email-scanner",
            )

        # Daily review — runs every hour, but only fires once per day
        self._tasks["daily_review"] = asyncio.create_task(
            self._periodic("daily_review", self._daily_review, interval=3600),
            name="sched-daily-review",
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
            run_id = await daemon_registry.start_run(name)
            try:
                await func()
                await daemon_registry.end_run(run_id, name, RunStatus.COMPLETED)
            except asyncio.CancelledError:
                await daemon_registry.end_run(run_id, name, RunStatus.FAILED, error="cancelled")
                break
            except Exception as exc:
                log.exception("scheduler.job_error", job=name)
                await daemon_registry.end_run(run_id, name, RunStatus.FAILED, error=str(exc))

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
            async with claude_process_slot(timeout=90, name="calendar_check"), ClaudeSDKClient(options=options) as client:
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

    # ------------------------------------------------------------------
    # Note Processor: process SU's internal notes-to-self
    # ------------------------------------------------------------------

    async def _note_processor(self) -> None:
        """Process active SU notes whose activate_after has passed."""
        notes = await SuNoteRepo.list_active_due()
        if not notes:
            return

        log.info("scheduler.note_processor_starting", count=len(notes))
        await self._run_note_processor_agent(notes)

    async def _run_note_processor_agent(self, notes: list[dict[str, Any]]) -> None:
        """Spawn a subagent to decide what to do with due SU notes."""
        from claude_agent_sdk import (
            ClaudeAgentOptions,
            ClaudeSDKClient,
            ResultMessage,
        )
        from app.memory_manager import get_basic_memory_mcp_config
        from app.life_manager import life_manager_mcp_server
        from app.su_notes_manager import su_notes_mcp_server
        from app.process_limiter import claude_process_slot

        # Build a summary of notes needing attention
        note_summaries: list[str] = []
        for note in notes:
            ctx = ""
            if note.get("context_json"):
                try:
                    ctx_data = json.loads(note["context_json"])
                    ctx = f" | context: {json.dumps(ctx_data)}"
                except (json.JSONDecodeError, TypeError):
                    ctx = f" | context: {note['context_json']}"

            related = ""
            if note.get("related_task_id"):
                related += f" | related_task: {note['related_task_id']}"
            if note.get("related_interjection_id"):
                related += f" | related_interjection: {note['related_interjection_id']}"

            note_summaries.append(
                f"- [{note['id']}] ({note['note_type']}, priority={note['priority']}, "
                f"attempts={note['attempts']}): {note['content']}{related}{ctx}"
            )

        now = datetime.utcnow()
        prompt = (
            f"Current time: {now.isoformat()}\n\n"
            f"The following SU notes are due for processing:\n\n"
            + "\n".join(note_summaries) + "\n\n"
            "For each note, decide what to do:\n"
            f"- If the user ({settings.user_name}) should be notified, create an interjection "
            "(set urgency based on priority and number of previous attempts).\n"
            "- If it's not the right time (too early, user likely busy), snooze the note "
            "by updating activate_after.\n"
            "- If the note has been addressed or is no longer relevant, complete it.\n"
            "- Update the note's context_json with what you did and why.\n"
            "- Increment attempts by updating the note after acting on it.\n\n"
            "Check the knowledge base and task list for context. Be decisive. "
            "Don't notify about things the user has already handled."
        )

        system_prompt = (
            f"You are {settings.su_name}'s note processor daemon. You review SU's internal "
            "notes-to-self and take action. You can create interjections to notify the user, "
            "snooze notes for later, update notes with context, or complete them. "
            "You have access to the knowledge base for context, the task/event list for "
            "schedule awareness, and the SU notes system for reading/updating notes.\n\n"
            "Be judicious about when to notify — consider time of day, urgency, and how "
            "many times the user has already been reminded. Escalate urgency over time "
            "for important deadlines. Headless — no clarifying questions."
        )

        options = ClaudeAgentOptions(
            mcp_servers={
                "basic_memory": get_basic_memory_mcp_config(),
                "life_manager": life_manager_mcp_server,
                "su_notes_manager": su_notes_mcp_server,
            },
            allowed_tools=[
                "mcp__basic_memory__search_notes",
                "mcp__basic_memory__read_note",
                "mcp__life_manager__create_interjection",
                "mcp__life_manager__list_interjections",
                "mcp__life_manager__list_tasks",
                "mcp__life_manager__list_events",
                "mcp__su_notes_manager__get_su_note",
                "mcp__su_notes_manager__update_su_note",
                "mcp__su_notes_manager__complete_su_note",
                "mcp__su_notes_manager__create_su_note",
            ],
            disallowed_tools=[
                "Task", "Bash", "Glob", "Grep", "Read", "Edit", "Write",
                "WebFetch", "WebSearch", "NotebookEdit",
            ],
            permission_mode="bypassPermissions",
            max_turns=20,
            system_prompt=system_prompt,
        )

        try:
            async with claude_process_slot(timeout=120, name="note_processor"), ClaudeSDKClient(options=options) as client:
                await client.query(prompt)
                async for message in client.receive_response():
                    if isinstance(message, ResultMessage) and message.is_error:
                        log.warning(
                            "scheduler.note_processor_error",
                            result=message.result or "unknown",
                        )
                        return

            log.info("scheduler.note_processor_completed", note_count=len(notes))
        except Exception:
            log.exception("scheduler.note_processor_failed")

    # ------------------------------------------------------------------
    # Email Scanner: triage inbox
    # ------------------------------------------------------------------

    async def _email_scanner(self) -> None:
        """Scan inbox via ProtonMail MCP and triage emails."""
        log.info("scheduler.email_scanner_starting")
        await self._run_email_scanner_agent()

    async def _run_email_scanner_agent(self) -> None:
        """Spawn a subagent to scan and triage the email inbox."""
        from claude_agent_sdk import (
            ClaudeAgentOptions,
            ClaudeSDKClient,
            ResultMessage,
        )
        from app.memory_manager import get_basic_memory_mcp_config
        from app.life_manager import life_manager_mcp_server
        from app.su_notes_manager import su_notes_mcp_server
        from app.process_limiter import claude_process_slot

        now = datetime.utcnow()
        prompt = (
            f"Current time: {now.isoformat()}\n\n"
            f"Scan {settings.user_name}'s inbox for new/unread emails. For each email:\n\n"
            "1. If it's actionable (deadline, request, appointment), create a task for the user "
            "and/or a SU note to follow up.\n"
            "2. If it needs a timely response, create a SU note with an appropriate activate_after.\n"
            "3. If it's informational but important, save relevant context to a SU note of type 'observation'.\n"
            "4. If it's spam/newsletter/low-priority, you can skip it.\n\n"
            "Check the knowledge base for context about people/projects mentioned. "
            "Don't create duplicate tasks for things already tracked. "
            "Store the email subject and sender in context_json on any SU notes you create."
        )

        system_prompt = (
            f"You are {settings.su_name}'s email scanner daemon. You periodically review "
            f"{settings.user_name}'s inbox and take proactive action. You can create tasks, "
            "calendar events, and SU notes. You have access to the email system, knowledge "
            "base, and task/event lists.\n\n"
            "Be selective — don't create noise. Only act on emails that genuinely need "
            "attention. For urgent items with deadlines, create both a user task AND a SU "
            "note to follow up if the user doesn't act. Headless — no clarifying questions."
        )

        protonmail_mcp = {
            "type": "stdio",
            "command": "protonmail-mcp-server",
            "args": [],
            "env": {
                "PROTONMAIL_USERNAME": settings.protonmail_username,
                "PROTONMAIL_PASSWORD": settings.protonmail_password,
                "PROTONMAIL_SMTP_HOST": settings.protonmail_smtp_host,
                "PROTONMAIL_SMTP_PORT": str(settings.protonmail_smtp_port),
                "PROTONMAIL_IMAP_HOST": settings.protonmail_imap_host,
                "PROTONMAIL_IMAP_PORT": str(settings.protonmail_imap_port),
            },
        }

        options = ClaudeAgentOptions(
            mcp_servers={
                "basic_memory": get_basic_memory_mcp_config(),
                "life_manager": life_manager_mcp_server,
                "su_notes_manager": su_notes_mcp_server,
                "protonmail": protonmail_mcp,
            },
            allowed_tools=[
                "mcp__protonmail__list_emails",
                "mcp__protonmail__read_email",
                "mcp__protonmail__search_emails",
                "mcp__protonmail__move_email",
                "mcp__protonmail__list_folders",
                "mcp__basic_memory__search_notes",
                "mcp__basic_memory__read_note",
                "mcp__life_manager__create_task",
                "mcp__life_manager__list_tasks",
                "mcp__life_manager__create_event",
                "mcp__life_manager__list_events",
                "mcp__life_manager__create_interjection",
                "mcp__su_notes_manager__create_su_note",
                "mcp__su_notes_manager__list_su_notes",
                "mcp__su_notes_manager__update_su_note",
            ],
            disallowed_tools=[
                "Task", "Bash", "Glob", "Grep", "Read", "Edit", "Write",
                "WebFetch", "WebSearch", "NotebookEdit",
            ],
            permission_mode="bypassPermissions",
            max_turns=30,
            system_prompt=system_prompt,
        )

        try:
            async with claude_process_slot(timeout=180, name="email_scanner"), ClaudeSDKClient(options=options) as client:
                await client.query(prompt)
                async for message in client.receive_response():
                    if isinstance(message, ResultMessage) and message.is_error:
                        log.warning(
                            "scheduler.email_scanner_error",
                            result=message.result or "unknown",
                        )
                        return

            log.info("scheduler.email_scanner_completed")
        except Exception:
            log.exception("scheduler.email_scanner_failed")

    # ------------------------------------------------------------------
    # Daily Review: morning brief
    # ------------------------------------------------------------------

    # Track the last date we ran daily review (in-memory; resets on restart)
    _last_daily_review_date: Optional[str] = None

    async def _daily_review(self) -> None:
        """Run daily review — once per day, targeting morning hours."""
        now = datetime.utcnow()
        today = now.strftime("%Y-%m-%d")

        # Only run once per day
        if self._last_daily_review_date == today:
            return

        # Only run between 6am and 9am (rough heuristic — UTC may need adjustment)
        if now.hour < 6 or now.hour >= 9:
            return

        self._last_daily_review_date = today
        log.info("scheduler.daily_review_starting", date=today)
        await self._run_daily_review_agent()

    async def _run_daily_review_agent(self) -> None:
        """Spawn a subagent to compose the morning brief."""
        from claude_agent_sdk import (
            ClaudeAgentOptions,
            ClaudeSDKClient,
            ResultMessage,
        )
        from app.memory_manager import get_basic_memory_mcp_config
        from app.life_manager import life_manager_mcp_server
        from app.su_notes_manager import su_notes_mcp_server
        from app.process_limiter import claude_process_slot

        now = datetime.utcnow()
        prompt = (
            f"Current time: {now.isoformat()}\n\n"
            "Prepare a morning brief for today. Review:\n"
            "1. All pending/in-progress tasks (especially urgent and overdue ones)\n"
            "2. Today's calendar events\n"
            "3. Active SU notes that need attention\n"
            "4. Recent knowledge base activity for anything relevant\n\n"
            "Compose a concise morning brief interjection. Structure:\n"
            f"- Greet {settings.user_name} briefly\n"
            "- Highlight today's schedule (key events)\n"
            "- Flag urgent/overdue tasks\n"
            "- Mention any follow-ups from SU notes\n"
            "- Keep it short — 3-5 bullet points max\n\n"
            "Create the interjection with source='daily_review', urgency='normal'."
        )

        system_prompt = (
            f"You are {settings.su_name}'s daily review daemon. You compose a morning "
            f"brief for {settings.user_name} covering the day's schedule, pending tasks, "
            "and anything needing attention. Be concise and useful — no fluff. "
            "Headless — no clarifying questions."
        )

        options = ClaudeAgentOptions(
            mcp_servers={
                "basic_memory": get_basic_memory_mcp_config(),
                "life_manager": life_manager_mcp_server,
                "su_notes_manager": su_notes_mcp_server,
            },
            allowed_tools=[
                "mcp__basic_memory__search_notes",
                "mcp__basic_memory__recent_activity",
                "mcp__life_manager__list_tasks",
                "mcp__life_manager__list_events",
                "mcp__life_manager__create_interjection",
                "mcp__su_notes_manager__list_su_notes",
            ],
            disallowed_tools=[
                "Task", "Bash", "Glob", "Grep", "Read", "Edit", "Write",
                "WebFetch", "WebSearch", "NotebookEdit",
            ],
            permission_mode="bypassPermissions",
            max_turns=15,
            system_prompt=system_prompt,
        )

        try:
            async with claude_process_slot(timeout=120, name="daily_review"), ClaudeSDKClient(options=options) as client:
                await client.query(prompt)
                async for message in client.receive_response():
                    if isinstance(message, ResultMessage) and message.is_error:
                        log.warning(
                            "scheduler.daily_review_error",
                            result=message.result or "unknown",
                        )
                        return

            log.info("scheduler.daily_review_completed")
        except Exception:
            log.exception("scheduler.daily_review_failed")


# Module-level singleton
scheduler = Scheduler()
