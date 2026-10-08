"""Compatibility envelope for the server-owned buy-and-hold benchmark."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from math import isfinite
from typing import Any

from src.shared.clients.mangrove import mangrove_ai_client
from src.shared.errors import AgentError, InsufficientData, SdkError, ValidationError, upstream_access_error
from src.shared.logging import get_logger

_log = get_logger(__name__)

#: Longest lookback one call will request from the provider.
MAX_LOOKBACK_DAYS = 3650


def _parse_when(value: str, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ValidationError(
            f"{field} must be an ISO date or datetime (e.g. 2026-01-01); got {value!r}."
        ) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _resolve_window(
    start_date: str | None, end_date: str | None, lookback_days: int | None,
) -> tuple[datetime, datetime, str]:
    now = datetime.now(timezone.utc)
    if (start_date or end_date) and lookback_days is not None:
        raise ValidationError("Pass explicit dates or lookback_days, not both.")
    if start_date or end_date:
        if not (start_date and end_date):
            raise ValidationError("Pass both start_date and end_date, or lookback_days.")
        start = _parse_when(start_date, "start_date")
        end = _parse_when(end_date, "end_date")
        if end > now:
            raise ValidationError("end_date must not be in the future.")
        if end <= start:
            raise ValidationError(f"end_date ({end_date}) must be after start_date ({start_date}).")
        return start, end, "explicit"
    if lookback_days is None:
        raise ValidationError("Pass start_date and end_date, or lookback_days.")
    if isinstance(lookback_days, bool) or not isinstance(lookback_days, int) or not 1 <= lookback_days <= MAX_LOOKBACK_DAYS:
        raise ValidationError(f"lookback_days must be between 1 and {MAX_LOOKBACK_DAYS}.")
    return now - timedelta(days=lookback_days), now, "trailing"


def get_benchmark(
    asset: str,
    start_date: str | None = None,
    end_date: str | None = None,
    lookback_days: int | None = None,
    *, base_token: str | None = None, quote_token: str | None = None,
    market_data_venue: str | None = None,
) -> dict[str, Any]:
    """Buy-and-hold return for ``asset`` over a window.

    Window: ``start_date`` + ``end_date`` (ISO), or ``lookback_days`` ending now.
    Raises ``ValidationError`` for a malformed window, ``SdkError`` when the data
    provider fails, and ``InsufficientData`` when fewer than two closes fall
    inside the window.
    """
    symbol = (asset or "").strip().upper()
    if not symbol:
        raise ValidationError("asset is required.")
    start, end, kind = _resolve_window(start_date, end_date, lookback_days)

    try:
        raw = mangrove_ai_client().backtesting.get_benchmark(
            symbol, start.isoformat(), end.isoformat(),
            **{key: value for key, value in {"base_token": base_token, "quote_token": quote_token,
               "market_data_venue": market_data_venue}.items() if value is not None})
    except AgentError:
        raise
    except Exception as exc:
        access_error = upstream_access_error(exc)
        if access_error is not None:
            raise access_error from None
        status = getattr(exc, "status_code", None)
        if status in (404, 422):
            raise InsufficientData("The requested market window has insufficient benchmark history.") from None
        if status == 400:
            raise ValidationError("The server rejected the benchmark window or market.") from None
        raise SdkError("The benchmark provider did not answer; retry later.") from None
    payload = raw.model_dump() if hasattr(raw, "model_dump") else raw
    if not isinstance(payload, dict):
        raise SdkError("The benchmark service returned an invalid response.")
    try:
        first = _parse_when(payload["start"], "start")
        last = _parse_when(payload["end"], "end")
        return_pct = payload["buy_and_hold_return_raw"]
        if isinstance(return_pct, bool) or not isinstance(return_pct, (int, float)) or not isfinite(return_pct) or payload["unit"] != "percent_0_100":
            raise ValueError("unsupported benchmark units")
        out = {
            **payload,
            "requested_window": {"kind": kind, "start": start.isoformat(),
                                 "end": end.isoformat(),
                                 "days": round((end - start).total_seconds() / 86400, 2)},
            "covered_window": {"start": first.isoformat(), "end": last.isoformat(),
                               "days": round((last - first).total_seconds() / 86400, 2),
                               "bars": payload["bars"], "bar_interval": payload.get("interval") or "1d"},
            "buy_and_hold_return_pct": round(float(return_pct), 4),
        }
    except (KeyError, TypeError, ValueError, ValidationError):
        raise SdkError("The benchmark service returned an invalid response.") from None
    if payload.get("partial"):
        out["note"] = (
            f"History covers {out['covered_window']['days']} of the "
            f"{out['requested_window']['days']} days requested; quote the covered window."
        )
    return out


def benchmark_for_window(asset: str, resolved_window: dict[str, Any] | None) -> dict[str, Any]:
    """Best-effort benchmark for a backtest's resolved window.

    Never raises: a benchmark that cannot be fetched must not fail a backtest
    that already ran. Returns ``{"available": False, "reason": ...}`` instead.

    The reason is safe to return to API callers: it carries only messages this
    module writes itself (bad window, too few closes). Upstream and unexpected
    exceptions become a generic reason; logs record only their type.
    """
    window = resolved_window or {}
    market = {key: window[key] for key in ("base_token", "quote_token", "market_data_venue")
              if window.get(key) is not None}
    try:
        if window.get("start_date") and window.get("end_date"):
            result = get_benchmark(asset, start_date=window["start_date"], end_date=window["end_date"], **market)
        elif window.get("lookback_months"):
            result = get_benchmark(asset, lookback_days=int(window["lookback_months"]) * 30, **market)
        else:
            return {"available": False, "reason": "the backtest reported no window to benchmark against"}
    except (ValidationError, InsufficientData) as exc:
        _log.info("benchmark.unavailable", asset=asset, error=exc.message)
        return {"available": False, "reason": exc.message}
    except Exception as exc:  # noqa: BLE001 — deliberately swallowed, reported as unavailable
        _log.warning("benchmark.unavailable", asset=asset, error_type=type(exc).__name__)
        return {
            "available": False,
            "reason": "price history for the benchmark could not be fetched (data provider error)",
        }
    return {"available": True, **result}
