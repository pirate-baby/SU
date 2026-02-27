# Pro-active SU: Implementation Plan

## What Already Exists (surprisingly a lot)

| Capability | Status | Where |
|---|---|---|
| Task/event CRUD | Done | `life_manager.py`, `repositories.py`, `orm.py` |
| Interjection queue | Done | `InterjectionRepo`, with urgency, source, related_task/event_id |
| WebSocket push delivery | Done | `scheduler.py` → `push_interjection_to_clients()` |
| Web Push (VAPID) fallback | Done | `push_service.py` + `sw.js` |
| Scheduler framework | Done | `scheduler.py` — periodic asyncio jobs |
| Claude subagent spawning | Done | Calendar check agent pattern, process limiter |
| basic-memory knowledge base | Done | MCP server for narrative memory |
| Subconscious memory injection | Done | `subconscious_agent.py` + `_collect_and_consume_memories()` |
| Session context replay | Done | `_inject_history()` in `agent_registry.py` |
| ProtonMail access | Done | MCP server for IMAP/SMTP |

## What's Missing (three things)

### 1. SU's Internal Notes-to-Self (`su_notes` table)

This is the **key new primitive**. Not a user task. Not a basic-memory note. A private scratchpad for SU's daemon processes to communicate with each other across time.

```sql
CREATE TABLE su_notes (
    id TEXT PRIMARY KEY,
    content TEXT NOT NULL,           -- what SU wants to remember/do
    note_type TEXT DEFAULT 'todo',   -- 'todo', 'reminder', 'observation', 'log'
    status TEXT DEFAULT 'active',    -- 'active', 'done', 'snoozed', 'cancelled'
    priority TEXT DEFAULT 'normal',  -- 'low', 'normal', 'high', 'urgent'
    activate_after TEXT,             -- ISO datetime: don't act on this before then
    related_task_id TEXT,            -- optional link to user task
    related_interjection_id TEXT,    -- optional link to a previous interjection
    source TEXT,                     -- which daemon created it: 'email_scan', 'daily_review', etc.
    context_json TEXT,               -- arbitrary JSON blob for rich context (e.g. email subject, attempts history)
    attempts INTEGER DEFAULT 0,      -- how many times SU has tried to act on this
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    completed_at TIMESTAMP
);
```

**Why not reuse existing tasks?** Tasks are *the user's* tasks, visible in the planner UI, managed by the user. SU notes are SU's private operational state — "remind him again on 3/1", "I already asked about this and he dismissed it", "check back after 10am". Mixing them would pollute the user's planner and confuse the agent about whose agenda items these are.

**Why not basic-memory?** basic-memory is for narrative/semantic knowledge ("Ethan prefers Postgres"). SU notes are operational and temporal — they have activation times, attempt counts, statuses. They need structured queries (give me all active notes with `activate_after < now`), not semantic search. SQLite is the right home.

### 2. Daemon Agent Framework (new scheduler jobs)

Add configurable daemon jobs to the existing `Scheduler`. Each daemon is a Claude subagent with specific tools and a focused prompt. The pattern already exists in `_calendar_check` — we're just adding more of them.

**New daemon jobs:**

| Job | Interval | What it does |
|---|---|---|
| `note_processor` | 10 min | Reads active `su_notes` where `activate_after <= now`. For each, spawns a subagent that decides what to do: create an interjection, snooze, update the note, etc. |
| `email_scanner` | 10 min | Scans inbox via ProtonMail MCP. Triages: archive, folder, create user tasks, create SU notes for follow-ups. |
| `daily_review` | Once/day (6am) | Reviews all pending user tasks, upcoming events, active SU notes. Creates a morning brief interjection. Identifies things that need attention. |

The `note_processor` is the **central nervous system** — it's what makes the corporate filing example work. Email scanner creates the note → note processor picks it up hours/days later → creates an interjection → tracks dismissals → escalates.

**Implementation:** Each daemon follows the exact pattern of `_compose_calendar_interjections`:
1. Query the relevant data (su_notes, emails, tasks)
2. Build a prompt with the data
3. Spawn a Claude subagent with `life_manager` + `basic_memory` + a new `su_notes_manager` MCP server
4. The agent decides what actions to take
5. Bounded by process_limiter and max_turns

**New MCP server: `su_notes_manager`** — exposes CRUD tools for su_notes, so daemon agents can read/create/update/snooze notes. Same pattern as `life_manager.py`. Tools:
- `create_su_note` — create a note-to-self
- `list_su_notes` — list active notes, optionally filtered by type/status/activation time
- `update_su_note` — update content, status, snooze (set activate_after)
- `complete_su_note` — mark done
- `get_su_note` — read a single note with full context

### 3. Context-Rich Push → Chat Session Bridge

Currently: push notification → landing page → blank session. We need: push notification → pre-contextified chat session.

**Changes:**

