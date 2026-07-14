"""portfolio_risk_service — agent-side portfolio kill switch (mangrove-agent#146).

The MangroveAI engine enforces PER-STRATEGY risk (position/daily/cooldown gates
and a per-strategy max-drawdown breaker) -- but it only ever sees one strategy's
account per evaluation. Aggregate drawdown across the whole live book is only
measurable HERE, because this agent owns every live strategy's allocation,
trades, and positions in its local SQLite.

This module maintains a single portfolio high-water mark and a LATCHED trip:

    book_value = SUM(active live allocation amounts)
               + SUM(realized p_and_l over live strategies' trades)

On every live tick, ``check_before_live_execution`` advances the high-water mark
and, if drawdown from it reaches ``PORTFOLIO_MAX_DRAWDOWN_PCT`` (default 0.30),
TRIPS: every live strategy is set inactive and the latch is set. Unlike the
per-strategy engine breaker (which cools down and re-baselines automatically),
the portfolio switch does NOT auto-resume -- a human must call ``reset()``.
This is the go-trader portfolio-kill-switch model (see the engine-side research
doc docs/research/kill-switch-circuit-breakers.md in MangroveAI).

Realized-P&L basis is deliberate: no per-tick price fetches, deterministic, and
the per-strategy engine breaker already covers each strategy's open-position
(unrealized) drawdown via mark-to-market.
"""
from __future__ import annotations

from datetime import datetime, timezone

from src.config import app_config
from src.shared.db.sqlite import get_connection
from src.shared.logging import get_logger

_log = get_logger(__name__)

# Fallback when PORTFOLIO_MAX_DRAWDOWN_PCT is not set in config. 30% aggregate
# drawdown across the live book -> pause everything for human review.
_DEFAULT_MAX_DRAWDOWN_PCT = 0.30


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _limit() -> float:
    """Portfolio drawdown limit as a fraction (0.30 = 30%). Config-driven."""
    return float(getattr(app_config, "PORTFOLIO_MAX_DRAWDOWN_PCT", None) or _DEFAULT_MAX_DRAWDOWN_PCT)


def _get_state() -> dict:
    row = get_connection().execute(
        "SELECT high_water_mark, tripped, tripped_at, tripped_reason, updated_at "
        "FROM portfolio_risk WHERE id = 1"
    ).fetchone()
    # Defensive: the migration seeds row 1, but never assume.
    if row is None:
        return {"high_water_mark": 0.0, "tripped": False, "tripped_at": None,
                "tripped_reason": None, "updated_at": None}
    return {
        "high_water_mark": float(row["high_water_mark"]),
        "tripped": bool(row["tripped"]),
        "tripped_at": row["tripped_at"],
        "tripped_reason": row["tripped_reason"],
        "updated_at": row["updated_at"],
    }


def _update(**fields) -> None:
    fields["updated_at"] = _now()
    cols = ", ".join(f"{k} = ?" for k in fields)
    conn = get_connection()
    conn.execute(f"UPDATE portfolio_risk SET {cols} WHERE id = 1", tuple(fields.values()))
    conn.commit()


def compute_book_value() -> float:
    """Realized-P&L book value across ACTIVE LIVE strategies.

    committed capital (active live allocations) + realized P&L (closed trades of
    strategies currently in 'live'). A strategy leaving 'live' drops out of both
    sums -- callers re-baseline the high-water mark on those transitions so a
    deliberate deactivation is not read as portfolio drawdown.
    """
    conn = get_connection()
    committed = conn.execute(
        """SELECT COALESCE(SUM(a.amount), 0) AS v
             FROM allocations a
             JOIN strategies s ON s.id = a.strategy_id
            WHERE a.active = 1 AND s.status = 'live'"""
    ).fetchone()["v"]
    realized = conn.execute(
        """SELECT COALESCE(SUM(t.p_and_l), 0) AS v
             FROM trades t
             JOIN strategies s ON s.id = t.strategy_id
            WHERE s.status = 'live' AND t.p_and_l IS NOT NULL"""
    ).fetchone()["v"]
    return float(committed) + float(realized)


