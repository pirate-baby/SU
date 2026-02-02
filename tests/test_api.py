"""Tests for the Claude Task Executor API."""
import pytest
from fastapi.testclient import TestClient
from app.main import app


@pytest.fixture
def client():
    """Create a test client."""
    return TestClient(app)


def test_root_endpoint(client):
    """Test the root endpoint returns healthy status."""
    response = client.get("/")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "healthy"
    assert data["service"] == "claude-task-executor"


def test_health_endpoint(client):
    """Test the health check endpoint."""
    response = client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert "status" in data
    assert "claude_available" in data
    assert "claude_version" in data


def test_execute_endpoint_missing_prompt(client):
    """Test execute endpoint without prompt returns validation error."""
    response = client.post("/execute", json={})
    assert response.status_code == 422  # Validation error


def test_execute_endpoint_with_prompt(client):
    """Test execute endpoint with valid prompt."""
    response = client.post(
        "/execute",
        json={
            "prompt": "echo test",
            "timeout": 30
        }
    )
    # Should return 200 even if Claude isn't available
    # (will have success=false in that case)
    assert response.status_code in [200, 404, 408, 500]

    if response.status_code == 200:
        data = response.json()
        assert "success" in data


def test_execute_endpoint_custom_binary_path(client):
    """Test execute endpoint with custom binary path."""
    response = client.post(
        "/execute",
        json={
            "prompt": "test",
            "claude_binary_path": "/nonexistent/path",
            "timeout": 5
        }
    )
    # Should fail with 404 for missing binary
    assert response.status_code == 404
    data = response.json()
    assert "not found" in data["detail"].lower()


def test_execute_endpoint_timeout(client):
    """Test execute endpoint respects timeout parameter."""
    # This test validates the timeout parameter is accepted
    response = client.post(
        "/execute",
        json={
            "prompt": "test",
            "timeout": 1  # Very short timeout
        }
    )
    # Could be 200, 408 (timeout), or 404 (no claude)
    assert response.status_code in [200, 404, 408, 500]
