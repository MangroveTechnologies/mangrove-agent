"""benchmark_service — buy-and-hold return for an asset over a window.

A strategy's return means little without what simply holding the asset did
over the same window. This computes it from the agent's existing OHLCV access
(``mangroveai.crypto_assets.get_ohlcv``): first close to last close, as a
percentage on the same 0-100 scale MangroveAI backtest metrics use.

The upstream endpoint takes a lookback in days ending now and returns the
provider's native bars (daily for the default provider). So an explicit
window is served by fetching back to its start and slicing; the response
always reports the window the bars actually covered, which can be shorter
than the one requested when history is thin.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from statistics import median
from typing import Any

from src.shared.clients.mangrove import mangrove_ai_client
from src.shared.errors import InsufficientData, SdkError, ValidationError
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
    if start_date or end_date:
        if not (start_date and end_date):
            raise ValidationError("Pass both start_date and end_date, or lookback_days.")
        start = _parse_when(start_date, "start_date")
        end = min(_parse_when(end_date, "end_date"), now)
        if end <= start:
            raise ValidationError(f"end_date ({end_date}) must be after start_date ({start_date}).")
        return start, end, "explicit"
    if lookback_days is None:
        raise ValidationError("Pass start_date and end_date, or lookback_days.")
    if lookback_days < 1 or lookback_days > MAX_LOOKBACK_DAYS:
        raise ValidationError(f"lookback_days must be between 1 and {MAX_LOOKBACK_DAYS}.")
    return now - timedelta(days=lookback_days), now, "trailing"


def _bar_label(gaps_hours: list[float]) -> str | None:
    if not gaps_hours:
        return None
    gap = median(gaps_hours)
    for label, hours in (("5m", 5 / 60), ("15m", 0.25), ("30m", 0.5), ("1h", 1), ("4h", 4), ("1d", 24), ("1w", 168)):
        if abs(gap - hours) <= hours * 0.1:
            return label
    return f"{round(gap, 2)}h"


def _bars(payload: Any) -> list[tuple[datetime, float]]:
    data = payload.get("data") if isinstance(payload, dict) else None
    rows: list[tuple[datetime, float]] = []
    for row in data or []:
        if not isinstance(row, dict) or row.get("close") is None or row.get("timestamp") is None:
            continue
        try:
            close = float(row["close"])
            ts = _parse_when(row["timestamp"], "timestamp")
        except (TypeError, ValueError, ValidationError):
            continue
        if math.isfinite(close):
            rows.append((ts, close))
    rows.sort(key=lambda r: r[0])
    return rows


def get_benchmark(
    asset: str,
    start_date: str | None = None,
    end_date: str | None = None,
    lookback_days: int | None = None,
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

    now = datetime.now(timezone.utc)
    days = min(MAX_LOOKBACK_DAYS, max(1, math.ceil((now - start).total_seconds() / 86400) + 1))
    try:
        raw = mangrove_ai_client().crypto_assets.get_ohlcv(symbol=symbol, days=days)
    except Exception as exc:  # noqa: BLE001 — SDK raises assorted subclasses
        raise SdkError(f"crypto_assets.get_ohlcv failed for {symbol}: {exc}") from exc
    payload = raw.model_dump() if hasattr(raw, "model_dump") else raw

    # A daily bar stamped at midnight covers the day the window starts in.
    day_start = start.replace(hour=0, minute=0, second=0, microsecond=0)
    bars = [(ts, close) for ts, close in _bars(payload) if day_start <= ts <= end]
    if len(bars) < 2:
        raise InsufficientData(
            f"Only {len(bars)} {symbol} close(s) fall between {start.date()} and {end.date()}; "
            "a buy-and-hold return needs at least two.",
            suggestion="Widen the window, or check the asset symbol with get_market_data.",
        )
    (first_ts, first_close), (last_ts, last_close) = bars[0], bars[-1]
    if first_close <= 0:
        raise InsufficientData(f"{symbol}'s first close in the window is {first_close}; no return can be computed.")

    gaps = [(b[0] - a[0]).total_seconds() / 3600 for a, b in zip(bars, bars[1:])]
    covered_days = round((last_ts - first_ts).total_seconds() / 86400, 2)
    requested_days = round((end - start).total_seconds() / 86400, 2)
    out: dict[str, Any] = {
        "asset": symbol,
        "requested_window": {
            "kind": kind,
            "start": start.isoformat(),
            "end": end.isoformat(),
            "days": requested_days,
        },
        "covered_window": {
            "start": first_ts.isoformat(),
            "end": last_ts.isoformat(),
            "days": covered_days,
            "bars": len(bars),
            "bar_interval": _bar_label(gaps),
        },
        "first_close": first_close,
        "last_close": last_close,
        "buy_and_hold_return_pct": round((last_close - first_close) / first_close * 100, 4),
        "unit": "percent_0_100",
    }
    # One bar of slack at each end: the first daily bar opens at midnight of the
    # start day and the last one opens on the end day.
    bar_days = (median(gaps) / 24) if gaps else 1
    if covered_days + 2 * bar_days < requested_days:
        out["note"] = (
            f"History covers {covered_days} of the {requested_days} days requested; "
            "quote the covered window, not the requested one."
        )
    _log.info("benchmark.computed", asset=symbol, bars=len(bars), covered_days=covered_days,
              return_pct=out["buy_and_hold_return_pct"])
    return out


def benchmark_for_window(asset: str, resolved_window: dict[str, Any] | None) -> dict[str, Any]:
    """Best-effort benchmark for a backtest's resolved window.

    Never raises: a benchmark that cannot be fetched must not fail a backtest
    that already ran. Returns ``{"available": False, "reason": ...}`` instead.

    The reason is safe to return to API callers: it carries only messages this
    module writes itself (bad window, too few closes). Upstream and unexpected
    exceptions become a generic reason; their detail goes to the server log only.
    """
    window = resolved_window or {}
    try:
        if window.get("start_date") and window.get("end_date"):
            result = get_benchmark(asset, start_date=window["start_date"], end_date=window["end_date"])
        elif window.get("lookback_months"):
            result = get_benchmark(asset, lookback_days=int(window["lookback_months"]) * 30)
        else:
            return {"available": False, "reason": "the backtest reported no window to benchmark against"}
    except (ValidationError, InsufficientData) as exc:
        _log.info("benchmark.unavailable", asset=asset, error=exc.message)
        return {"available": False, "reason": exc.message}
    except Exception as exc:  # noqa: BLE001 — deliberately swallowed, reported as unavailable
        _log.warning("benchmark.unavailable", asset=asset, error=f"{type(exc).__name__}: {exc}")
        return {
            "available": False,
            "reason": "price history for the benchmark could not be fetched (data provider error)",
        }
    return {"available": True, **result}
