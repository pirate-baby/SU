"""Tier 2: Tasks REST API endpoint tests."""
import pytest


class TestCreateTask:
    async def test_creates_task(self, client):
        resp = await client.post("/api/tasks", json={"title": "Buy milk"})
        assert resp.status_code == 201
        data = resp.json()
        assert "id" in data
        assert data["title"] == "Buy milk"
        assert data["status"] == "pending"

    async def test_with_all_fields(self, client):
        resp = await client.post("/api/tasks", json={
            "title": "Deploy",
            "description": "Deploy the app",
            "priority": 1,
            "category": "work",
            "due_date": "2026-03-01",
            "due_time": "14:00",
        })
        assert resp.status_code == 201
        assert resp.json()["title"] == "Deploy"

    async def test_missing_title_returns_422(self, client):
        resp = await client.post("/api/tasks", json={})
        assert resp.status_code == 422


class TestListTasks:
    async def test_empty_initially(self, client):
        resp = await client.get("/api/tasks")
        assert resp.status_code == 200
        assert resp.json() == []

    async def test_returns_created_tasks(self, client):
        await client.post("/api/tasks", json={"title": "Task A"})
        await client.post("/api/tasks", json={"title": "Task B"})

        resp = await client.get("/api/tasks")
        assert len(resp.json()) == 2

    async def test_filter_by_status(self, client):
        resp1 = await client.post("/api/tasks", json={"title": "To complete"})
        task_id = resp1.json()["id"]
        await client.put(f"/api/tasks/{task_id}", json={"status": "done"})
        await client.post("/api/tasks", json={"title": "Still pending"})

        resp = await client.get("/api/tasks", params={"status": "pending"})
        data = resp.json()
        assert len(data) == 1
        assert data[0]["title"] == "Still pending"

    async def test_filter_by_category(self, client):
        await client.post("/api/tasks", json={"title": "Work", "category": "work"})
        await client.post("/api/tasks", json={"title": "Home", "category": "home"})

        resp = await client.get("/api/tasks", params={"category": "work"})
        data = resp.json()
        assert len(data) == 1
        assert data[0]["title"] == "Work"

    async def test_filter_by_priority(self, client):
        await client.post("/api/tasks", json={"title": "Urgent", "priority": 1})
        await client.post("/api/tasks", json={"title": "Low", "priority": 4})

        resp = await client.get("/api/tasks", params={"priority": 1})
        data = resp.json()
        assert len(data) == 1
        assert data[0]["title"] == "Urgent"

    async def test_filter_by_due_date_range(self, client):
        await client.post("/api/tasks", json={"title": "Early", "due_date": "2026-01-01"})
        await client.post("/api/tasks", json={"title": "Mid", "due_date": "2026-06-15"})
        await client.post("/api/tasks", json={"title": "Late", "due_date": "2026-12-31"})

        resp = await client.get("/api/tasks", params={
            "due_after": "2026-06-01",
            "due_before": "2026-07-01",
        })
        data = resp.json()
        assert len(data) == 1
        assert data[0]["title"] == "Mid"

    async def test_respects_limit(self, client):
        for i in range(10):
            await client.post("/api/tasks", json={"title": f"Task {i}"})

        resp = await client.get("/api/tasks", params={"limit": 3})
        assert len(resp.json()) == 3


class TestUpdateTask:
    async def test_updates_fields(self, client):
        resp = await client.post("/api/tasks", json={"title": "Original"})
        task_id = resp.json()["id"]

        resp = await client.put(f"/api/tasks/{task_id}", json={
            "title": "Updated",
            "priority": 1,
        })
        assert resp.status_code == 200
        assert resp.json()["updated"] == task_id

        # Verify the update
        tasks = (await client.get("/api/tasks")).json()
        assert tasks[0]["title"] == "Updated"
        assert tasks[0]["priority"] == 1

    async def test_empty_body_returns_400(self, client):
        resp = await client.post("/api/tasks", json={"title": "Test"})
        task_id = resp.json()["id"]

        resp = await client.put(f"/api/tasks/{task_id}", json={})
        assert resp.status_code == 400


class TestDeleteTask:
    async def test_deletes_task(self, client):
        resp = await client.post("/api/tasks", json={"title": "Delete me"})
        task_id = resp.json()["id"]

        resp = await client.delete(f"/api/tasks/{task_id}")
        assert resp.status_code == 200
        assert resp.json()["deleted"] == task_id

        # Verify deletion
        tasks = (await client.get("/api/tasks")).json()
        assert len(tasks) == 0
