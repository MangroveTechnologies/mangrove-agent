"""Health check endpoint."""
from datetime import datetime, timezone


def health_payload() -> dict:
    from src.services.scheduler_service import get_scheduler

    return {
        "scheduler_running": get_scheduler().running,
        "status": "healthy",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
