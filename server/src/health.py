"""Health check endpoint."""
from datetime import datetime, timezone

from apscheduler.schedulers.base import STATE_PAUSED, STATE_RUNNING, STATE_STOPPED


def _scheduler_health(scheduler) -> dict:
    """Describe whether the in-process scheduler can dispatch jobs."""
    state = getattr(scheduler, "state", None)
    state_name = {
        STATE_STOPPED: "stopped",
        STATE_RUNNING: "running",
        STATE_PAUSED: "paused",
    }.get(state, "unknown")

    # APScheduler's `running` property only checks state; BackgroundScheduler's
    # private thread can die while state remains RUNNING, so check that thread.
    thread = getattr(scheduler, "_thread", None)
    thread_alive = thread.is_alive() if thread is not None else False
    running = state == STATE_RUNNING and thread_alive

    if running:
        reason = "scheduler_running"
    elif state == STATE_PAUSED:
        reason = "scheduler_paused"
    elif state == STATE_STOPPED:
        reason = "scheduler_stopped"
    elif state == STATE_RUNNING and not thread_alive:
        reason = "scheduler_thread_not_alive"
    else:
        reason = "scheduler_state_unknown"

    return {
        "scheduler_running": running,
        "scheduler_state": state_name,
        "scheduler_thread_alive": thread_alive,
        "scheduler_health_reason": reason,
    }


def health_payload() -> dict:
    from src.services.scheduler_service import get_scheduler

    scheduler_health = _scheduler_health(get_scheduler())
    return {
        **scheduler_health,
        "status": "healthy" if scheduler_health["scheduler_running"] else "unhealthy",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
