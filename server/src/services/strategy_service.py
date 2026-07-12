"""strategy_service — CRUD, lifecycle, cron tick.

Responsibilities:
- Autonomous creation (goal → candidates → backtest → filter → rank → full → persist)
- Manual creation (validate composition → persist)
- List + get + status transitions (single source of truth for lifecycle)
- Cron tick: call mangroveai.execution.evaluate(strategy_id), dispatch
  returned OrderIntents to order_executor.

Mangrove's SDK owns all evaluation logic — signal firing, risk gates,
position sizing, cooldowns, volatility adjustment. The agent just
orchestrates: fetch strategy from local cache → call SDK evaluate →
hand orders to executor → log.
"""
from __future__ import annotations

import json
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Literal

from mangrove_ai.models import CreateStrategyRequest
from pydantic import BaseModel, Field

from src.models.domain import Evaluation, OrderIntent
from src.services import (
    allocation_service,
    backtest_service,
    candidate_generator,
    order_executor,
    scheduler_service,
    trade_log,
)
from src.shared import timeframes
from src.shared.clients.mangrove import mangrove_ai_client
from src.shared.db.sqlite import get_connection
from src.shared.errors import (
    SdkError,
    StrategyInvalidComposition,
    StrategyInvalidStatusTransition,
    StrategyNotFound,
    StrategyNoViableCandidates,
)
from src.shared.logging import get_logger, with_correlation_id

_log = get_logger(__name__)


# Valid status transitions: {from: {allowed to}}
_TRANSITIONS: dict[str, set[str]] = {
    "draft": {"inactive", "archived"},
    "inactive": {"paper", "live", "archived"},
    "paper": {"live", "inactive", "archived"},
    "live": {"inactive", "archived"},
    "archived": set(),
}

_VALID_STATUSES = {"draft", "inactive", "paper", "live", "archived"}


def _validate_status_transition(current: str, target: str) -> None:
    """Raise StrategyInvalidStatusTransition if the transition is not in _TRANSITIONS."""
    if target not in _TRANSITIONS.get(current, set()):
        raise StrategyInvalidStatusTransition(
            f"Cannot transition from {current} to {target}.",
            suggestion=f"Valid transitions from {current}: {sorted(_TRANSITIONS.get(current, set()))}",
        )


def _apply_scheduler_effects(strategy_id: str, row: dict, target: str, current: str) -> None:
    """Register or cancel the cron job and release allocation based on target status."""
    if target in {"paper", "live"}:
        scheduler_service.register_job(
            strategy_id, row["timeframe"],
            "src.services.strategy_service:tick",
        )
    else:
        scheduler_service.cancel_job(strategy_id)
        if current == "live":
            allocation_service.release_allocation(strategy_id)


def _extract_order_intents(
    sdk_resp: Any, *, strategy_id: str, tick_id: str
) -> list[OrderIntent]:
    """Pull OrderIntents out of an SDK evaluate response.

    The shape varies by SDK / upstream version — be defensive. Current
    canonical key is `new_orders`; older variants used `order_intents` or
    `orders`. Missing this key is a silent data loss: every tick logs an
    evaluation but zero orders, which looks like "strategy didn't fire"
    when in fact it did.

    A malformed intent dict is skipped, but never silently: we emit a
    structured `strategy.tick.order_intent.skipped` warning (keys only,
    no values — see PR #87) so a dropped trade is diagnosable instead of
    looking identical to "strategy didn't fire". `strategy_id`/`tick_id`
    make the warning correlatable.
    """
    raw_orders = (
        getattr(sdk_resp, "new_orders", None)
        or getattr(sdk_resp, "order_intents", None)
        or getattr(sdk_resp, "orders", None)
        or (sdk_resp.model_dump().get("new_orders") if hasattr(sdk_resp, "model_dump") else None)
        or (sdk_resp.model_dump().get("order_intents") if hasattr(sdk_resp, "model_dump") else None)
        or []
    )
    order_intents: list[OrderIntent] = []
    for o in raw_orders:
        if isinstance(o, OrderIntent):
            order_intents.append(o)
        elif isinstance(o, dict):
            try:
                order_intents.append(OrderIntent.model_validate(_map_engine_order(o)))
            except Exception as e:  # noqa: BLE001
                _log.warning(
                    "strategy.tick.order_intent.skipped",
                    strategy_id=strategy_id,
                    tick_id=tick_id,
                    reason=str(e),
                    payload_keys=sorted(o.keys()),
                )
                continue
    return order_intents


