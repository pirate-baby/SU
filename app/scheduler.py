"""
Background scheduler: periodic tasks for proactive SU behavior.

Jobs:
  - calendar_check: detect upcoming events and spawn a subagent to compose
    contextual reminders (interjections) for delivery.
  - interjection_delivery: push pending interjections to connected clients.
  - note_processor: process SU's internal notes-to-self (every 10 min).
  - email_scanner: triage inbox via ProtonMail MCP (every 10 min).
  - email_unsubscriber: process unsubscribe requests from scanner (every 30 min).
  - daily_review: compose morning brief from tasks/events/notes (once/day).
"""
import asyncio
import json
from datetime import datetime, timedelta
from typing import Any, Callable, Coroutine, Optional

from app.config import settings
from app.tz import LOCAL_TZ, now as local_now
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
            description="Pushes pending interjections to WebSocket/Telegram",
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
                name="email_unsubscriber",
                display_name="Email Unsubscriber",
                category=DaemonCategory.SCHEDULER,
                interval_seconds=1800,
                description="Processes unsubscribe requests from email scanner via SU notes",
            ))
        daemon_registry.register(DaemonInfo(
            name="daily_review",
            display_name="Daily Review",
            category=DaemonCategory.SCHEDULER,
            interval_seconds=3600,
            condition="Once/day, 6-9am Eastern",
            description="Composes morning brief from tasks, events, and notes",
        ))
        daemon_registry.register(DaemonInfo(
            name="health_snapshot",
            display_name="Health Snapshot",
            category=DaemonCategory.SYSTEM,
            interval_seconds=300,
            description="Collects health metrics and runs retention cleanup",
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

        # Email scanner and unsubscriber — only if ProtonMail is configured
        if settings.protonmail_username and settings.protonmail_password:
            self._tasks["email_scanner"] = asyncio.create_task(
                self._periodic("email_scanner", self._email_scanner, interval=600),
                name="sched-email-scanner",
            )
            self._tasks["email_unsubscriber"] = asyncio.create_task(
                self._periodic("email_unsubscriber", self._email_unsubscriber, interval=1800),
                name="sched-email-unsubscriber",
            )

        # Daily review — runs every hour, but only fires once per day
        self._tasks["daily_review"] = asyncio.create_task(
            self._periodic("daily_review", self._daily_review, interval=3600),
            name="sched-daily-review",
        )

        # Health snapshot — every 5 minutes
        self._tasks["health_snapshot"] = asyncio.create_task(
            self._periodic("health_snapshot", self._health_snapshot, interval=300),
            name="sched-health-snapshot",
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
        """Run *func* every *interval* seconds until stopped.

        If *func* returns a dict it is stored as run metadata.
        """
        # Wait until start() signals that the app is fully initialized
        await self._ready.wait()

        while self._running:
            run_id = await daemon_registry.start_run(name)
            try:
                result = await func()
                metadata = result if isinstance(result, dict) else None
                await daemon_registry.end_run(
                    run_id, name, RunStatus.COMPLETED, metadata=metadata,
                )
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
        now = local_now()
        rows = await EventRepo.upcoming_within_window(now)

        events_to_remind: list[dict[str, Any]] = []
        for event in rows:
            event_id = event["id"]
            if event_id in _reminded_event_ids:
                continue

            start = datetime.fromisoformat(event["start_time"])
            # Ensure start is tz-aware (events may be stored without offset)
            if start.tzinfo is None:
                start = start.replace(tzinfo=LOCAL_TZ)
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
        from app.agents import build_calendar_agent

        # Build a summary of the events needing reminders
        event_summaries: list[str] = []
        for event in events:
            start = datetime.fromisoformat(event["start_time"])
            if start.tzinfo is None:
                start = start.replace(tzinfo=LOCAL_TZ)
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

        try:
            agent = build_calendar_agent()
            result = await agent.run(prompt)
            log.info("scheduler.calendar_agent_completed", event_count=len(events))
        except Exception:
            log.exception("scheduler.calendar_agent_failed")
            # Fallback: create basic text interjections so reminders aren't lost
            for event in events:
                start = datetime.fromisoformat(event["start_time"])
                if start.tzinfo is None:
                    start = start.replace(tzinfo=LOCAL_TZ)
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
                    content=f"Reminder: \"{event['title']}\"{location} is {time_str}.",
                    urgency="high",
                    source="calendar_check",
                    related_event_id=event["id"],
                )
            log.info("scheduler.calendar_fallback_created", count=len(events))

    async def _deliver_interjections(self) -> None:
        """Push pending interjections to connected clients.

        Delivers via WebSocket for real-time in-page updates AND via
        Telegram for native phone notifications. Interjections with
        urgency='call' trigger the phone-call flow instead.
        """
        if not self._push_fn:
            return

        items = await InterjectionRepo.pending()
        if not items:
            return

        for item in items:
            try:
                # Call-urgency interjections use the phone-call flow
                if item.get("urgency") == "call":
                    await self._deliver_call_interjection(item)
                    await InterjectionRepo.mark_delivered(item["id"])
                    continue

                ws_delivered = await self._push_fn(item)

                # Send via Telegram so the user gets a phone notification
                # regardless of whether a browser tab is open.
                tg_delivered = 0
                try:
                    from app.telegram_bot import deliver_interjection_via_telegram
                    tg_delivered = await deliver_interjection_via_telegram(item)
                except Exception:
                    log.exception("scheduler.telegram_delivery_failed", id=item["id"])

                await InterjectionRepo.mark_delivered(item["id"])
                log.info(
                    "scheduler.interjection_delivered",
                    id=item["id"],
                    ws_delivered=ws_delivered,
                    tg_delivered=tg_delivered,
                )
            except Exception:
                log.exception("scheduler.interjection_push_failed", id=item["id"])
                break

    async def _deliver_call_interjection(self, item: dict[str, Any]) -> None:
        """Deliver a call-urgency interjection as a phone call.

        1. Pre-creates a session with interjection context as memory.
        2. Pushes `incoming_call` via WebSocket if a client is connected.
        3. Sends Telegram message with inline "Answer Call" button deep-linking
           to /call/{session_id}.
        """
        from app.session_manager import create_session, save_message

        # Pre-create session with context
        session_id = await create_session()
        context = item.get("content", "")
        await save_message(session_id, "memory", (
            f"<context>\nSU initiated a call.\n"
            f"Reason: {context}\n"
            f"Source: {item.get('source', 'unknown')}\n"
            f"</context>"
        ))
        await InterjectionRepo.link_session(item["id"], session_id)

        # Push via WebSocket
        ws_delivered = 0
        try:
            from app.main import push_incoming_call_to_clients
            ws_delivered = await push_incoming_call_to_clients(session_id, context)
        except Exception:
            log.exception("scheduler.call_ws_push_failed", id=item["id"])

        # Send Telegram notification with "Answer Call" deep link
        tg_delivered = 0
        if settings.telegram_bot_token and settings.app_host:
            try:
                from app.telegram_bot import send_call_notification
                from app.telegram_users import TelegramUserRepo
                call_url = f"https://{settings.app_host}/call/{session_id}"
                users = await TelegramUserRepo.list_all()
                for user in users:
                    if await send_call_notification(
                        user["telegram_chat_id"],
                        context[:200],
                        call_url,
                    ):
                        tg_delivered += 1
            except Exception:
                log.exception("scheduler.call_telegram_failed", id=item["id"])

        log.info(
            "scheduler.call_interjection_delivered",
            id=item["id"],
            session_id=session_id,
            ws_delivered=ws_delivered,
            tg_delivered=tg_delivered,
        )

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
        from app.agents import build_note_processor_agent

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

        now = local_now()
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

        try:
            agent = build_note_processor_agent()
            result = await agent.run(prompt)
            log.info("scheduler.note_processor_completed", note_count=len(notes))
        except Exception:
            log.exception("scheduler.note_processor_failed")

    # ------------------------------------------------------------------
    # Email Scanner: triage inbox
    # ------------------------------------------------------------------

    async def _email_scanner(self) -> dict:
        """Scan inbox via ProtonMail MCP and triage emails."""
        log.info("scheduler.email_scanner_starting")
        return await self._run_email_scanner_agent()

    # Maximum emails to process per agent run (keeps context within model
    # limits).  Each batch spawns a fresh agent with a clean context window.
    _EMAIL_BATCH_SIZE: int = 10
    _EMAIL_MAX_BATCHES: int = 20

    async def _run_email_scanner_agent(self) -> dict:
        """Spawn a subagent to scan and triage the email inbox.

        Processes emails in batches to stay within the model's context window.
        Each batch creates a fresh agent so conversation history is reset.

        Returns a metadata dict with scan metrics for the daemon run record.
        """
        from app.agents import build_email_scanner_agent

        now = local_now()
        email_instructions = (
            "For each email:\n\n"
            "1. If it's actionable (deadline, request, appointment), create a task for the user "
            "and/or a SU note to follow up.\n"
            "2. If it needs a timely response, create a SU note with an appropriate activate_after.\n"
            "3. If it's informational but important, save relevant context to a SU note of type 'observation'.\n"
            "4. If it's spam/newsletter/low-priority, no task or note needed.\n\n"
            "After processing each email, you MUST clear it from the inbox:\n"
            "- Actionable/important emails: move to an appropriate folder (e.g. 'Receipts', "
            "'Work', 'Personal') using move_email. Create the folder with create_folder first "
            "if it doesn't exist.\n"
            "- Spam/newsletters/junk: delete with delete_email.\n"
            "- Everything else: archive by moving to 'Archive' folder.\n\n"
            "UNSUBSCRIBE: When you delete a newsletter/spam/marketing email, look for an "
            "unsubscribe link in the email body (text like 'unsubscribe', 'opt out', "
            "'manage preferences', 'email preferences' with an href URL). If you find one, "
            "first use check_unsubscribed with the sender email to see if we already handled "
            "this sender. If NOT already unsubscribed, create a SU note with:\n"
            "  - note_type: 'todo'\n"
            "  - source: 'email_scanner'\n"
            "  - content: 'Unsubscribe from <sender name/email>'\n"
            "  - context_json: JSON string with keys: sender_email, sender_name, "
            "unsubscribe_url (the https:// or mailto: link), email_subject\n"
            "This note will be picked up by the unsubscriber daemon later.\n\n"
            "Check the knowledge base for context about people/projects mentioned. "
            "Don't create duplicate tasks for things already tracked. "
            "Store the email subject and sender in context_json on any SU notes you create.\n\n"
            "IMPORTANT: When you are finished, you MUST end your final message with "
            "exactly one of these markers on its own line:\n"
            "- [INBOX_EMPTY] — if no emails remain in the inbox\n"
            "- [MORE_EMAILS] — if there are still unprocessed emails in the inbox"
        )

        batches_run = 0
        for batch_num in range(1, self._EMAIL_MAX_BATCHES + 1):
            prompt = (
                f"Current time: {now.isoformat()}\n\n"
                f"Scan {settings.user_name}'s inbox across all addresses and aliases "
                "(multiple addresses may be proxied into this account). "
                f"List the emails, then process UP TO {self._EMAIL_BATCH_SIZE} of them. "
                "Do NOT try to process more than that in a single run.\n\n"
                f"{email_instructions}"
            )

            try:
                agent = build_email_scanner_agent()
                result = await agent.run(prompt)
                batches_run = batch_num
                response = str(result.output).strip()
                log.info(
                    "scheduler.email_scanner_batch_completed",
                    batch=batch_num,
                )

                # Stop if the agent reports the inbox is empty, or if there's
                # no clear signal that more emails remain.
                if "[INBOX_EMPTY]" in response or "[MORE_EMAILS]" not in response:
                    break
            except Exception:
                log.exception("scheduler.email_scanner_failed", batch=batch_num)
                raise

        log.info(
            "scheduler.email_scanner_completed",
            batches=batches_run,
        )
        return {"status": "completed", "batches": batches_run}

    # ------------------------------------------------------------------
    # Email Unsubscriber: process pending unsubscribe requests
    # ------------------------------------------------------------------

    async def _email_unsubscriber(self) -> dict:
        """Process pending unsubscribe SU notes created by the email scanner."""
        # Find active SU notes from email_scanner that are about unsubscribing
        notes = await SuNoteRepo.list(
            status="active", source="email_scanner", limit=50,
        )
        unsub_notes = [
            n for n in notes
            if "unsubscribe" in (n.get("content", "")).lower()
        ]

        if not unsub_notes:
            return {"unsubscribe_notes_found": 0}

        log.info("scheduler.email_unsubscriber_starting", count=len(unsub_notes))
        return await self._run_email_unsubscriber_agent(unsub_notes)

    async def _run_email_unsubscriber_agent(self, notes: list[dict]) -> dict:
        """Spawn a subagent to execute unsubscribe actions."""
        from app.agents import build_email_unsubscriber_agent

        # Build note summaries for the prompt
        note_summaries = []
        for note in notes:
            ctx = note.get("context_json", "")
            note_summaries.append(
                f"- Note [{note['id']}]: {note['content']} | context: {ctx}"
            )

        now = local_now()
        prompt = (
            f"Current time: {now.isoformat()}\n\n"
            "Process the following unsubscribe requests:\n\n"
            + "\n".join(note_summaries) + "\n\n"
            "For each one:\n"
            "1. First check if we've already unsubscribed from this sender "
            "(use check_unsubscribed).\n"
            "2. If already unsubscribed, just complete the SU note and move on.\n"
            "3. Parse the unsubscribe_url from the note's context_json:\n"
            "   - If it's a mailto: URL, extract the email address and any subject "
            "parameter, then use send_email to send an unsubscribe email.\n"
            "   - If it's an https:// URL, use the dangerous_assignment tool to visit "
            "the page and click any confirm/unsubscribe buttons. Set websites_allowed "
            "to [the unsubscribe URL] and use this response_schema:\n"
            '   {"type": "object", "properties": {"success": {"type": "boolean"}, '
            '"message": {"type": "string"}}, "required": ["success", "message"], '
            '"additionalProperties": false}\n'
            "4. Record the result with record_unsubscribe (status: completed or failed).\n"
            "5. Complete the SU note when done (use complete_su_note).\n\n"
            "If an unsubscribe page asks for an email address to confirm, "
            "do NOT enter one — record as failed with an explanation. "
            "Be efficient — don't waste turns on retries."
        )

        try:
            agent = build_email_unsubscriber_agent()
            result = await agent.run(prompt)
            log.info(
                "scheduler.email_unsubscriber_completed",
                unsubscribe_notes_found=len(notes),
            )
            return {
                "unsubscribe_notes_found": len(notes),
                "status": "completed",
            }
        except Exception:
            log.exception(
                "scheduler.email_unsubscriber_failed",
                unsubscribe_notes_found=len(notes),
            )
            raise

    # ------------------------------------------------------------------
    # Daily Review: morning brief
    # ------------------------------------------------------------------

    # Track the last date we ran daily review (in-memory; resets on restart)
    _last_daily_review_date: Optional[str] = None

    async def _daily_review(self) -> None:
        """Run daily review — once per day, targeting morning hours (Eastern)."""
        now = local_now()
        today = now.strftime("%Y-%m-%d")

        # Only run once per day
        if self._last_daily_review_date == today:
            return

        # Only run between 6am and 9am Eastern
        if now.hour < 6 or now.hour >= 9:
            return

        self._last_daily_review_date = today
        log.info("scheduler.daily_review_starting", date=today)
        await self._run_daily_review_agent()

    async def _run_daily_review_agent(self) -> None:
        """Spawn a subagent to compose the morning brief."""
        from app.agents import build_daily_review_agent

        now = local_now()
        prompt = (
            f"Current time: {now.isoformat()}\n\n"
            "Prepare a morning brief for today. Review:\n"
            "1. All pending/in-progress tasks (especially urgent and overdue ones)\n"
            "2. Today's calendar events\n"
            "3. Active SU notes that need attention\n"
            "4. Recent knowledge base activity for anything relevant\n\n"
            "Compose a concise morning brief interjection. Structure:\n"
            "- Greet the user\n"
            "- Highlight today's schedule (key events)\n"
            "- Flag urgent/overdue tasks\n"
            "- Mention any follow-ups from SU notes\n"
            "- Keep it short — 3-5 bullet points max\n\n"
            "Create the interjection with source='daily_review', urgency='normal'."
        )

        try:
            agent = build_daily_review_agent()
            result = await agent.run(prompt)
            log.info("scheduler.daily_review_completed")
        except Exception:
            log.exception("scheduler.daily_review_failed")


    # ------------------------------------------------------------------
    # Health Snapshot: collect metrics and run retention cleanup
    # ------------------------------------------------------------------

    # Track the last hour we ran retention cleanup (avoid running every 5 min)
    _last_cleanup_hour: Optional[int] = None

    async def _health_snapshot(self) -> None:
        """Collect health metrics and persist a snapshot.

        Also runs retention cleanup once per hour (at minute 0-4).
        """
        from app.health import collect_health_snapshot, save_health_snapshot, run_retention_cleanup

        snapshot = await collect_health_snapshot()
        await save_health_snapshot(snapshot)

        # Run retention cleanup once per hour
        now = datetime.utcnow()
        current_hour = now.hour
        if self._last_cleanup_hour != current_hour:
            self._last_cleanup_hour = current_hour
            try:
                result = await run_retention_cleanup()
                log.info("scheduler.retention_cleanup_completed", **result)
            except Exception:
                log.exception("scheduler.retention_cleanup_failed")


# Module-level singleton
scheduler = Scheduler()