def get_status() -> dict:
    """Current portfolio risk state for GET /status and diagnostics."""
    st = _get_state()
    book = compute_book_value()
    hwm = max(st["high_water_mark"], book if not st["tripped"] else st["high_water_mark"])
    drawdown = (hwm - book) / hwm if hwm > 0 else 0.0
    return {
        "tripped": st["tripped"],
        "tripped_at": st["tripped_at"],
        "tripped_reason": st["tripped_reason"],
        "high_water_mark": hwm,
        "book_value": book,
        "drawdown": round(drawdown, 4),
        "max_drawdown_limit": _limit(),
    }


def rebaseline(reason: str = "live_membership_change") -> None:
    """Reset the high-water mark to the current book value.

    Called whenever the set of live strategies changes (a strategy enters or
    leaves 'live'), so adding/removing committed capital -- or removing a
    strategy's realized P&L from the sum -- does not register as drawdown.
    No-op while latched: a tripped switch must not silently re-baseline itself.
    """
    st = _get_state()
    if st["tripped"]:
        return
    book = compute_book_value()
    _update(high_water_mark=book)
    _log.info("portfolio_risk.rebaselined", book_value=book, reason=reason)


def check_before_live_execution() -> dict:
    """Called at the top of every LIVE tick, before placing orders.

    Advances the high-water mark and trips if drawdown crosses the limit.
    Returns ``{"allowed": bool, ...}``; ``allowed=False`` means the caller must
    NOT place new orders (already tripped, or just tripped this call).
    """
    st = _get_state()
    if st["tripped"]:
        return {"allowed": False, "tripped": True, "reason": st["tripped_reason"],
                "high_water_mark": st["high_water_mark"]}

    book = compute_book_value()
    hwm = max(st["high_water_mark"], book)
    drawdown = (hwm - book) / hwm if hwm > 0 else 0.0
    limit = _limit()

    if hwm > 0 and drawdown >= limit:
        reason = (f"portfolio drawdown {drawdown:.1%} >= limit {limit:.0%} "
                  f"(book_value={book:.2f}, high_water_mark={hwm:.2f})")
        _trip(reason=reason, drawdown=drawdown, book=book, hwm=hwm)
        return {"allowed": False, "tripped": True, "reason": reason,
                "drawdown": round(drawdown, 4), "book_value": book, "high_water_mark": hwm}

    # Persist any new peak so the next tick measures against it.
    if hwm > st["high_water_mark"]:
        _update(high_water_mark=hwm)
    return {"allowed": True, "tripped": False, "drawdown": round(drawdown, 4),
            "book_value": book, "high_water_mark": hwm}


def _trip(*, reason: str, drawdown: float, book: float, hwm: float) -> None:
    """Latch the switch and pause every live strategy. Idempotent-safe."""
    _update(tripped=1, tripped_at=_now(), tripped_reason=reason, high_water_mark=hwm)
    _log.error("portfolio_risk.tripped", drawdown=round(drawdown, 4),
               book_value=book, high_water_mark=hwm, reason=reason)
    _pause_all_live(reason)


def _pause_all_live(reason: str) -> None:
    """Set every live strategy inactive (cancels its cron + releases allocation).

    Reuses strategy_service.update_status (single source of truth for lifecycle)
    so scheduler + allocation + upstream sync all stay consistent. A failure on
    one strategy is logged and does NOT block the others -- and the latch itself
    (checked first on every tick) already prevents any execution regardless.
    """
    from src.services import strategy_service  # lazy: break import cycle

    conn = get_connection()
    live_ids = [r["id"] for r in conn.execute(
        "SELECT id FROM strategies WHERE status = 'live'"
    ).fetchall()]
    for sid in live_ids:
        try:
            strategy_service.update_status(
                sid, strategy_service.StrategyStatusUpdate(status="inactive", confirm=True)
            )
            _log.warning("portfolio_risk.paused_strategy", strategy_id=sid, reason=reason)
        except Exception as e:  # noqa: BLE001
            _log.error("portfolio_risk.pause_failed", strategy_id=sid, exception=str(e))


def reset() -> dict:
    """Clear the latch and re-baseline to current book value (human re-activation).

    The switch is intentionally latched: only an explicit human action clears it.
    After reset the operator can promote strategies back to live; the high-water
    mark starts fresh from the current book so it does not immediately re-trip.
    """
    book = compute_book_value()
    _update(tripped=0, tripped_at=None, tripped_reason=None, high_water_mark=book)
    _log.warning("portfolio_risk.reset", book_value=book)
    return get_status()
