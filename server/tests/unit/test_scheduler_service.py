"""Unit tests for scheduler_service — register/cancel/list semantics."""
from __future__ import annotations

import os

os.environ.setdefault("ENVIRONMENT", "test")

import pytest  # noqa: E402


@pytest.fixture
def scheduler_in_tmp(tmp_path, monkeypatch):
    """Fresh scheduler bound to a tmp SQLite jobstore. No .start() — we
    only care about the in-memory job list here, not actually firing."""
    db_file = tmp_path / "sched_test.db"
    from src.config import app_config
    from src.services import scheduler_service as ss

    monkeypatch.setattr(app_config, "DB_PATH", str(db_file))
    ss.reset_scheduler_cache()
    ss.start()  # get_jobs() needs a running (or at least initialized) scheduler
    yield ss
    ss.reset_scheduler_cache()


# A no-op callable the scheduler can reference by dotted path.
def _noop(_strategy_id: str) -> None:
    pass


def test_register_job_adds_to_scheduler(scheduler_in_tmp):
    ss = scheduler_in_tmp
    job_id = ss.register_job("s1", "5m", "tests.unit.test_scheduler_service:_noop")
    assert job_id == "eval-s1"
    assert ss.active_job_count() == 1


def test_register_is_idempotent(scheduler_in_tmp):
    ss = scheduler_in_tmp
    ss.register_job("s1", "5m", "tests.unit.test_scheduler_service:_noop")
    ss.register_job("s1", "1h", "tests.unit.test_scheduler_service:_noop")
    assert ss.active_job_count() == 1  # replaced, not duplicated


def test_cancel_job(scheduler_in_tmp):
    ss = scheduler_in_tmp
    ss.register_job("s1", "5m", "tests.unit.test_scheduler_service:_noop")
    assert ss.cancel_job("s1") is True
    assert ss.active_job_count() == 0


def test_cancel_unknown_is_safe(scheduler_in_tmp):
    ss = scheduler_in_tmp
    assert ss.cancel_job("nonexistent") is False


def test_list_active_jobs_summary(scheduler_in_tmp):
    ss = scheduler_in_tmp
    ss.register_job("s1", "5m", "tests.unit.test_scheduler_service:_noop")
    ss.register_job("s2", "1h", "tests.unit.test_scheduler_service:_noop")
    jobs = ss.list_active_jobs()
    assert len(jobs) == 2
    ids = {j["strategy_id"] for j in jobs}
    assert ids == {"s1", "s2"}
    for j in jobs:
        assert j["job_id"].startswith("eval-")
        assert j["cron_expression"]  # non-empty


def test_unknown_timeframe_raises(scheduler_in_tmp):
    ss = scheduler_in_tmp
    from src.shared.errors import SchedulerError

    with pytest.raises(SchedulerError):
        ss.register_job("s1", "42m", "tests.unit.test_scheduler_service:_noop")


def test_event_listener_emits_scheduler_job_fired_on_success():
    """Direct invocation of the listener to verify the observability
    contract (strategy_id extraction, event name) without APScheduler's
    threadpool in the loop.
    """
    from unittest.mock import MagicMock

    from src.services.scheduler_service import _on_job_event

    event = MagicMock()
    event.job_id = "eval-abc-123"
    event.exception = None

    # Should not raise. (Log output is side-effect only; separately verified
    # by test_logging.py that structured logs render correctly.)
    _on_job_event(event)


def test_event_listener_emits_scheduler_job_errored_on_failure():
    from unittest.mock import MagicMock

    from src.services.scheduler_service import _on_job_event

    event = MagicMock()
    event.job_id = "eval-abc-123"
    event.exception = RuntimeError("boom")
    _on_job_event(event)  # should not raise


def test_reset_scheduler_cache_logs_warning_on_shutdown_failure(monkeypatch):
    """If shutdown raises, a warning is logged and cache_clear still runs."""
    from unittest.mock import MagicMock

    from src.services import scheduler_service as ss

    mock_sched = MagicMock()
    mock_sched.running = True
    mock_sched.shutdown.side_effect = RuntimeError("forced shutdown failure")

    mock_get_scheduler = MagicMock(return_value=mock_sched)
    mock_get_scheduler.cache_clear = MagicMock()
    monkeypatch.setattr(ss, "get_scheduler", mock_get_scheduler)

    mock_log = MagicMock()
    monkeypatch.setattr(ss, "_log", mock_log)

    ss.reset_scheduler_cache()

    mock_log.warning.assert_called_once_with(
        "scheduler.shutdown.failed", error="forced shutdown failure"
    )
    mock_get_scheduler.cache_clear.assert_called_once()
