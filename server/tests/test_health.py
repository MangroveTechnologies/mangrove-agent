"""Health check endpoint tests."""
import os

os.environ.setdefault("ENVIRONMENT", "test")


def test_health_returns_200(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "healthy"
    assert "timestamp" in data


def test_health_reports_scheduler_state(client):
    from types import SimpleNamespace
    from unittest.mock import patch

    for running in [True, False]:
        with patch('src.services.scheduler_service.get_scheduler', return_value=SimpleNamespace(running=running)):
            assert client.get('/health').json()['scheduler_running'] is running
