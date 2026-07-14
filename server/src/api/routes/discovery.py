"""Discovery endpoints — free, no auth.

- GET /api/v1/agent/status: server + SDK versions, catalog counts, wallet
  count, strategies grouped by status, active cron jobs, db path, uptime.
- GET /api/v1/agent/tools: MCP tool catalog (auto-populated by
  src/mcp/tools.py at registration time).

/health lives in src/app.py (template-provided).
"""
from __future__ import annotations

import importlib.metadata
import time
from collections import Counter
from datetime import datetime, timezone

from fastapi import APIRouter

from src.config import app_config
from src.mcp.registry import list_tools as list_registered_tools
from src.services.scheduler_service import active_job_count
from src.shared.db.sqlite import get_connection

router = APIRouter()

_STARTUP_MONOTONIC = time.monotonic()

_CATALOG_CACHE: dict | None = None
_CATALOG_CACHE_SET_AT: float = 0.0
_CATALOG_CACHE_TTL_SEC = 600  # 10 min


def _pkg_version(name: str) -> str | None:
    """Return installed package version, or None if not importable."""
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return None


def _fetch_catalog_counts() -> dict:
    """Live catalog lookup against the mangroveai SDK. Resilient on failure."""
    counts: dict = {
        "signals_total": None,
        "kb_indicators_total": None,
        "kb_tags_total": None,
        "as_of": datetime.now(timezone.utc).isoformat(),
        "error": None,
    }
    try:
        from src.shared.clients.mangrove import mangrove_ai_client
        client = mangrove_ai_client()
    except Exception as e:  # noqa: BLE001
        counts["error"] = f"client_init_failed: {str(e)[:200]}"
        return counts

    try:
        counts["signals_total"] = sum(1 for _ in client.signals.list_iter(limit_per_page=100))
    except Exception as e:  # noqa: BLE001
        counts["error"] = f"signals_list_failed: {str(e)[:200]}"

    try:
        counts["kb_indicators_total"] = len(list(client.kb.indicators.list()))
    except Exception as e:  # noqa: BLE001
        if counts["error"] is None:
            counts["error"] = f"kb_indicators_list_failed: {str(e)[:200]}"

    try:
        counts["kb_tags_total"] = len(list(client.kb.tags.list()))
    except Exception as e:  # noqa: BLE001
        if counts["error"] is None:
            counts["error"] = f"kb_tags_list_failed: {str(e)[:200]}"

    return counts


def _catalog_counts() -> dict:
    """Return cached catalog counts. Refreshes every _CATALOG_CACHE_TTL_SEC."""
    global _CATALOG_CACHE, _CATALOG_CACHE_SET_AT
    now = time.monotonic()
    if _CATALOG_CACHE is not None and (now - _CATALOG_CACHE_SET_AT) < _CATALOG_CACHE_TTL_SEC:
        return _CATALOG_CACHE
    _CATALOG_CACHE = _fetch_catalog_counts()
    _CATALOG_CACHE_SET_AT = now
    return _CATALOG_CACHE


def reset_catalog_cache() -> None:
    """Clear cached catalog counts. Used by tests."""
    global _CATALOG_CACHE, _CATALOG_CACHE_SET_AT
    _CATALOG_CACHE = None
    _CATALOG_CACHE_SET_AT = 0.0


@router.get(
    "/status",
    summary="Agent status",
    description=(
        "Server + SDK versions, signal/KB catalog counts, wallet count, "
        "strategies by status, active cron jobs, db path, uptime. Free, no auth."
    ),
    tags=["discovery"],
)
async def status() -> dict:
    conn = get_connection()
    wallets_count = conn.execute("SELECT COUNT(*) AS c FROM wallets").fetchone()["c"]
    # Exclude the 'user-initiated' placeholder (plumbing for /dex/swap FK).
    strategy_rows = conn.execute(
        "SELECT status FROM strategies WHERE id != 'user-initiated'",
    ).fetchall()
    counts = Counter(r["status"] for r in strategy_rows)

    server_version = _pkg_version("mangrove-agent") or "0.1.0"

    return {
        "version": server_version,
        "server_version": server_version,
        "sdk_versions": {
            "mangroveai": _pkg_version("mangroveai"),
            "mangrovemarkets": _pkg_version("mangrovemarkets"),
        },
        "catalog": _catalog_counts(),
        "wallets_count": wallets_count,
        "strategies": {
            "draft": counts.get("draft", 0),
            "inactive": counts.get("inactive", 0),
            "paper": counts.get("paper", 0),
            "live": counts.get("live", 0),
            "archived": counts.get("archived", 0),
        },
        "active_cron_jobs": active_job_count(),
        "portfolio_risk": _portfolio_risk_status(),
        "db_path": str(app_config.DB_PATH),
        "uptime_seconds": int(time.monotonic() - _STARTUP_MONOTONIC),
    }


def _portfolio_risk_status() -> dict:
    """Portfolio kill-switch state for /status (#146). Never fail /status over it."""
    try:
        from src.services import portfolio_risk_service
        return portfolio_risk_service.get_status()
    except Exception:  # noqa: BLE001
        return {"tripped": None, "error": "unavailable"}


@router.get(
    "/tools",
    summary="MCP tool catalog",
    description="Full catalog of registered MCP tools: name, description, parameters, access tier, pricing. Free, no auth.",
    tags=["discovery"],
)
async def tools() -> dict:
    return {"tools": list_registered_tools()}
