"""Tier 1: Repository layer tests for tasks, events, and interjections."""
import pytest

from app.database import init_database
from app.repositories import TaskRepo, EventRepo, InterjectionRepo


# ============================================================================
# Tasks
# ============================================================================

class TestTaskRepo:
    async def test_create_returns_task(self, db):
        task = await TaskRepo.create(title="Buy groceries")
        assert task.id is not None
        assert task.title == "Buy groceries"
        assert task.status == "pending"
        assert task.priority == 3

    async def test_create_with_all_fields(self, db):
        task = await TaskRepo.create(
            title="Deploy v2",
            description="Deploy the new version",
            priority=1,
            category="work",
            due_date="2026-03-01",
            due_time="14:00",
            source="chat",
            source_ref="session-123",
        )
        assert task.title == "Deploy v2"
        assert task.description == "Deploy the new version"
        assert task.priority == 1
        assert task.category == "work"
        assert task.due_date == "2026-03-01"

    async def test_list_empty(self, db):
        result = await TaskRepo.list()
        assert result == []

    async def test_list_returns_created_tasks(self, db):
        await TaskRepo.create(title="Task A")
        await TaskRepo.create(title="Task B")
        result = await TaskRepo.list()
        assert len(result) == 2
        titles = {t["title"] for t in result}
        assert titles == {"Task A", "Task B"}

    async def test_list_filter_by_status(self, db):
        task = await TaskRepo.create(title="To complete")
        await TaskRepo.complete(task.id)
        await TaskRepo.create(title="Still pending")

        pending = await TaskRepo.list(status="pending")
        assert len(pending) == 1
        assert pending[0]["title"] == "Still pending"

        done = await TaskRepo.list(status="done")
        assert len(done) == 1
        assert done[0]["title"] == "To complete"

    async def test_list_filter_by_category(self, db):
        await TaskRepo.create(title="Work task", category="work")
        await TaskRepo.create(title="Home task", category="home")

        result = await TaskRepo.list(category="work")
        assert len(result) == 1
        assert result[0]["title"] == "Work task"

    async def test_list_filter_by_priority(self, db):
        await TaskRepo.create(title="Urgent", priority=1)
        await TaskRepo.create(title="Low", priority=4)

        result = await TaskRepo.list(priority=1)
        assert len(result) == 1
        assert result[0]["title"] == "Urgent"

    async def test_list_filter_by_due_date_range(self, db):
        await TaskRepo.create(title="Early", due_date="2026-01-01")
        await TaskRepo.create(title="Mid", due_date="2026-06-15")
        await TaskRepo.create(title="Late", due_date="2026-12-31")

        result = await TaskRepo.list(due_after="2026-06-01", due_before="2026-07-01")
        assert len(result) == 1
        assert result[0]["title"] == "Mid"

    async def test_list_respects_limit(self, db):
        for i in range(10):
            await TaskRepo.create(title=f"Task {i}")
        result = await TaskRepo.list(limit=3)
        assert len(result) == 3

    async def test_update_fields(self, db):
        task = await TaskRepo.create(title="Original")
        await TaskRepo.update(task.id, title="Updated", priority=1)

        tasks = await TaskRepo.list()
        assert tasks[0]["title"] == "Updated"
        assert tasks[0]["priority"] == 1

    async def test_update_status_to_done_sets_completed_at(self, db):
        task = await TaskRepo.create(title="Finish me")
        await TaskRepo.update(task.id, status="done")

        tasks = await TaskRepo.list(status="done")
        assert len(tasks) == 1
        assert tasks[0]["completed_at"] is not None

    async def test_complete(self, db):
        task = await TaskRepo.create(title="Complete me")
        await TaskRepo.complete(task.id)

        tasks = await TaskRepo.list(status="done")
        assert len(tasks) == 1
        assert tasks[0]["completed_at"] is not None

    async def test_delete(self, db):
        task = await TaskRepo.create(title="Delete me")
        await TaskRepo.delete(task.id)

        tasks = await TaskRepo.list()
        assert len(tasks) == 0

    async def test_list_ordered_by_priority_then_due_date(self, db):
        await TaskRepo.create(title="Low priority late", priority=4, due_date="2026-12-01")
        await TaskRepo.create(title="High priority", priority=1, due_date="2026-06-01")
        await TaskRepo.create(title="Low priority early", priority=4, due_date="2026-01-01")

        result = await TaskRepo.list()
        titles = [t["title"] for t in result]
        assert titles[0] == "High priority"
        # Low priority tasks ordered by due_date
        assert titles[1] == "Low priority early"
        assert titles[2] == "Low priority late"


# ============================================================================
# Events
# ============================================================================

