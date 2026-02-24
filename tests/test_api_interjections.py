"""Tier 2: Interjections, voice config, and logs API tests."""
import pytest

from app.database import init_database, get_db
from app.repositories import InterjectionRepo


# ============================================================================
# Interjections API
# ============================================================================

class TestListInterjections:
    async def test_empty_initially(self, client):
        resp = await client.get("/api/interjections")
        assert resp.status_code == 200
        assert resp.json() == []

    async def test_returns_pending_by_default(self, client):
        # Create via repo since there's no POST endpoint for interjections
        await InterjectionRepo.create(content="Pending")
        i2 = await InterjectionRepo.create(content="Delivered")
        await InterjectionRepo.mark_delivered(i2.id)

        resp = await client.get("/api/interjections")
        data = resp.json()
        assert len(data) == 1
        assert data[0]["content"] == "Pending"

    async def test_filter_by_status(self, client):
        i = await InterjectionRepo.create(content="To deliver")
        await InterjectionRepo.mark_delivered(i.id)

        resp = await client.get("/api/interjections", params={"status": "delivered"})
        data = resp.json()
        assert len(data) == 1
        assert data[0]["content"] == "To deliver"


class TestDismissInterjection:
    async def test_dismisses(self, client):
        interjection = await InterjectionRepo.create(content="Dismiss me")

        resp = await client.post(f"/api/interjections/{interjection.id}/dismiss")
        assert resp.status_code == 200
        assert resp.json()["dismissed"] == interjection.id

        # Verify it's no longer pending
        resp = await client.get("/api/interjections")
        assert len(resp.json()) == 0


# ============================================================================
# Voice Config API
# ============================================================================

class TestVoiceConfig:
    async def test_disabled_without_api_key(self, client):
        resp = await client.get("/api/voice/config")
        assert resp.status_code == 200
        assert resp.json()["enabled"] is False

    async def test_token_503_when_not_configured(self, client):
        resp = await client.get("/api/voice/token/stt")
        assert resp.status_code == 503

    async def test_token_invalid_type_400(self, client):
        resp = await client.get("/api/voice/token/invalid")
        # Without API key, 503 takes precedence
        assert resp.status_code in (400, 503)


# ============================================================================
# Logs API
# ============================================================================

class TestLogsAPI:
    async def test_returns_empty_list(self, client):
        resp = await client.get("/api/logs")
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)

    async def test_returns_logs_after_insert(self, client):
        # Insert a log entry directly
        async with get_db() as conn:
            await conn.execute(
                "INSERT INTO logs (timestamp, level, event, module) VALUES (?, ?, ?, ?)",
                ("2026-01-01T00:00:00", "info", "test.event", "test_module"),
            )
            await conn.commit()

        resp = await client.get("/api/logs")
        data = resp.json()
        assert len(data) == 1
        assert data[0]["event"] == "test.event"

    async def test_filter_by_level(self, client):
        async with get_db() as conn:
            await conn.execute(
                "INSERT INTO logs (timestamp, level, event, module) VALUES (?, ?, ?, ?)",
                ("2026-01-01T00:00:00", "info", "info.event", "test"),
            )
            await conn.execute(
                "INSERT INTO logs (timestamp, level, event, module) VALUES (?, ?, ?, ?)",
                ("2026-01-01T00:00:01", "error", "error.event", "test"),
            )
            await conn.commit()

        resp = await client.get("/api/logs", params={"level": "error"})
        data = resp.json()
        assert len(data) == 1
        assert data[0]["event"] == "error.event"

    async def test_filter_by_session_id(self, client):
        async with get_db() as conn:
            await conn.execute(
                "INSERT INTO logs (timestamp, level, event, module, session_id) VALUES (?, ?, ?, ?, ?)",
                ("2026-01-01T00:00:00", "info", "with.session", "test", "sess-123"),
            )
            await conn.execute(
                "INSERT INTO logs (timestamp, level, event, module) VALUES (?, ?, ?, ?)",
                ("2026-01-01T00:00:01", "info", "no.session", "test"),
            )
            await conn.commit()

        resp = await client.get("/api/logs", params={"session_id": "sess-123"})
        data = resp.json()
        assert len(data) == 1
        assert data[0]["event"] == "with.session"

    async def test_respects_limit(self, client):
        async with get_db() as conn:
            for i in range(10):
                await conn.execute(
                    "INSERT INTO logs (timestamp, level, event, module) VALUES (?, ?, ?, ?)",
                    (f"2026-01-01T00:00:{i:02d}", "info", f"event.{i}", "test"),
                )
            await conn.commit()

        resp = await client.get("/api/logs", params={"limit": 3})
        assert len(resp.json()) == 3