def _map_engine_order(o: dict) -> dict:
    """Translate a MangroveAI engine order into OrderIntent field names.

    The engine's evaluate() emits `new_orders` as
    ``{order_id, asset: "ETH-USD", side: "enter_long"|"exit_long",
    order_type, status, price, position_size, position_id, ...}``
    (managers OrderResponse; `position_size` is in ASSET units), which
    shares no required field with OrderIntent — before this mapping,
    every real engine order failed validation and was skipped, so
    cron-driven strategies never traded (#139). Dicts already in
    OrderIntent shape pass through untouched.
    """
    side = o.get("side")
    if side not in ("enter_long", "exit_long") or "position_size" not in o:
        return o
    symbol = (o.get("asset") or "").split("-")[0] or (o.get("asset") or "")
    return {
        "action": "enter" if side == "enter_long" else "exit",
        "side": "buy" if side == "enter_long" else "sell",
        "symbol": symbol,
        "amount": float(o["position_size"]),
        "reason": f"engine {o.get('order_type', 'market')} order {o.get('order_id', '')}".strip(),
        "ref_price": o.get("price"),
        "stop_loss": o.get("stop_loss_price"),
        "take_profit": o.get("take_profit_price"),
    }


def _get_live_allocation(
    strategy_id: str,
) -> tuple[str | None, int | None, float | None, float | None]:
    """Return (wallet_address, chain_id, slippage_pct, amount) from the
    active allocation, or Nones. `amount` caps the input-token spend of any
    single live swap (#139: engine intents are sized off the engine's own
    simulated account, not the user's allocation)."""
    active_alloc = allocation_service.get_active_allocation(strategy_id)
    if not active_alloc:
        return None, None, None, None
    wallet_address = active_alloc.wallet_address
    slippage_pct = active_alloc.slippage_pct
    wrow = get_connection().execute(
        "SELECT chain_id FROM wallets WHERE address = ?",
        (wallet_address,),
    ).fetchone()
    chain_id = wrow["chain_id"] if wrow else None
    return wallet_address, chain_id, slippage_pct, active_alloc.amount


# -- Pydantic request/response models ---------------------------------------


class StrategyAutonomousRequest(BaseModel):
    goal: str
    asset: str
    timeframe: str
    candidate_count: int = Field(7, ge=5, le=10)
    # None = auto-pick from timeframes.recommended_lookback_months(timeframe):
    #   5m/15m/30m/1h → 3 months; 4h → 6 months; 1d → 12 months.
    # Explicit value overrides the recommendation.
    backtest_lookback_months: int | None = None
    seed: int | None = None  # reproducibility


class StrategyManualRequest(BaseModel):
    name: str
    asset: str
    timeframe: str
    entry: list[dict[str, Any]]
    exit: list[dict[str, Any]] = Field(default_factory=list)
    execution_config: dict[str, Any] | None = None


class StrategyAllocationInput(BaseModel):
    wallet_address: str
    token: str  # symbol like "USDC"
    token_address: str
    amount: float
    # Per-allocation slippage tolerance as DECIMAL (0.005 = 0.5%).
    # REQUIRED — picking a tolerance is a risk decision the user must
    # make explicitly when committing funds to a live strategy.
    # Capped at 0.0025 (0.25%); anything higher is rejected at the
    # API boundary. Matches the decimal convention used for direct
    # swap slippage_pct (server/src/api/routes/dex.py SwapRequest).
    slippage_pct: float = Field(..., gt=0, le=0.0025, description=(
        "Slippage tolerance as DECIMAL (0.005 = 0.5%). Max 0.0025 (0.25%). "
        "No default — must be set at allocation time."
    ))


