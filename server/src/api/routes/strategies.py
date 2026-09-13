"""Strategy routes — auth-gated. All thin wrappers over strategy_service."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from src.services import backtest_service, backtest_verdict, strategy_service
from src.services.strategy_service import (
    StrategyAutonomousRequest,
    StrategyDetailResponse,
    StrategyManualRequest,
    StrategyStatusUpdate,
)
from src.shared.auth.dependency import require_api_key

router = APIRouter(
    prefix="/strategies",
    dependencies=[Depends(require_api_key)],
    tags=["strategies"],
)


class AutonomousResponse(BaseModel):
    strategy: StrategyDetailResponse
    generation_report: dict[str, Any]


@router.post(
    "/autonomous",
    response_model=AutonomousResponse,
    status_code=201,
    summary="Autonomous strategy creation from a natural-language goal",
)
async def create_autonomous(req: StrategyAutonomousRequest) -> AutonomousResponse:
    detail, report = strategy_service.create_autonomous(req)
    return AutonomousResponse(strategy=detail, generation_report=report)


@router.post(
    "/manual",
    response_model=StrategyDetailResponse,
    status_code=201,
    summary="Manual strategy creation with explicit entry/exit rules",
)
async def create_manual(req: StrategyManualRequest) -> StrategyDetailResponse:
    return strategy_service.create_manual(req)


@router.get(
    "",
    response_model=list[StrategyDetailResponse],
    summary="List strategies",
)
async def list_strategies(
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[StrategyDetailResponse]:
    return strategy_service.list_strategies(status=status, limit=limit, offset=offset)


@router.get(
    "/{strategy_id}",
    response_model=StrategyDetailResponse,
    summary="Get a strategy by ID",
)
async def get_strategy(strategy_id: str) -> StrategyDetailResponse:
    return strategy_service.get_strategy(strategy_id)


@router.patch(
    "/{strategy_id}/status",
    response_model=StrategyDetailResponse,
    summary="Update strategy status (single source of truth for lifecycle)",
    description=(
        "Transitions: draft→inactive, inactive→{paper,live,archived}, "
        "paper→{live,inactive,archived}, live→{inactive,archived}. "
        "Target=live requires confirm=true AND an allocation block. "
        "Transition off live requires confirm=true."
    ),
)
async def update_status(strategy_id: str, req: StrategyStatusUpdate) -> StrategyDetailResponse:
    return strategy_service.update_status(strategy_id, req)


class BacktestInput(BaseModel):
    """Parameters for POST /strategies/{id}/backtest.

    Thin pass-through to MangroveAI's BacktestRequest. Only window-
    selection has special resolution on our side; all other tuning
    flows through a single `config` dict that merges over the canonical
    `trading_defaults.json`.

    Lookback window resolution — first non-null group wins:
      1. start_date + end_date (ISO 8601)
      2. lookback_hours
      3. lookback_days
      4. lookback_months
      5. timeframes.recommended_lookback_months(strategy.timeframe)
         (5m/15m/30m/1h → 3 mo, 4h → 6 mo, 1d → 12 mo)

    `config` accepts any BacktestRequest-compatible key:
      - slippage_pct, fee_pct, max_hold_time_hours
      - initial_balance, min_balance_threshold, min_trade_amount
      - max_open_positions, max_trades_per_day, max_risk_per_trade
      - max_units_per_trade, max_trade_amount
      - volatility_window, target_volatility, volatility_mode,
        enable_volatility_adjustment
      - cooldown_config (per-timeframe dict; the legacy cooldown_bars /
        daily_momentum_limit / weekly_momentum_limit are deprecated and
        ignored by the engine when cooldown_config is present)
      - reward_factor, atr_period, volatility_tolerance, ...

    Response carries `verdict` (mode=full, successful run): PASS / MARGINAL /
    FAIL / INSUFFICIENT_TRADES against threshold_spec.json, with per-threshold
    actual vs required — see services/backtest_verdict.py.
    Any key here overrides the corresponding trading_defaults.json entry.
    Unknown keys are forwarded as-is (SDK allows extras).
    """
    mode: str = "full"  # quick | full

    # Window selection. Leave all null to get timeframe-aware defaults.
    lookback_months: int | None = None
    lookback_days: int | None = None
    lookback_hours: int | None = None
    start_date: str | None = None
    end_date: str | None = None

    # Single escape hatch for tuning. Merges over trading_defaults.json.
    config: dict | None = None

    # Attach buy-and-hold over the same window as `benchmark` (one OHLCV call;
    # a benchmark that cannot be fetched never fails the backtest).
    include_benchmark: bool = True


@router.post(
    "/{strategy_id}/backtest",
    summary="Run a backtest on an existing strategy",
)
async def backtest(strategy_id: str, req: BacktestInput) -> dict:
    detail = strategy_service.get_strategy(strategy_id)
    # Build a StrategyCandidate shape from the stored strategy.
    from src.services.candidate_generator import StrategyCandidate
    candidate = StrategyCandidate(
        name=detail.name,
        asset=detail.asset,
        timeframe=detail.timeframe,
        entry=detail.entry,
        exit=detail.exit,
    )
    if req.mode == "quick":
        # Quick mode is summary-only; it uses the bulk path which only
        # understands lookback_months. If the caller passed finer
        # granularity, translate it best-effort (days → months, hours
        # too short to make sense in quick mode — fall through to
        # timeframe-aware default).
        months = req.lookback_months
        if months is None and req.lookback_days:
            months = max(1, req.lookback_days // 30)
        results = backtest_service.quick_backtest_all(
            [candidate], lookback_months=months,
        )
        result = results[0]
    else:
        result = backtest_service.full_backtest(
            candidate,
            lookback_months=req.lookback_months,
            lookback_days=req.lookback_days,
            lookback_hours=req.lookback_hours,
            start_date=req.start_date,
            end_date=req.end_date,
            config=req.config,
        )

    # Pass through the FULL metrics dict from the SDK, not a hand-picked
    # subset. Keeps this route aligned with what a direct curl or SDK
    # call would return and surfaces upstream metrics (sortino, calmar,
    # profit_factor, etc.) we can't anticipate here.
    full_metrics: dict = dict(result.raw_metrics) if result.raw_metrics else {}
    # Stable top-level aliases for existing callers + generation_report
    # schema. Overwrite whatever shape came from the server to keep
    # callers simple.
    full_metrics.update({
        "irr_annualized": result.irr_annualized,
        "win_rate": result.win_rate,
        "total_trades": result.total_trades,
        "sharpe_ratio": result.sharpe_ratio,
        "max_drawdown": result.max_drawdown,
        "net_pnl": result.net_pnl,
    })

    # Server-side verdict against threshold_spec.json (see
    # services/backtest_verdict.py for units + PASS/MARGINAL/FAIL rules).
    # Only for a successful FULL run: quick mode has no risk management
    # (no SL/TP/time exits), so its metrics are a screen, not performance.
    verdict: dict | None = None
    verdict_note: str | None = None
    if not result.success:
        verdict_note = "backtest did not succeed; no verdict issued"
    elif req.mode != "full":
        verdict_note = "quick mode is a signal-frequency screen without risk management; run mode=full for a verdict"
    else:
        verdict = backtest_verdict.compute_verdict(full_metrics)

    resolved_window = result.raw_metrics.get("resolved_window")
    response: dict[str, Any] = {
        "strategy_id": strategy_id,
        # Server-side run id (full mode) — read it back with get_backtest
        # instead of paying for the same measurement again.
        "backtest_id": result.backtest_id,
        "mode": req.mode,
        "verdict": verdict,
        "verdict_note": verdict_note,
        "metrics": full_metrics,
        "metric_units": backtest_service.METRIC_UNITS,
        "metric_units_note": backtest_service.METRIC_UNITS_NOTE,
        "trade_history": result.raw_metrics.get("trade_history") if req.mode == "full" else None,
        "resolved_window": resolved_window,
        "success": result.success,
        "error": result.error,
    }
    if req.include_benchmark and result.success:
        # A strategy's return always travels with what holding the asset did
        # over the same window.
        from src.services.benchmark_service import benchmark_for_window

        benchmark = benchmark_for_window(detail.asset, resolved_window)
        total_return = full_metrics.get("total_return")
        if benchmark.get("available") and isinstance(total_return, (int, float)):
            benchmark["strategy_minus_benchmark_pct"] = round(
                float(total_return) - benchmark["buy_and_hold_return_pct"], 4,
            )
        response["benchmark"] = benchmark
    return response


@router.post(
    "/{strategy_id}/evaluate",
    summary="Manually trigger a single evaluation tick",
    description="Same code path the cron job runs. Useful for debugging or power-user workflows.",
)
async def evaluate(strategy_id: str) -> dict:
    # Make sure the strategy exists; strategy_service.get_strategy raises
    # StrategyNotFound otherwise.
    strategy_service.get_strategy(strategy_id)
    strategy_service.tick(strategy_id)
    # Return the latest evaluation row for this strategy.
    from src.services.trade_log import list_evaluations
    evs = list_evaluations(strategy_id, limit=1)
    if not evs:
        return {"strategy_id": strategy_id, "status": "no_evaluation_recorded"}
    e = evs[0]
    return {
        "strategy_id": strategy_id,
        "evaluation_id": e.id,
        "status": e.status,
        "order_count": len(e.order_intents),
        "duration_ms": e.duration_ms,
        "error_msg": e.error_msg,
    }
