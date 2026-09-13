"""backtest_service — quick + full backtest orchestration + IRR ranking.

Phase 3 Task 3.2. Thin orchestrator over mangroveai.backtesting.run().
The SDK exposes a single run() today; Tim is adding a dedicated quick
mode on the server. Until that ships, "quick" and "full" here both hit
run() — the distinction is in how we summarize results (quick = metrics
only; full = metrics + trade_history).

Filter + rank (same bars as the backtest verdict, backtest_verdict.py):
- Drop candidates with total_trades < BACKTEST_MIN_TRADES (default 10)
- Drop candidates with win_rate below threshold_spec.json `min_win_rate`
  (0.25; the SDK's 0-100 win_rate is converted before comparing)
- Sort survivors by irr_annualized DESC

Metric key lookup is defensive: the SDK's metrics dict field names may
vary. We look up several common spellings and return 0.0 if none present.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from mangrove_ai.exceptions import NotFoundError
from mangrove_ai.models import BacktestRequest
from mangrove_ai.models.backtesting import BacktestResult
from pydantic import BaseModel

from src.config import app_config
from src.services import backtest_verdict
from src.services.candidate_generator import StrategyCandidate
from src.shared import timeframes
from src.shared.clients.mangrove import mangrove_ai_client
from src.shared.errors import BacktestNotFound, SdkError
from src.shared.logging import get_logger

_log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Canonical trading defaults — fetched from MangroveAI's free public API.
#
# Endpoint: GET https://api.mangrovedeveloper.ai/api/v1/config/trading-defaults
# (no auth required — public configuration). Wrapped by the mangroveai SDK as
# `client.config.trading_defaults()` (mangroveai >= 0.3.0).
#
# Replaces the previous local copy at server/src/services/data/
# trading_defaults.json which silently drifted from canon. Lazy + cached
# for the process lifetime: first call hits the API, all subsequent calls
# return the cached dict. If the API is unreachable (offline dev, local-
# only run, etc.), we fall back to a hardcoded v3.5.0 snapshot so server
# startup doesn't crash.
#
# To pick up canon updates, restart the server after the API returns the
# new values. `mcp__mangrove-agent__status` and `/api/v1/agent/status` both
# show which version the cache holds.
# ---------------------------------------------------------------------------

# Hardcoded fallback canon — used ONLY if the SDK fetch fails. Mirrors
# MangroveAI v3.5.0's trading_defaults.json (commit 89d6713). Refresh when
# the canon shape changes (rare); values here are functionally safe so a
# fallback-using server still produces sane backtests until reconnected.
_FALLBACK_TRADING_DEFAULTS: dict[str, Any] = {
    "description": "Hardcoded fallback (v3.5.0 snapshot) — used only when SDK fetch from /api/v1/config/trading-defaults fails.",
    "signal_defaults": {},
    "backtest_defaults": {"slippage_pct": 0.004, "fee_pct": 0.0085},
    "risk_management": {
        "max_risk_per_trade": 0.01,
        "reward_factor": 2,
        # Required in execution_config since MangroveAI v3.8.0 (no silent
        # default). The live canon ships it; this keeps the offline fallback
        # valid so backtests don't 500 when the canon fetch is unavailable.
        "position_size_calc": "v2",
        "atr_period": 14,
        # atr_width_dial stop model. The weighted_atr keys this used to carry
        # (atr_volatility_factor / atr_short_weight / atr_long_weight /
        # atr_cap_multiplier) are superseded and refused by strategies.create,
        # so a fallback-using server could not create any strategy.
        "stop_model": "atr_width_dial",
        "volatility_tolerance": 0.5,
        "min_stop_distance_pct": 0.005,
        "max_drawdown_limit": 0.2,
        "max_drawdown_halt_limit": 0.2,
    },
    "position_limits": {
        "initial_balance": 10000,
        "min_balance_threshold": 0.1,
        "min_trade_amount": 25,
        "max_open_positions": 10,
        "max_trades_per_day": 50,
        "max_units_per_trade": 1000000,
        "max_trade_amount": 10000000,
    },
    "volatility_settings": {
        "volatility_window": 24,
        "target_volatility": 0.1,
        "volatility_mode": "stddev",
        "enable_volatility_adjustment": False,
    },
    "trading_rules": {
        # cooldown_config only. The legacy scalars (cooldown_bars /
        # daily_momentum_limit / weekly_momentum_limit / max_hold_time_hours)
        # are deprecated in mangroveai and ignored by the engine whenever
        # cooldown_config is present (MangroveAI managers/risk_manager.py).
        # Every supported timeframe is keyed — the engine raises if the
        # strategy's primary timeframe is missing (30m/4h used to be).
        # Values mirror the live canon (2026-09).
        "cooldown_config": {
            "5m":  {"short_loss_limit": 4, "long_loss_limit": 6, "short_window_bars": 180, "long_window_bars": 480,
                    "short_cooldown_bars": 180, "long_cooldown_bars": 480},
            "15m": {"short_loss_limit": 4, "long_loss_limit": 6, "short_window_bars": 120, "long_window_bars": 320,
                    "short_cooldown_bars": 120, "long_cooldown_bars": 320},
            "30m": {"short_loss_limit": 4, "long_loss_limit": 6, "short_window_bars": 80,  "long_window_bars": 220,
                    "short_cooldown_bars": 80,  "long_cooldown_bars": 220},
            "1h":  {"short_loss_limit": 4, "long_loss_limit": 6, "short_window_bars": 48,  "long_window_bars": 144,
                    "short_cooldown_bars": 48,  "long_cooldown_bars": 144},
            "4h":  {"short_loss_limit": 4, "long_loss_limit": 6, "short_window_bars": 32,  "long_window_bars": 96,
                    "short_cooldown_bars": 32,  "long_cooldown_bars": 96},
            "1d":  {"short_loss_limit": 4, "long_loss_limit": 6, "short_window_bars": 20,  "long_window_bars": 60,
                    "short_cooldown_bars": 20,  "long_cooldown_bars": 60},
        },
    },
    "time_based_exits": {
        "max_hold_bars": 1000,
        "exit_on_loss_after_bars": 1000,
        "exit_on_profit_after_bars": 1000,
        "profit_threshold_pct": 0.05,
    },
}

_cached_trading_defaults: dict[str, Any] | None = None


def _get_trading_defaults() -> dict[str, Any]:
    """Fetch canon from MangroveAI's free public /api/v1/config/trading-defaults.

    Cached for the process lifetime after first successful fetch. Falls
    back to the hardcoded snapshot above on ANY of these failure modes:
      - mangrove_ai_client() raises (config not loaded, etc.)
      - .config attribute missing (older SDK without ConfigService — pre-0.3.0)
      - .trading_defaults() raises (network down, 5xx, etc.)
      - .trading_defaults() returns empty/non-dict/missing-required-sections
        (envelope-changed unexpectedly, partial response, etc.)

    Restart the server once API connectivity is restored to pick up
    canon updates — this function does not auto-retry.
    """
    global _cached_trading_defaults
    if _cached_trading_defaults is not None:
        return _cached_trading_defaults

    # Required top-level sections — used to validate the API response.
    # If any are missing, treat as a malformed fetch and fall back.
    _REQUIRED_SECTIONS = ("risk_management", "position_limits", "trading_rules")

    fetched: dict[str, Any] | None = None
    try:
        client = mangrove_ai_client()
        config_svc = getattr(client, "config", None)
        if config_svc is None:
            raise AttributeError("mangroveai client has no `config` service (need SDK >= 0.3.0)")
        fetched = config_svc.trading_defaults()
    except Exception as e:  # noqa: BLE001 — SDK / network / config errors all fall back
        _log.warning(
            "trading_defaults.fetch_failed",
            error=f"{type(e).__name__}: {e}",
            note="using hardcoded v3.5.0 fallback snapshot",
        )
        _cached_trading_defaults = _FALLBACK_TRADING_DEFAULTS
        return _cached_trading_defaults

    # Defensive shape check — empty/non-dict/missing sections all fall back.
    if not isinstance(fetched, dict) or not all(s in fetched for s in _REQUIRED_SECTIONS):
        _log.warning(
            "trading_defaults.fetch_malformed",
            type=type(fetched).__name__,
            keys=list(fetched.keys()) if isinstance(fetched, dict) else None,
            note="using hardcoded v3.5.0 fallback snapshot",
        )
        _cached_trading_defaults = _FALLBACK_TRADING_DEFAULTS
        return _cached_trading_defaults

    _log.info("trading_defaults.loaded_from_api", sections=list(fetched.keys()))
    _cached_trading_defaults = fetched
    return _cached_trading_defaults


def flattened_defaults() -> dict[str, Any]:
    """Flatten the trading_defaults sections into a single dict.

    Mirrors MangroveAI/domains/strategies/services.py:306-309 — the same
    sections in the same order, so a config override that works against
    the upstream copilot works identically here.
    """
    canon = _get_trading_defaults()
    out: dict[str, Any] = {}
    for section in (
        "risk_management",
        "position_limits",
        "volatility_settings",
        "trading_rules",
        "time_based_exits",
    ):
        section_data = canon.get(section) or {}
        out.update(section_data)
    return drop_legacy_cooldown_fields(out)


# Top-level cooldown fields superseded by `cooldown_config` (mangroveai
# DeprecationWarning; MangroveAI's RiskManager reads them ONLY when
# cooldown_config is None). The live canon still ships them for old clients.
LEGACY_COOLDOWN_FIELDS: tuple[str, ...] = (
    "cooldown_bars",
    "daily_momentum_limit",
    "weekly_momentum_limit",
    "max_hold_time_hours",
)


def drop_legacy_cooldown_fields(config: dict[str, Any]) -> dict[str, Any]:
    """Remove the deprecated cooldown scalars when cooldown_config covers them.

    Behaviour-neutral: with a non-empty cooldown_config the engine ignores
    the scalars (and strategies.create accepts cooldown_config alone). If
    cooldown_config is absent the scalars are the only cooldown source, so
    they are kept. Operates on the canon defaults only — a caller that
    explicitly passes one of these in a backtest `config` still sends it.
    """
    if isinstance(config.get("cooldown_config"), dict) and config["cooldown_config"]:
        return {k: v for k, v in config.items() if k not in LEGACY_COOLDOWN_FIELDS}
    return config


def backtest_cost_defaults() -> dict[str, Any]:
    """Return the slippage_pct / fee_pct defaults.

    These live under `backtest_defaults` in the canon (separate from the
    execution config sections) so they need a dedicated accessor when a
    caller wants to surface them.
    """
    return dict(_get_trading_defaults().get("backtest_defaults") or {})


def _resolve_window(
    timeframe: str,
    lookback_months: int | None,
    lookback_days: int | None = None,
    lookback_hours: int | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
) -> tuple[int | None, str | None, str | None]:
    """Resolve a lookback specification to (lookback_months, start_date, end_date).

    Precedence (most specific first):
      1. explicit start_date + end_date (pass-through)
      2. lookback_hours (converted to pinned ISO window ending now)
      3. lookback_days (same)
      4. lookback_months (pass-through — server converts using 30d/month)
      5. if none given, recommended by timeframe via
         `timeframes.recommended_lookback_months`

    Returns a tuple where the lookback_months entry is ``None`` whenever
    explicit dates are returned (matches BacktestRequest's "dates take
    precedence over lookback_months" contract).
    """
    # 1. pass-through for explicit dates
    if start_date and end_date:
        return None, start_date, end_date

    # 2/3. hours and days → compute ISO window ending now (UTC)
    if lookback_hours is not None and lookback_hours > 0:
        end = datetime.now(timezone.utc)
        start = end - timedelta(hours=lookback_hours)
        return None, start.isoformat(), end.isoformat()
    if lookback_days is not None and lookback_days > 0:
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=lookback_days)
        return None, start.isoformat(), end.isoformat()

    # 4. explicit months
    if lookback_months is not None and lookback_months > 0:
        return lookback_months, start_date, end_date

    # 5. auto by timeframe (matches MangroveAI prompt_builder.py defaults)
    return timeframes.recommended_lookback_months(timeframe), start_date, end_date


# `_DEFAULT_EXECUTION_CONFIG` removed — use `flattened_defaults()` + caller override.
# The old hardcoded dict had drifted from upstream trading_defaults.json
# (max_risk_per_trade 0.02 vs 0.01, max_open_positions 3 vs 10,
# max_trades_per_day 10 vs 50). Single source of truth now.


class CandidateBacktestResult(BaseModel):
    """Per-candidate outcome of quick_backtest_all."""

    candidate: StrategyCandidate
    success: bool
    irr_annualized: float
    win_rate: float
    total_trades: int
    sharpe_ratio: float
    max_drawdown: float
    net_pnl: float
    reject_reason: str | None = None  # filled after filter step
    raw_metrics: dict[str, Any] = {}
    error: str | None = None
    # Server-side run id (full backtests only) — read it back with get_backtest.
    backtest_id: str | None = None


def _metric(metrics: dict[str, Any] | None, *keys: str, default: float = 0.0) -> float:
    """Defensive metric lookup: try each key in order."""
    if not metrics:
        return default
    for k in keys:
        if k in metrics and metrics[k] is not None:
            try:
                return float(metrics[k])
            except (TypeError, ValueError):
                continue
    return default


def _int_metric(metrics: dict[str, Any] | None, *keys: str, default: int = 0) -> int:
    if not metrics:
        return default
    for k in keys:
        if k in metrics and metrics[k] is not None:
            try:
                return int(metrics[k])
            except (TypeError, ValueError):
                continue
    return default


def _build_request(
    candidate: StrategyCandidate,
    lookback_months: int | None,
    start_date: str | None = None,
    end_date: str | None = None,
    config: dict[str, Any] | None = None,
) -> BacktestRequest:
    """Build a BacktestRequest from a candidate + canonical trading defaults.

    `config` is merged over `flattened_defaults()` — the single source of
    truth is `data/trading_defaults.json`. Any key a caller passes in
    `config` wins. Keys the SDK doesn't recognize are forwarded anyway
    (BacktestRequest has `model_config={'extra': 'allow'}`, verified
    2026-04-22) — the server treats them as execution_config extras.

    Slippage, fee, and max_hold_time_hours are just keys in `config`
    now. Callers no longer need dedicated arguments for them.
    """
    # Reject unsupported timeframes up front (1m etc.) — see
    # src.shared.timeframes.canonicalize_timeframe for the whitelist.
    interval = timeframes.canonicalize_timeframe(candidate.timeframe)

    merged = {**flattened_defaults(), **(config or {})}

    strategy_json = json.dumps({
        "name": candidate.name,
        "asset": candidate.asset,
        "entry": candidate.entry,
        "exit": candidate.exit or [],
    })
    kwargs: dict[str, Any] = {
        "asset": candidate.asset,
        "interval": interval,
        "strategy_json": strategy_json,
        **merged,
        "lookback_months": lookback_months if not start_date else None,
        "start_date": start_date,
        "end_date": end_date,
    }
    return BacktestRequest(**kwargs)


def _summarize(
    candidate: StrategyCandidate,
    raw_result: Any,
) -> CandidateBacktestResult:
    """Translate an SDK BacktestResult into CandidateBacktestResult."""
    metrics: dict[str, Any] = getattr(raw_result, "metrics", None) or {}
    success = bool(getattr(raw_result, "success", False))
    return CandidateBacktestResult(
        candidate=candidate,
        success=success,
        irr_annualized=_metric(metrics, "irr_annualized", "irr", "annualized_return"),
        win_rate=_metric(metrics, "win_rate", "winrate"),
        total_trades=_int_metric(
            metrics,
            "total_trades",
            "trade_count",
            default=int(getattr(raw_result, "trade_count", None) or 0),
        ),
        sharpe_ratio=_metric(metrics, "sharpe_ratio", "sharpe"),
        max_drawdown=_metric(metrics, "max_drawdown", "maxdd"),
        net_pnl=_metric(metrics, "net_pnl", "total_pnl", "return"),
        raw_metrics=metrics,
        error=getattr(raw_result, "error", None),
    )


def quick_backtest_all(
    candidates: list[StrategyCandidate],
    lookback_months: int | None = None,
) -> list[CandidateBacktestResult]:
    """Run a backtest for every candidate. Per-candidate failures do not
    abort the batch — the result's .success and .error fields carry the
    outcome.

    If `lookback_months` is None, picks the timeframe-aware recommended
    default per `timeframes.recommended_lookback_months`. All candidates
    are assumed to share the same timeframe (they come from one
    candidate_generator.generate() call), so the first one drives the
    recommendation.
    """
    if lookback_months is None and candidates:
        lookback_months = timeframes.recommended_lookback_months(candidates[0].timeframe)
    if lookback_months is None:
        lookback_months = int(app_config.BACKTEST_DEFAULT_LOOKBACK_MONTHS)

    client = mangrove_ai_client()
    results: list[CandidateBacktestResult] = []
    for c in candidates:
        try:
            raw = client.backtesting.run(
                _build_request(c, lookback_months=lookback_months),
            )
            results.append(_summarize(c, raw))
        except Exception as e:  # noqa: BLE001 — SDK may raise arbitrary subclasses
            results.append(CandidateBacktestResult(
                candidate=c,
                success=False,
                irr_annualized=0.0,
                win_rate=0.0,
                total_trades=0,
                sharpe_ratio=0.0,
                max_drawdown=0.0,
                net_pnl=0.0,
                error=str(e),
            ))

    _log.info(
        "backtest.quick_batch_completed",
        n=len(candidates),
        succeeded=sum(1 for r in results if r.success),
    )
    return results


def filter_and_rank(
    results: list[CandidateBacktestResult],
    min_win_rate: float | None = None,
    min_trades: int | None = None,
) -> tuple[list[CandidateBacktestResult], list[CandidateBacktestResult]]:
    """Split results into (survivors, rejected), with rejected carrying a
    reject_reason. Survivors are sorted by irr_annualized DESC.

    The win-rate floor is the SAME bar the backtest verdict uses:
    `threshold_spec.json` `min_win_rate` (a decimal, 0.25). `min_win_rate`
    overrides it, also as a decimal. The SDK reports `win_rate` on a 0-100
    scale, so it is converted before comparing — the old
    `BACKTEST_MIN_WIN_RATE=0.51` compared 0-100 values against a decimal
    and therefore rejected nothing. `min_trades` defaults to
    `BACKTEST_MIN_TRADES`, the same floor behind INSUFFICIENT_TRADES.
    """
    if min_win_rate is None:
        min_win_rate = float(backtest_verdict.load_thresholds()["min_win_rate"])
    if min_trades is None:
        min_trades = int(app_config.BACKTEST_MIN_TRADES)

    survivors: list[CandidateBacktestResult] = []
    rejected: list[CandidateBacktestResult] = []

    for r in results:
        if not r.success:
            rejected.append(r.model_copy(update={"reject_reason": f"backtest failed: {r.error or 'unknown error'}"}))
            continue
        if r.total_trades < min_trades:
            rejected.append(r.model_copy(update={
                "reject_reason": f"total_trades {r.total_trades} < {min_trades}"
            }))
            continue
        if not backtest_verdict.passes_win_rate_floor(r.win_rate, min_win_rate):
            rejected.append(r.model_copy(update={
                "reject_reason": (
                    f"win_rate {r.win_rate:.1f}% < {min_win_rate * 100:g}% "
                    "(threshold_spec min_win_rate)"
                )
            }))
            continue
        survivors.append(r)

    survivors.sort(key=lambda r: r.irr_annualized, reverse=True)
    return survivors, rejected


def full_backtest(
    candidate: StrategyCandidate,
    lookback_months: int | None = None,
    lookback_days: int | None = None,
    lookback_hours: int | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    config: dict[str, Any] | None = None,
) -> CandidateBacktestResult:
    """Run a full backtest — same SDK call as quick, plus trade_history
    in raw_metrics for downstream display.

    Lookback resolution (first non-null wins):
      start_date+end_date > lookback_hours > lookback_days > lookback_months
      > timeframes.recommended_lookback_months(candidate.timeframe).

    `config` merges over the canonical `flattened_defaults()` from
    `data/trading_defaults.json`. Any key goes: the upstream execution
    knobs (initial_balance, max_risk_per_trade, atr_period, …), plus the
    three BacktestRequest-level optionals (slippage_pct, fee_pct,
    max_hold_time_hours), plus anything upstream adds later — the SDK
    model is `extra='allow'`, so unknown keys round-trip to the server
    without client-side error.
    """
    resolved_months, resolved_start, resolved_end = _resolve_window(
        candidate.timeframe,
        lookback_months,
        lookback_days=lookback_days,
        lookback_hours=lookback_hours,
        start_date=start_date,
        end_date=end_date,
    )

    client = mangrove_ai_client()
    try:
        raw, backtest_id = _run_tracked(
            client,
            _build_request(
                candidate,
                lookback_months=resolved_months,
                start_date=resolved_start,
                end_date=resolved_end,
                config=config,
            ),
        )
    except SdkError:
        raise
    except Exception as e:  # noqa: BLE001
        raise SdkError(
            f"Full backtest failed: {e}",
            suggestion="Check the strategy JSON is well-formed and the asset/interval are supported by mangroveai.",
        ) from e

    summary = _summarize(candidate, raw)
    summary.backtest_id = backtest_id
    # Attach trade history (if present) so the /strategies/autonomous response
    # can include it in full_backtest_metrics.
    trade_history = getattr(raw, "trade_history", None)
    if trade_history is not None:
        summary.raw_metrics = {**summary.raw_metrics, "trade_history": trade_history}
    # Record the resolved window so downstream callers can surface it to
    # the user (and detect fallbacks from the server).
    summary.raw_metrics = {
        **summary.raw_metrics,
        "resolved_window": {
            "lookback_months": resolved_months,
            "start_date": resolved_start,
            "end_date": resolved_end,
            "requested_timeframe": candidate.timeframe,
        },
    }

    _log.info(
        "backtest.full_completed",
        candidate_name=candidate.name,
        irr=summary.irr_annualized,
        win_rate=summary.win_rate,
        total_trades=summary.total_trades,
        resolved_months=resolved_months,
        resolved_start=resolved_start,
        resolved_end=resolved_end,
        backtest_id=backtest_id,
    )
    return summary


# ---------------------------------------------------------------------------
# Tracked submission — the run's server-side id survives
# ---------------------------------------------------------------------------

_POLL_INTERVAL_S = 2.0
_POLL_TIMEOUT_S = 600.0


def _run_tracked(client: Any, request: BacktestRequest) -> tuple[BacktestResult, str]:
    """Submit a backtest and poll it to completion, keeping its backtest_id.

    Same transport as the SDK's ``backtesting.run()`` (async submit to
    ``/api/v2/backtests/`` + status polling, no gateway ceiling), which
    discards the id. MangroveAI persists every run under the API key's
    user, so keeping the id is what lets a result be read back with
    ``get_backtest`` instead of re-run.
    """
    submission = client.backtesting.submit_async(request)
    backtest_id = str(submission.backtest_id)
    deadline = time.monotonic() + _POLL_TIMEOUT_S
    while True:
        status = client.backtesting.poll_status(backtest_id)
        if status.status == "completed":
            trades = status.trade_history
            return BacktestResult(
                success=True,
                metrics=status.metrics,
                trade_history=trades,
                execution_time_seconds=status.execution_time_seconds,
                trade_count=len(trades) if trades else 0,
            ), backtest_id
        if status.status == "failed":
            return BacktestResult(
                success=False,
                error=status.error_message,
                execution_time_seconds=status.execution_time_seconds,
            ), backtest_id
        if time.monotonic() > deadline:
            raise SdkError(
                f"Backtest {backtest_id} did not finish within {_POLL_TIMEOUT_S:.0f}s.",
                suggestion=f"It may still complete server-side; read it later with get_backtest('{backtest_id}').",
            )
        time.sleep(_POLL_INTERVAL_S)


# ---------------------------------------------------------------------------
# Stored runs — read back instead of re-running
# ---------------------------------------------------------------------------

#: The unit of each backtest metric. MangroveAI returns percent-typed metrics
#: on a 0-100 scale (0.52 means 0.52%, not 52%) — see threshold_spec.json's
#: metrics_mapping — and the scale cannot be recovered from the value itself.
METRIC_UNITS: dict[str, str] = {
    "starting_balance": "usd",
    "ending_balance": "usd",
    "total_return": "percent_0_100",
    "win_rate": "percent_0_100",
    "avg_daily_return": "percent_0_100",
    "irr_daily": "percent_0_100",
    "irr_annualized": "percent_0_100",
    "max_drawdown": "percent_0_100",
    "sharpe_ratio": "ratio_annualized",
    "sortino_ratio": "ratio_annualized",
    "calmar_ratio": "ratio_annualized",
    "gain_to_pain_ratio": "ratio",
    "num_days": "days",
    "max_drawdown_duration": "days",
    "total_trades": "count",
    "max_consecutive_wins": "count",
    "max_consecutive_losses": "count",
}
METRIC_UNITS_NOTE = (
    "Percent-typed metrics are on a 0-100 scale: 0.52 means 0.52%, not 52%. "
    "Never infer the scale from how big a number looks."
)

_LIST_FIELDS = (
    "id", "asset", "status", "result", "start_date", "end_date", "initial_balance",
    "total_return", "irr_annualized", "sharpe_ratio", "win_rate", "max_drawdown",
    "total_trades", "execution_time", "created_at", "archived",
)

_TIMEFRAME_ORDER = ("1m", "5m", "15m", "30m", "1h", "4h", "1d")


def _finest_timeframe(config: dict[str, Any]) -> str | None:
    """The candle size a stored run used: the finest timeframe its rules name."""
    found = {
        str(rule.get("timeframe"))
        for rule in (config.get("entry") or []) + (config.get("exit") or [])
        if isinstance(rule, dict) and rule.get("timeframe")
    }
    ranked = [tf for tf in _TIMEFRAME_ORDER if tf in found]
    return ranked[0] if ranked else (sorted(found)[0] if found else None)


def list_backtests(
    asset: str | None = None,
    status: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    limit: int = 20,
    offset: int = 0,
    include_archived: bool = False,
) -> dict[str, Any]:
    """The caller's stored backtest runs, newest first (``users.get_my_backtests``).

    Runs are keyed to the API key's user, not to this agent's local strategy
    ids — MangroveAI does not record a strategy id for agent-submitted runs —
    so filter by ``asset`` / dates and read ``get_backtest`` for the rules.
    """
    limit = max(1, min(int(limit), 100))
    offset = max(0, int(offset))
    try:
        page = mangrove_ai_client().users.get_my_backtests(
            status=status,
            asset=asset.strip().upper() if asset else None,
            date_from=date_from,
            date_to=date_to,
            limit=limit,
            offset=offset,
            include_archived=include_archived,
        )
    except Exception as e:  # noqa: BLE001
        raise SdkError(f"users.get_my_backtests failed: {e}") from e
    rows = [{field: getattr(item, field, None) for field in _LIST_FIELDS} for item in page.items]
    return {
        "total": page.total,
        "offset": offset,
        "limit": limit,
        "count": len(rows),
        "backtests": rows,
        "metric_units": {k: METRIC_UNITS[k] for k in ("total_return", "irr_annualized", "sharpe_ratio", "win_rate", "max_drawdown", "total_trades")},
        "metric_units_note": METRIC_UNITS_NOTE,
    }


def get_backtest(
    backtest_id: str, include_trades: bool = False, include_benchmark: bool = True,
) -> dict[str, Any]:
    """One stored run in full: status, window, rules, metrics and (optionally) trades.

    Reads ``GET /backtests/{id}`` through the SDK transport directly: in
    mangroveai <= 1.15 ``backtesting.get()`` types the response as
    ``BacktestResult`` (which requires ``success``), but the endpoint returns
    the stored run record, so the typed call raises on every successful 200.
    """
    if not backtest_id or not str(backtest_id).strip():
        raise BacktestNotFound("backtest_id is required.")
    client = mangrove_ai_client()
    try:
        raw = client.backtesting._core.request("GET", f"/backtests/{backtest_id}").json()
    except NotFoundError as e:
        raise BacktestNotFound(
            f"No backtest {backtest_id} is visible to this API key.",
            suggestion="List stored runs with list_backtests.",
        ) from e
    except Exception as e:  # noqa: BLE001
        raise SdkError(f"GET /backtests/{backtest_id} failed: {e}") from e

    config = raw.get("config") if isinstance(raw.get("config"), dict) else {}
    trades = raw.get("trade_history") or []
    out: dict[str, Any] = {
        "backtest_id": raw.get("id") or str(backtest_id),
        "status": raw.get("status"),
        "asset": raw.get("asset"),
        "strategy_name": config.get("name"),
        "interval": _finest_timeframe(config),
        "window": {"start": raw.get("start_date"), "end": raw.get("end_date")},
        "created_at": raw.get("created_at"),
        "completed_at": raw.get("completed_at"),
        "execution_time_seconds": raw.get("execution_time_seconds"),
        "initial_balance": raw.get("initial_balance"),
        "metrics": raw.get("metrics"),
        "metric_units": METRIC_UNITS,
        "metric_units_note": METRIC_UNITS_NOTE,
        "trade_count": len(trades),
        "config": config,
        "error": raw.get("error_message"),
    }
    if include_trades:
        out["trade_history"] = trades
    if include_benchmark and raw.get("status") == "completed" and raw.get("asset") \
            and raw.get("start_date") and raw.get("end_date"):
        from src.services.benchmark_service import benchmark_for_window

        benchmark = benchmark_for_window(
            raw["asset"], {"start_date": raw["start_date"], "end_date": raw["end_date"]},
        )
        total_return = (raw.get("metrics") or {}).get("total_return")
        if benchmark.get("available") and isinstance(total_return, (int, float)):
            benchmark["strategy_minus_benchmark_pct"] = round(
                float(total_return) - benchmark["buy_and_hold_return_pct"], 4,
            )
        out["benchmark"] = benchmark
    return out
