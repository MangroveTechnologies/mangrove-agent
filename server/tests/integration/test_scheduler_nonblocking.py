"""Non-blocking integration test for scheduler_service.

Proves that an in-flight cron tick does NOT delay HTTP requests.

Scenario:
- Register a job whose callable sleeps for 3 seconds.
- Start the scheduler; immediately trigger a manual fire.
- While the tick is still sleeping in the threadpool, hit GET /health
  10 times over a ~1-second window.
- Every request must return in well under the threshold (<100 ms on a
  reasonable machine).
- After the sleep finishes, assert _SLOW_TICK_DONE is set (proof the
  job callable actually ran in the threadpool).

The scheduler.job.fired log event is verified separately in
tests/unit/test_scheduler_service.py via a direct listener call.
"""
from __future__ import annotations

import os
import threading
import time
from datetime import datetime

os.environ.setdefault("ENVIRONMENT", "test")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

# Module-level flag we can flip from the test; the job callable reads it.
_SLOW_TICK_DONE = threading.Event()


def _slow_tick(strategy_id: str) -> None:
    """Callable that sleeps for 3 seconds then sets the done flag.

    The scheduler threadpool runs this; the request path must NOT wait
    for it.
    """
    time.sleep(3.0)
    _SLOW_TICK_DONE.set()


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    db_file = tmp_path / "sched_nb.db"
    from src.config import app_config
    from src.services import scheduler_service as ss
    from src.shared.db import sqlite as db_mod

    monkeypatch.setattr(app_config, "DB_PATH", str(db_file))
    db_mod.reset_connection()
    ss.reset_scheduler_cache()

    from src.shared.db.sqlite import init_db
    init_db()
    yield db_file
    ss.reset_scheduler_cache()
    db_mod.reset_connection()


def test_in_flight_tick_does_not_block_requests(tmp_db):
    """HTTP /health stays fast while a 3-second tick is running."""
    from src.app import create_app
    app = create_app()
    from src.services import scheduler_service as ss

    _SLOW_TICK_DONE.clear()

    with TestClient(app) as client:
        # Start the scheduler and register the slow job.
        ss.start()
        ss.register_job("slow-strategy", "1m", "tests.integration.test_scheduler_nonblocking:_slow_tick")

        # Fire the job now (without waiting for the next cron tick).
        sched = ss.get_scheduler()
        sched.modify_job("eval-slow-strategy", next_run_time=datetime.now())

        # Give APScheduler ~150ms to pick up and dispatch to threadpool.
        time.sleep(0.15)
        assert not _SLOW_TICK_DONE.is_set(), (
            "tick must still be running — if it's already done, the test is meaningless"
        )

        # Hammer /health 10 times; each must return fast.
        latencies_ms: list[float] = []
        start_window = time.monotonic()
        for _ in range(10):
            t0 = time.monotonic()
            r = client.get("/health")
            latencies_ms.append((time.monotonic() - t0) * 1000)
            assert r.status_code == 200
            assert not _SLOW_TICK_DONE.is_set(), (
                "the slow tick should STILL be running while we make these calls"
            )

        window_s = time.monotonic() - start_window
        assert window_s < 1.0, f"10 /health calls took {window_s:.2f}s — something is blocking"

        # A BLOCKED request would take ~3s (the tick's sleep), so the
        # discriminating signal is orders of magnitude, not milliseconds.
        # Assert on the median (typical latency stays snappy) and a loose
        # per-call ceiling — a single 200-300ms hiccup from a loaded CI
        # runner must not fail the suite (it did: 218ms on 2026-07-12),
        # but anything approaching the tick duration still fails hard.
        latencies_ms.sort()
        median_ms = latencies_ms[len(latencies_ms) // 2]
        assert median_ms < 200, (
            f"median request took {median_ms:.1f}ms — cron tick is blocking the request path"
        )
        assert latencies_ms[-1] < 1000, (
            f"slowest request took {latencies_ms[-1]:.1f}ms — cron tick is blocking the request path"
        )

        # Now wait for the tick to finish.
        assert _SLOW_TICK_DONE.wait(timeout=6.0), "slow tick never completed"

        ss.shutdown()
