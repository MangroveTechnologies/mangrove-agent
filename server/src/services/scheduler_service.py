"""scheduler_service — APScheduler wrapper.

Non-blocking by construction: BackgroundScheduler runs jobs in a
threadpool. HTTP requests (REST + MCP) are never delayed by in-flight
ticks. An integration test (tests/integration/test_scheduler_nonblocking.py)
enforces this.

Job store: the same SQLite file as the rest of the agent's data. Jobs
survive process restart.

Job defaults:
- coalesce=True       — if we miss ticks (app was down), run just one
                        catch-up, not every missed one
- max_instances=1     — never let two copies of the same job's callable
                        run in parallel
- misfire_grace_time=60 — allow up to 60s late-fire before skipping

External observability: for every job execution we emit
scheduler.job.fired (success) or scheduler.job.errored (failure) via an
event listener, so log tailers + the chat UI can see ticks happen
in real time.
"""
from __future__ import annotations

import time
from functools import lru_cache
from typing import Any

from apscheduler.events import EVENT_JOB_ERROR, EVENT_JOB_EXECUTED
from apscheduler.executors.pool import ThreadPoolExecutor
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from src.config import app_config
from src.shared.errors import SchedulerError
from src.shared.logging import get_logger

_log = get_logger(__name__)

# Strategy timeframe -> cron expression (minute hour day month day_of_week)
_TIMEFRAME_TO_CRON: dict[str, dict[str, str]] = {
    "1m": {"minute": "*/1"},
    "5m": {"minute": "*/5"},
    "15m": {"minute": "*/15"},
    "1h": {"minute": "0"},
    "4h": {"minute": "0", "hour": "*/4"},
    "1d": {"minute": "0", "hour": "0"},
}


def _jobstore_url() -> str:
    # APScheduler's SQLAlchemyJobStore needs a SQLAlchemy URL.
    # :memory: gets special-cased (SQLAlchemy wants "sqlite:///:memory:" to
    # share a single connection inside a process).
    db_path = str(app_config.DB_PATH)
    return f"sqlite:///{db_path}" if db_path != ":memory:" else "sqlite:///:memory:"


def _job_name(strategy_id: str) -> str:
    return f"eval-{strategy_id}"


@lru_cache(maxsize=1)
def get_scheduler() -> BackgroundScheduler:
    """Return the singleton scheduler. Lazy init.

    Call start() separately (from FastAPI lifespan startup). Call
    shutdown(wait=False) on app stop to avoid blocking on in-flight ticks.
    """
    scheduler = BackgroundScheduler(
        jobstores={"default": SQLAlchemyJobStore(url=_jobstore_url())},
        executors={"default": ThreadPoolExecutor(max_workers=10)},
        job_defaults={
            "coalesce": True,
            "max_instances": 1,
            "misfire_grace_time": 60,
        },
    )
    scheduler.add_listener(_on_job_event, EVENT_JOB_EXECUTED | EVENT_JOB_ERROR)
    return scheduler


def reset_scheduler_cache() -> None:
    """Clear the cached scheduler (test helper)."""
    try:
        sched = get_scheduler()
        if sched.running:
            sched.shutdown(wait=False)
    except Exception as exc:
        _log.warning("scheduler.shutdown.failed", error=str(exc))
    get_scheduler.cache_clear()


def _on_job_event(event: Any) -> None:
    """Event listener for JOB_EXECUTED / JOB_ERROR.

    This is how external observers (chat UI, log tail) know a tick
    actually fired. duration_ms is APScheduler-provided where available;
    otherwise we omit it.
    """
    strategy_id = None
    if event.job_id and event.job_id.startswith("eval-"):
        strategy_id = event.job_id.removeprefix("eval-")

    if event.exception:
        _log.error(
            "scheduler.job.errored",
            job_id=event.job_id,
            strategy_id=strategy_id,
            exception=str(event.exception),
        )
    else:
        _log.info(
            "scheduler.job.fired",
            job_id=event.job_id,
            strategy_id=strategy_id,
        )


def start() -> None:
    """Start the scheduler. Called from the FastAPI lifespan startup."""
    sched = get_scheduler()
    if not sched.running:
        sched.start()
    _log.info("scheduler.started", jobstore=_jobstore_url())


def shutdown() -> None:
    """Stop the scheduler without waiting on in-flight ticks."""
    sched = get_scheduler()
    if sched.running:
        sched.shutdown(wait=False)


def register_job(strategy_id: str, timeframe: str, callable_path: str) -> str:
    """Register (or replace) the cron job for a strategy.

    Args:
        strategy_id: used to form job_id (eval-<strategy_id>).
        timeframe: one of the keys in _TIMEFRAME_TO_CRON.
        callable_path: APScheduler-style module:attr reference to the tick
                       function, e.g. "src.services.strategy_service:tick".
                       Note the colon separator (not a dot). The function
                       must accept a single `strategy_id` argument.

    Returns the job_id.
    """
    cron = _TIMEFRAME_TO_CRON.get(timeframe)
    if cron is None:
        raise SchedulerError(
            f"Unknown timeframe '{timeframe}'.",
            suggestion=f"Use one of: {sorted(_TIMEFRAME_TO_CRON)}.",
        )

    sched = get_scheduler()
    job_id = _job_name(strategy_id)
    trigger = CronTrigger(**cron)
    sched.add_job(
        func=callable_path,
        trigger=trigger,
        args=[strategy_id],
        id=job_id,
        replace_existing=True,
        coalesce=True,
        max_instances=1,
    )

    _log.info(
        "scheduler.job.registered",
        job_id=job_id,
        strategy_id=strategy_id,
        timeframe=timeframe,
        cron=cron,
    )
    return job_id


def cancel_job(strategy_id: str) -> bool:
    """Cancel the strategy's cron job. Returns True if a job was removed."""
    sched = get_scheduler()
    job_id = _job_name(strategy_id)
    try:
        sched.remove_job(job_id)
    except Exception:
        # Job wasn't there; not an error.
        return False
    _log.info("scheduler.job.cancelled", job_id=job_id, strategy_id=strategy_id)
    return True


def list_active_jobs() -> list[dict]:
    """Return a summary of all active jobs.

    Used by GET /status to show active_cron_jobs and by debugging tools.
    """
    sched = get_scheduler()
    out: list[dict] = []
    for job in sched.get_jobs():
        strategy_id = job.id.removeprefix("eval-") if job.id.startswith("eval-") else None
        out.append({
            "job_id": job.id,
            "strategy_id": strategy_id,
            "next_run_at": job.next_run_time.isoformat() if job.next_run_time else None,
            "cron_expression": str(job.trigger),
        })
    return out


def active_job_count() -> int:
    """Cheap count of active jobs (for GET /status)."""
    return len(get_scheduler().get_jobs())


# Exposed for tests that want to wait on a job without sleeping.
def _wait_for_job(job_id: str, timeout_s: float = 5.0, poll_s: float = 0.05) -> bool:
    """Poll until the job has fired at least once. Returns True on success."""
    sched = get_scheduler()
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        job = sched.get_job(job_id)
        if job is None:
            return False
        # If the next_run_time has advanced past the first scheduled fire,
        # we know it has fired at least once. Also if job is done.
        time.sleep(poll_s)
    return True
