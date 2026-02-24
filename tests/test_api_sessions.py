"""Tier 2: Session API endpoint tests."""
import pytest


class TestCreateSession:
    async def test_returns_session_id(self, client):
        resp = await client.post("/api/sessions/new")
        assert resp.status_code == 200
        data = resp.json()
        assert "session_id" in data
        assert len(data["session_id"]) == 36  # UUID

    async def test_returns_redirect_url(self, client):
        resp = await client.post("/api/sessions/new")
        data = resp.json()
        assert "redirect_url" in data
        assert data["redirect_url"].startswith("/chat/")
        assert data["session_id"] in data["redirect_url"]


class TestListSessions:
    async def test_empty_initially(self, client):
        resp = await client.get("/api/sessions")
        assert resp.status_code == 200
        assert resp.json() == []

    async def test_lists_created_sessions(self, client):
        await client.post("/api/sessions/new")
        await client.post("/api/sessions/new")

        resp = await client.get("/api/sessions")
        data = resp.json()
        assert len(data) == 2

    async def test_session_shape(self, client):
        await client.post("/api/sessions/new")

        resp = await client.get("/api/sessions")
        session = resp.json()[0]
        assert "id" in session
        assert "created_at" in session
        assert "last_activity" in session
        assert "status" in session
        assert "message_count" in session
        assert session["status"] == "active"


class TestGetSessionMessages:
    async def test_404_for_missing_session(self, client):
        resp = await client.get("/api/sessions/nonexistent/messages")
        assert resp.status_code == 404

    async def test_empty_messages_for_new_session(self, client):
        create_resp = await client.post("/api/sessions/new")
        sid = create_resp.json()["session_id"]

        resp = await client.get(f"/api/sessions/{sid}/messages")
        assert resp.status_code == 200
        assert resp.json() == []


class TestEndSession:
    async def test_ends_session(self, client):
        create_resp = await client.post("/api/sessions/new")
        sid = create_resp.json()["session_id"]

        resp = await client.post(f"/api/sessions/{sid}/end")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ended"

    async def test_404_for_missing_session(self, client):
        resp = await client.post("/api/sessions/nonexistent/end")
        assert resp.status_code == 404

    async def test_ended_session_not_in_active_chat(self, client):
        create_resp = await client.post("/api/sessions/new")
        sid = create_resp.json()["session_id"]
        await client.post(f"/api/sessions/{sid}/end")

        # Chat page should 404 for ended session
        resp = await client.get(f"/chat/{sid}")
        assert resp.status_code == 404
