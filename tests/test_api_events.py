"""Tier 2: Events REST API endpoint tests."""
import pytest


class TestCreateEvent:
    async def test_creates_event(self, client):
        resp = await client.post("/api/events", json={
            "title": "Meeting",
            "start_time": "2026-03-01T10:00:00",
        })
        assert resp.status_code == 201
        data = resp.json()
        assert "id" in data
        assert data["title"] == "Meeting"

    async def test_with_all_fields(self, client):
        resp = await client.post("/api/events", json={
            "title": "Conference",
            "start_time": "2026-06-15T09:00:00",
            "end_time": "2026-06-15T17:00:00",
            "description": "Annual conference",
            "all_day": False,
            "location": "Convention Center",
            "reminder_minutes": 60,
        })
        assert resp.status_code == 201

    async def test_missing_required_fields_returns_422(self, client):
        resp = await client.post("/api/events", json={"title": "No time"})
        assert resp.status_code == 422

        resp = await client.post("/api/events", json={"start_time": "2026-01-01T10:00:00"})
        assert resp.status_code == 422


class TestListEvents:
    async def test_empty_initially(self, client):
        resp = await client.get("/api/events")
        assert resp.status_code == 200
        assert resp.json() == []

    async def test_returns_created_events(self, client):
        await client.post("/api/events", json={"title": "A", "start_time": "2026-01-01T10:00:00"})
        await client.post("/api/events", json={"title": "B", "start_time": "2026-02-01T10:00:00"})

        resp = await client.get("/api/events")
        assert len(resp.json()) == 2

    async def test_filter_by_date_range(self, client):
        await client.post("/api/events", json={"title": "Jan", "start_time": "2026-01-15T10:00:00"})
        await client.post("/api/events", json={"title": "Jun", "start_time": "2026-06-15T10:00:00"})
        await client.post("/api/events", json={"title": "Dec", "start_time": "2026-12-15T10:00:00"})

        resp = await client.get("/api/events", params={
            "start_after": "2026-06-01T00:00:00",
            "start_before": "2026-07-01T00:00:00",
        })
        data = resp.json()
        assert len(data) == 1
        assert data[0]["title"] == "Jun"

    async def test_ordered_by_start_time(self, client):
        await client.post("/api/events", json={"title": "Later", "start_time": "2026-12-01T10:00:00"})
        await client.post("/api/events", json={"title": "Earlier", "start_time": "2026-01-01T10:00:00"})

        resp = await client.get("/api/events")
        data = resp.json()
        assert data[0]["title"] == "Earlier"
        assert data[1]["title"] == "Later"

    async def test_respects_limit(self, client):
        for i in range(10):
            await client.post("/api/events", json={
                "title": f"Event {i}",
                "start_time": f"2026-{i+1:02d}-01T10:00:00",
            })

        resp = await client.get("/api/events", params={"limit": 3})
        assert len(resp.json()) == 3


class TestUpdateEvent:
    async def test_updates_fields(self, client):
        resp = await client.post("/api/events", json={
            "title": "Original",
            "start_time": "2026-01-01T10:00:00",
        })
        event_id = resp.json()["id"]

        resp = await client.put(f"/api/events/{event_id}", json={
            "title": "Updated",
            "location": "New Location",
        })
        assert resp.status_code == 200

        events = (await client.get("/api/events")).json()
        assert events[0]["title"] == "Updated"
        assert events[0]["location"] == "New Location"

    async def test_empty_body_returns_400(self, client):
        resp = await client.post("/api/events", json={
            "title": "Test",
            "start_time": "2026-01-01T10:00:00",
        })
        event_id = resp.json()["id"]

        resp = await client.put(f"/api/events/{event_id}", json={})
        assert resp.status_code == 400


class TestDeleteEvent:
    async def test_deletes_event(self, client):
        resp = await client.post("/api/events", json={
            "title": "Delete me",
            "start_time": "2026-01-01T10:00:00",
        })
        event_id = resp.json()["id"]

        resp = await client.delete(f"/api/events/{event_id}")
        assert resp.status_code == 200

        events = (await client.get("/api/events")).json()
        assert len(events) == 0