class StrategyStatusUpdate(BaseModel):
    status: Literal["draft", "inactive", "paper", "live", "archived"]
    confirm: bool = False
    allocation: StrategyAllocationInput | None = None


class StrategyDetailResponse(BaseModel):
    id: str
    mangrove_id: str
    name: str
    asset: str
    timeframe: str
    status: str
    entry: list[dict[str, Any]]
    exit: list[dict[str, Any]]
    execution_config: dict[str, Any]
    generation_report: dict[str, Any] | None = None
    created_at: datetime
    updated_at: datetime


# -- Composition validation --------------------------------------------------


def _validate_composition(entry: list[dict], exit_rules: list[dict]) -> None:
    """Entry must be exactly 1 TRIGGER + 0+ FILTERs; exit 0-1 TRIGGERs + 0+ FILTERs."""
    entry_triggers = [r for r in entry if (r.get("signal_type") or "").upper() == "TRIGGER"]
    if len(entry_triggers) != 1:
        raise StrategyInvalidComposition(
            f"Entry must have exactly 1 TRIGGER; got {len(entry_triggers)}.",
            suggestion="Compose entry as: [one TRIGGER, zero or more FILTERs].",
        )
    exit_triggers = [r for r in exit_rules if (r.get("signal_type") or "").upper() == "TRIGGER"]
    if len(exit_triggers) > 1:
        raise StrategyInvalidComposition(
            f"Exit may have at most 1 TRIGGER; got {len(exit_triggers)}.",
            suggestion="Compose exit as: [zero or one TRIGGER, zero or more FILTERs].",
        )


# -- Local cache helpers -----------------------------------------------------


