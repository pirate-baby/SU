"""Tier 2: Health check and page route tests."""
import pytest


class TestHealthEndpoint:
    async def test_returns_200(self, client):
        resp = await client.get("/health")
        assert resp.status_code == 200

    async def test_response_shape(self, client):
        resp = await client.get("/health")
        data = resp.json()
        assert data["status"] == "healthy"
        assert data["service"] == "su-personal-assistant"
        assert data["version"] == "3.0.0"


class TestPageRoutes:
    async def test_landing_page(self, client):
        resp = await client.get("/")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]

    async def test_sessions_page(self, client):
        resp = await client.get("/sessions")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]

    async def test_planner_page(self, client):
        resp = await client.get("/planner")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]

    async def test_chat_page_404_for_missing_session(self, client):
        resp = await client.get("/chat/nonexistent-session-id")
        assert resp.status_code == 404

    async def test_chat_page_200_for_valid_session(self, client):
        # Create a session first
        create_resp = await client.post("/api/sessions/new")
        session_id = create_resp.json()["session_id"]

        resp = await client.get(f"/chat/{session_id}")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]