a. **`POST /api/sessions/new`** gains an optional JSON body:
```json
{
    "interjection_id": "uuid",
    "initial_context": "optional text blob"
}
```
When `interjection_id` is provided, the endpoint:
1. Creates the session
2. Loads the interjection (and its related task/SU notes via `context_json`)
3. Saves a `role="memory"` message with all the context (what triggered this, SU's previous attempts, the user's dismissal history, relevant task details)
4. Returns the session_id as usual

b. **Push payload URL** changes from `"/"` to `"/api/sessions/from-interjection/{interjection_id}"` — a new endpoint that creates the session, pre-loads context, and redirects to `/chat/{session_id}`.

c. **Service worker `notificationclick`** already navigates to the payload URL — no change needed there.

d. **Interjection UI in planner** gains a "Chat about this" button (in addition to dismiss) that hits the same endpoint.

e. **`InterjectionRow`** gets a new nullable column `session_id` — when a chat session is created from an interjection, we link them. This lets SU notes track "I created an interjection, the user opened a chat about it."

---

## The Corporate Filing Example, End-to-End

With this plan implemented, your example plays out exactly as described:

1. **`email_scanner` daemon** (every 10 min) scans inbox, finds corporate filing overdue email. Creates a user task (`source='email'`, due_date='2026-03-05'). Creates an `su_note`:
   ```
   content: "Corporate filing overdue — deadline 3/5. Need to remind user. Today looks hectic per calendar. Try tomorrow."
   note_type: 'reminder'
   activate_after: '2026-02-27T09:00'
   related_task_id: <the task UUID>
   context_json: {"email_subject": "...", "deadline": "2026-03-05", "attempts": []}
   ```

2. **`note_processor` daemon** next day picks up the note (activate_after has passed). Subagent reviews user's schedule, composes an interjection, calls `create_interjection(urgency='normal', source='note_processor', related_task_id=...)`. Updates the su_note: `attempts += 1`, logs the attempt in `context_json`.

3. **Interjection delivery** pushes the reminder to the user. User dismisses it. The `POST /api/interjections/{id}/dismiss` handler also updates the su_note: `context_json.attempts.append({date, result: 'dismissed'})`, snoozes note to `activate_after='2026-03-01T10:00'`.

4. **`note_processor`** on 2/29 sees the note but `activate_after` is 3/1 — skips it.

5. **`note_processor`** on 3/1 at the 10am cycle picks it up. Subagent sees 2 previous attempts, deadline in 4 days, increases urgency. Creates `create_interjection(urgency='high', ...)`.

6. **Push notification** arrives. User clicks it → `sw.js` navigates to `/api/sessions/from-interjection/{id}` → server creates session, pre-loads context:
   ```
   <context>
   SU note: Corporate filing is overdue. Deadline March 5th.
   History: First reminded on 2/27 — user dismissed. Second reminder on 3/1 — user is now engaging.
   Related task: "File corporate filing" (priority: urgent, due: 2026-03-05)
   The user clicked into this reminder to deal with it.
   </context>
   ```
   → Redirects to `/chat/{session_id}`. User asks "get me the form" and SU has full context.

---

## Implementation Order

### Phase 1: SU Notes primitive (foundation)
1. Add `su_notes` table to `database.py` `init_database()`
2. Add `SuNoteRow` to `orm.py`
3. Add `SuNote` pydantic model to `models.py`
4. Add `SuNoteRepo` to `repositories.py`
5. Create `su_notes_manager.py` (MCP server with CRUD tools)

### Phase 2: Note Processor daemon
6. Add `_note_processor` job to `scheduler.py`
7. Wire it into `scheduler.start()` with 10-minute interval
8. Write the subagent prompt — it reads active due notes and decides actions

### Phase 3: Context-rich push → chat bridge
9. Add `session_id` column to interjections table
10. Modify `POST /api/sessions/new` to accept optional context
11. Add `GET /api/sessions/from-interjection/{id}` endpoint (create + redirect)
12. Update push payload URL in `push_service.py`
13. Add "Chat about this" button to planner interjection UI
14. Update interjection dismiss handler to snooze related SU notes

### Phase 4: Email Scanner daemon
15. Add `_email_scanner` job to `scheduler.py`
16. Wire ProtonMail MCP into the daemon subagent
17. Write the subagent prompt — triage emails into folders/archive/tasks/notes

### Phase 5: Daily Review daemon
18. Add `_daily_review` job (once per day at configured hour)
19. Write the subagent prompt — compose morning brief from tasks/events/notes

---

## What We're NOT Building

- No new databases. SQLite handles everything. su_notes is just a new table.
- No new process types. Everything runs as asyncio tasks inside the existing FastAPI process, using the existing process_limiter.
- No new frontend framework. Minimal changes to existing planner.html/chat.js.
- No external job scheduler (cron, celery, etc.). The existing Scheduler class is sufficient — it already handles periodic jobs correctly and survives for the lifetime of the process.
- No changes to basic-memory. It continues to handle narrative knowledge. SU notes handle operational state.