def _insert_cache(
    mangrove_detail: Any,
    entry: list[dict],
    exit_rules: list[dict],
    execution_config: dict[str, Any],
    generation_report: dict[str, Any] | None,
) -> str:
    """Insert a row into local strategies cache. Returns our local UUID."""
    local_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    mangrove_id = str(getattr(mangrove_detail, "id", None) or getattr(mangrove_detail, "strategy_id", local_id))
    get_connection().execute(
        """INSERT INTO strategies
           (id, mangrove_id, name, asset, timeframe, status,
            entry_json, exit_json, execution_config_json,
            generation_report_json, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            local_id, mangrove_id,
            getattr(mangrove_detail, "name", "unnamed"),
            getattr(mangrove_detail, "asset", ""),
            _extract_timeframe(entry),
            getattr(mangrove_detail, "status", "draft"),
            json.dumps(entry),
            json.dumps(exit_rules),
            json.dumps(execution_config or {}),
            json.dumps(generation_report) if generation_report else None,
            now, now,
        ),
    )
    get_connection().commit()
    return local_id


def _extract_timeframe(entry: list[dict]) -> str:
    if not entry:
        return "1h"
    return entry[0].get("timeframe") or "1h"


def _row_to_response(row: Any) -> StrategyDetailResponse:
    return StrategyDetailResponse(
        id=row["id"],
        mangrove_id=row["mangrove_id"],
        name=row["name"],
        asset=row["asset"],
        timeframe=row["timeframe"],
        status=row["status"],
        entry=json.loads(row["entry_json"] or "[]"),
        exit=json.loads(row["exit_json"] or "[]"),
        execution_config=json.loads(row["execution_config_json"] or "{}"),
        generation_report=json.loads(row["generation_report_json"]) if row["generation_report_json"] else None,
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
    )


def _get_row(strategy_id: str) -> Any:
    row = get_connection().execute(
        "SELECT * FROM strategies WHERE id = ?", (strategy_id,),
    ).fetchone()
    if not row:
        raise StrategyNotFound(
            f"Strategy {strategy_id} not found.",
            suggestion="Use GET /api/v1/agent/strategies to list available strategies.",
        )
    return row


def _set_status(strategy_id: str, new_status: str) -> None:
    now = datetime.now(timezone.utc).isoformat()
    get_connection().execute(
        "UPDATE strategies SET status = ?, updated_at = ? WHERE id = ?",
        (new_status, now, strategy_id),
    )
    get_connection().commit()


# -- Public API --------------------------------------------------------------


def create_autonomous(req: StrategyAutonomousRequest) -> tuple[StrategyDetailResponse, dict[str, Any]]:
    """Generate candidates, backtest, filter, rank, create winner.

    Returns (strategy_detail_response, generation_report). The report is
    also persisted in the local strategies cache for audit.
    """
    # Reject unsupported timeframes (e.g. 1m) up front — the server would
    # otherwise silently fall back to 1h and produce misleading results.
    timeframe = timeframes.canonicalize_timeframe(req.timeframe)

    candidates = candidate_generator.generate(
        goal=req.goal, asset=req.asset, timeframe=timeframe,
        n=req.candidate_count, seed=req.seed,
    )

    # Use timeframe-aware recommended lookback if caller didn't specify one.
    lookback_months = (
        req.backtest_lookback_months
        if req.backtest_lookback_months is not None
        else timeframes.recommended_lookback_months(timeframe)
    )

    results = backtest_service.quick_backtest_all(
        candidates, lookback_months=lookback_months,
    )
    survivors, rejected = backtest_service.filter_and_rank(results)

    if not survivors:
        raise StrategyNoViableCandidates(
            f"No candidate passed filters (n_tried={len(candidates)}, n_rejected={len(rejected)}).",
            suggestion="Try a different goal, longer timeframe, or longer backtest lookback.",
        )

    winner = survivors[0]
    full = backtest_service.full_backtest(
        winner.candidate, lookback_months=lookback_months,
    )

    # Build the SDK request from the winning candidate.
    try:
        detail = mangrove_ai_client().strategies.create(
            CreateStrategyRequest(
                name=winner.candidate.name,
                asset=winner.candidate.asset,
                entry=winner.candidate.entry,
                exit=winner.candidate.exit,
                status="inactive",
            ),
        )
    except Exception as e:  # noqa: BLE001
        raise SdkError(f"strategies.create failed: {e}") from e

    generation_report = {
        "candidates_tried": len(candidates),
        "candidates_passed_filter": len(survivors),
        "winner_rank": 1,
        "full_backtest_metrics": {
            "irr_annualized": full.irr_annualized,
            "win_rate": full.win_rate,
            "total_trades": full.total_trades,
            "sharpe_ratio": full.sharpe_ratio,
            "max_drawdown": full.max_drawdown,
            "net_pnl": full.net_pnl,
        },
        "rejected_reasons": [
            {"candidate": r.candidate.name, "reason": r.reject_reason}
            for r in rejected
        ],
    }

    local_id = _insert_cache(
        detail,
        entry=winner.candidate.entry,
        exit_rules=winner.candidate.exit,
        execution_config=backtest_service.flattened_defaults(),
        generation_report=generation_report,
    )
    row = _get_row(local_id)
    resp = _row_to_response(row)
    _log.info("strategy.created",
              strategy_id=local_id, mangrove_id=resp.mangrove_id, mode="autonomous",
              asset=req.asset, timeframe=req.timeframe,
              winner_irr=full.irr_annualized, winner_trades=full.total_trades)
    return resp, generation_report


def create_manual(req: StrategyManualRequest) -> StrategyDetailResponse:
    """Manual creation — caller supplies explicit entry/exit rules."""
    # Reject unsupported timeframes up front. For manual strategies the
    # timeframe is embedded in each entry/exit rule; we also accept a
    # top-level req.timeframe which takes precedence when set.
    if getattr(req, "timeframe", None):
        timeframes.canonicalize_timeframe(req.timeframe)
    else:
        # Validate the per-rule timeframe — matches what _extract_timeframe
        # would pull when there's no top-level field.
        inferred = req.entry[0].get("timeframe") if req.entry else None
        if inferred:
            timeframes.canonicalize_timeframe(inferred)

    _validate_composition(req.entry, req.exit)

    try:
        detail = mangrove_ai_client().strategies.create(
            CreateStrategyRequest(
                name=req.name, asset=req.asset,
                entry=req.entry, exit=req.exit,
                status="inactive",
                execution_config=req.execution_config,
            ),
        )
    except Exception as e:  # noqa: BLE001
        raise SdkError(f"strategies.create failed: {e}") from e

    local_id = _insert_cache(
        detail,
        entry=req.entry,
        exit_rules=req.exit,
        execution_config=req.execution_config or backtest_service.flattened_defaults(),
        generation_report=None,
    )
    row = _get_row(local_id)
    resp = _row_to_response(row)
    _log.info("strategy.created", strategy_id=local_id, mangrove_id=resp.mangrove_id, mode="manual")
    return resp


def list_strategies(
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[StrategyDetailResponse]:
    """List strategies from local cache."""
    sql = "SELECT * FROM strategies"
    params: list = []
    if status:
        sql += " WHERE status = ?"
        params.append(status)
    sql += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])
    rows = get_connection().execute(sql, params).fetchall()
    return [_row_to_response(r) for r in rows]


def get_strategy(strategy_id: str) -> StrategyDetailResponse:
    return _row_to_response(_get_row(strategy_id))


def update_status(strategy_id: str, update: StrategyStatusUpdate) -> StrategyDetailResponse:
    """Single source of truth for strategy lifecycle transitions."""
    row = _get_row(strategy_id)
    current = row["status"]
    target = update.status

    if target not in _VALID_STATUSES:
        raise StrategyInvalidStatusTransition(f"Unknown status '{target}'.")
    if target == current:
        return _row_to_response(row)  # no-op
    _validate_status_transition(current, target)

    # Gate transitions that move real money or stop live execution.
    needs_confirm = (
        target == "live"
        or (current == "live" and target in {"inactive", "archived"})
    )
    if needs_confirm and not update.confirm:
        from src.shared.errors import ConfirmationRequired
        raise ConfirmationRequired(
            f"Transition {current} → {target} requires confirm=true.",
            suggestion="This transition affects real funds or stops live execution. Re-submit with confirm=true.",
        )

    if target == "live":
        if not update.allocation:
            raise StrategyInvalidStatusTransition(
                "Transition to live requires an allocation block.",
                suggestion="Include allocation: {wallet_address, token, token_address, amount} in the request.",
            )
        # Backup gate: the wallet that will be funding live trades must
        # have been confirmed-backed-up by the user. Paper→live is the
        # last chance to catch "user deposited money but never saved
        # their recovery secret" before real funds move.
        from src.services.wallet_manager import require_backup_confirmed
        require_backup_confirmed(update.allocation.wallet_address)
        allocation_service.record_allocation(
            strategy_id=strategy_id,
            wallet_address=update.allocation.wallet_address,
            token_address=update.allocation.token_address,
            token_symbol=update.allocation.token,
            amount=update.allocation.amount,
            slippage_pct=update.allocation.slippage_pct,
        )

    # Sync status upstream.
    try:
        mangrove_ai_client().strategies.update_status(row["mangrove_id"], target)
    except Exception as e:  # noqa: BLE001
        # If we already recorded an allocation, roll it back.
        if target == "live":
            allocation_service.release_allocation(strategy_id)
        raise SdkError(f"strategies.update_status failed: {e}") from e

    _apply_scheduler_effects(strategy_id, row, target, current)
    _set_status(strategy_id, target)
    new_row = _get_row(strategy_id)
    _log.info("strategy.status_changed",
              strategy_id=strategy_id, from_status=current, to_status=target,
              allocation=bool(update.allocation))
    return _row_to_response(new_row)


# -- Cron tick ---------------------------------------------------------------


def tick(strategy_id: str) -> None:
    """The scheduler callback. Runs in a threadpool — must never propagate
    exceptions, and must never block the request path.

    Semantics (from the architecture doc):
    1. Load strategy from local cache.
    2. Call mangroveai.execution.evaluate(mangrove_id,
       persist=(mode=='live')). SDK fetches market data, applies all
       risk gates, returns OrderIntent[].
    3. If empty: log evaluation status=ok, no trades.
    4. If present: dispatch to order_executor.execute_many(...).
    5. On any exception: log evaluation status=error with error_msg.
       Never let the exception escape.
    """
    tick_id = str(uuid.uuid4())
    start_ns = time.monotonic()
    with with_correlation_id(tick_id):
        try:
            row = _get_row(strategy_id)
        except StrategyNotFound:
            _log.error("strategy.tick.errored",
                       strategy_id=strategy_id, tick_id=tick_id,
                       exception="StrategyNotFound")
            return
        mode = row["status"]
        _log.info("strategy.tick.started",
                  strategy_id=strategy_id, tick_id=tick_id,
                  timeframe=row["timeframe"], mode=mode)

        # Only run the tick for paper or live strategies (defensive).
        if mode not in {"paper", "live"}:
            _log.info("strategy.tick.completed",
                      strategy_id=strategy_id, tick_id=tick_id,
                      order_count=0, duration_ms=0, reason="not active")
            return

        try:
            persist = mode == "live"
            sdk_resp = mangrove_ai_client().execution.evaluate(
                row["mangrove_id"], persist=persist,
            )
        except Exception as e:  # noqa: BLE001
            duration_ms = int((time.monotonic() - start_ns) * 1000)
            trade_log.log_evaluation(Evaluation(
                id=str(uuid.uuid4()),
                strategy_id=strategy_id,
                timestamp=trade_log.now_utc(),
                duration_ms=duration_ms,
                status="error",
                error_msg=f"SDK evaluate failed: {e}",
            ))
            _log.error("strategy.tick.errored",
                       strategy_id=strategy_id, tick_id=tick_id,
                       exception=str(e), duration_ms=duration_ms)
            return

        # Extract OrderIntents from the SDK response (defensive about
        # response shape; logs — never silently drops — malformed intents).
        order_intents = _extract_order_intents(
            sdk_resp, strategy_id=strategy_id, tick_id=tick_id
        )

        # Find the wallet + chain_id for live execution (allocation provides it).
        wallet_address = None
        chain_id = None
        slippage_pct = None
        allocation_amount = None
        if mode == "live":
            wallet_address, chain_id, slippage_pct, allocation_amount = (
                _get_live_allocation(strategy_id)
            )

        # Log the evaluation FIRST so trades can FK-reference it. The
        # evaluation describes the SDK call outcome, not the downstream
        # execution outcomes; those get their own rows in `trades`.
        evaluation_id = str(uuid.uuid4())
        duration_ms = int((time.monotonic() - start_ns) * 1000)
        sdk_dump = sdk_resp.model_dump() if hasattr(sdk_resp, "model_dump") else {}
        trade_log.log_evaluation(Evaluation(
            id=evaluation_id,
            strategy_id=strategy_id,
            timestamp=trade_log.now_utc(),
            sdk_response=sdk_dump,
            order_intents=order_intents,
            duration_ms=duration_ms,
            status="ok",
        ))

        if order_intents:
            order_executor.execute_many(
                order_intents, mode=mode, strategy_id=strategy_id,
                evaluation_id=evaluation_id,
                wallet_address=wallet_address, chain_id=chain_id,
                slippage_pct=slippage_pct,
                max_input_amount=allocation_amount,
            )

        _log.info("strategy.tick.completed",
                  strategy_id=strategy_id, tick_id=tick_id,
                  order_count=len(order_intents), duration_ms=duration_ms)
