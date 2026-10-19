"""Health check endpoint tests."""
import os
import time
from unittest.mock import patch

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.schedulers.base import STATE_PAUSED, STATE_RUNNING, STATE_STOPPED

os.environ.setdefault("ENVIRONMENT", "test")


def test_health_returns_200(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "healthy"
    assert data["scheduler_running"] is True
    assert data["scheduler_thread_alive"] is True
    assert data["scheduler_health_reason"] == "scheduler_running"
    assert "timestamp" in data


def test_health_reports_scheduler_state(client):
    from types import SimpleNamespace

    cases = [
        (STATE_STOPPED, False, False, "scheduler_stopped"),
        (STATE_RUNNING, True, True, "scheduler_running"),
        (STATE_PAUSED, True, False, "scheduler_paused"),
        (STATE_RUNNING, False, False, "scheduler_thread_not_alive"),
    ]
    for state, thread_alive, expected_running, reason in cases:
        scheduler = SimpleNamespace(
            state=state,
            _thread=SimpleNamespace(is_alive=lambda alive=thread_alive: alive),
        )
        with patch('src.services.scheduler_service.get_scheduler', return_value=scheduler):
            response = client.get('/health')
        assert response.status_code == (200 if expected_running else 503)
        assert response.json()['scheduler_running'] is expected_running
        assert response.json()['scheduler_thread_alive'] is thread_alive
        assert response.json()['scheduler_health_reason'] == reason


def test_health_detects_terminated_background_scheduler_thread(client):
    """A dead APScheduler thread can leave its state at STATE_RUNNING."""
    scheduler = BackgroundScheduler()
    scheduler._main_loop = lambda: None
    scheduler.start()
    try:
        deadline = time.monotonic() + 1
        while scheduler._thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.001)
        assert not scheduler._thread.is_alive()
        assert scheduler.running is True

        with patch('src.services.scheduler_service.get_scheduler', return_value=scheduler):
            response = client.get('/health')

        assert response.status_code == 503
        assert response.json()['status'] == 'unhealthy'
        assert response.json()['scheduler_running'] is False
        assert response.json()['scheduler_state'] == 'running'
        assert response.json()['scheduler_thread_alive'] is False
        assert response.json()['scheduler_health_reason'] == 'scheduler_thread_not_alive'
    finally:
        scheduler.shutdown(wait=False)