class TestEventRepo:
    async def test_create_returns_event(self, db):
        event = await EventRepo.create(title="Meeting", start_time="2026-03-01T10:00:00")
        assert event.id is not None
        assert event.title == "Meeting"
        assert event.start_time == "2026-03-01T10:00:00"

    async def test_create_with_all_fields(self, db):
        event = await EventRepo.create(
            title="Conference",
            start_time="2026-06-15T09:00:00",
            end_time="2026-06-15T17:00:00",
            description="Annual tech conference",
            all_day=False,
            location="Convention Center",
            reminder_minutes=60,
        )
        assert event.location == "Convention Center"
        assert event.reminder_minutes == 60
        assert event.all_day is False

    async def test_list_empty(self, db):
        result = await EventRepo.list()
        assert result == []

    async def test_list_returns_created_events(self, db):
        await EventRepo.create(title="Event A", start_time="2026-01-01T10:00:00")
        await EventRepo.create(title="Event B", start_time="2026-02-01T10:00:00")
        result = await EventRepo.list()
        assert len(result) == 2

    async def test_list_filter_by_date_range(self, db):
        await EventRepo.create(title="January", start_time="2026-01-15T10:00:00")
        await EventRepo.create(title="June", start_time="2026-06-15T10:00:00")
        await EventRepo.create(title="December", start_time="2026-12-15T10:00:00")

        result = await EventRepo.list(
            start_after="2026-06-01T00:00:00",
            start_before="2026-07-01T00:00:00",
        )
        assert len(result) == 1
        assert result[0]["title"] == "June"

    async def test_list_ordered_by_start_time(self, db):
        await EventRepo.create(title="Later", start_time="2026-12-01T10:00:00")
        await EventRepo.create(title="Earlier", start_time="2026-01-01T10:00:00")

        result = await EventRepo.list()
        assert result[0]["title"] == "Earlier"
        assert result[1]["title"] == "Later"

    async def test_list_respects_limit(self, db):
        for i in range(10):
            await EventRepo.create(title=f"Event {i}", start_time=f"2026-{i+1:02d}-01T10:00:00")
        result = await EventRepo.list(limit=3)
        assert len(result) == 3

    async def test_update_fields(self, db):
        event = await EventRepo.create(title="Original", start_time="2026-01-01T10:00:00")
        await EventRepo.update(event.id, title="Updated", location="New Place")

        events = await EventRepo.list()
        assert events[0]["title"] == "Updated"
        assert events[0]["location"] == "New Place"

    async def test_delete(self, db):
        event = await EventRepo.create(title="Delete me", start_time="2026-01-01T10:00:00")
        await EventRepo.delete(event.id)

        events = await EventRepo.list()
        assert len(events) == 0

    async def test_upcoming_within_window(self, db):
        from datetime import datetime
        await EventRepo.create(title="Past", start_time="2020-01-01T10:00:00")
        await EventRepo.create(title="Future", start_time="2099-01-01T10:00:00")

        result = await EventRepo.upcoming_within_window(datetime.utcnow())
        assert len(result) == 1
        assert result[0]["title"] == "Future"


# ============================================================================
# Interjections
# ============================================================================

class TestInterjectionRepo:
    async def test_create_returns_interjection(self, db):
        interjection = await InterjectionRepo.create(content="Time for lunch!")
        assert interjection.id is not None
        assert interjection.content == "Time for lunch!"
        assert interjection.urgency == "normal"
        assert interjection.status == "pending"

    async def test_create_with_all_fields(self, db):
        interjection = await InterjectionRepo.create(
            content="Meeting in 15 minutes",
            urgency="high",
            source="calendar_check",
            related_event_id="event-123",
        )
        assert interjection.urgency == "high"
        assert interjection.source == "calendar_check"

    async def test_list_empty(self, db):
        result = await InterjectionRepo.list()
        assert result == []

    async def test_list_with_status_filter(self, db):
        await InterjectionRepo.create(content="Pending one")
        i2 = await InterjectionRepo.create(content="To deliver")
        await InterjectionRepo.mark_delivered(i2.id)

        pending = await InterjectionRepo.list(status="pending")
        assert len(pending) == 1
        assert pending[0]["content"] == "Pending one"

        delivered = await InterjectionRepo.list(status="delivered")
        assert len(delivered) == 1
        assert delivered[0]["content"] == "To deliver"

    async def test_pending_returns_oldest_first(self, db):
        await InterjectionRepo.create(content="First")
        await InterjectionRepo.create(content="Second")

        result = await InterjectionRepo.pending()
        assert len(result) == 2
        assert result[0]["content"] == "First"
        assert result[1]["content"] == "Second"

    async def test_pending_respects_limit(self, db):
        for i in range(5):
            await InterjectionRepo.create(content=f"Item {i}")

        result = await InterjectionRepo.pending(limit=2)
        assert len(result) == 2

    async def test_mark_delivered(self, db):
        interjection = await InterjectionRepo.create(content="Deliver me")
        await InterjectionRepo.mark_delivered(interjection.id)

        items = await InterjectionRepo.list(status="delivered")
        assert len(items) == 1
        assert items[0]["delivered_at"] is not None

    async def test_dismiss(self, db):
        interjection = await InterjectionRepo.create(content="Dismiss me")
        await InterjectionRepo.dismiss(interjection.id)

        items = await InterjectionRepo.list(status="dismissed")
        assert len(items) == 1

    async def test_delivered_not_in_pending(self, db):
        interjection = await InterjectionRepo.create(content="Already delivered")
        await InterjectionRepo.mark_delivered(interjection.id)

        pending = await InterjectionRepo.pending()
        assert len(pending) == 0
