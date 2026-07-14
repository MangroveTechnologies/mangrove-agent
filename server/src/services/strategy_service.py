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

from src.config import app_config
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
            mapped = _map_engine_order(o)
            if mapped is None:
                # Resting engine order (pending bracket etc.) — normal, not
                # an error; the engine re-emits it as `filled` when it
                # actually triggers (#149).
                _log.info(
                    "strategy.tick.order_intent.resting",
                    strategy_id=strategy_id,
                    tick_id=tick_id,
                    order_type=o.get("order_type"),
                    order_status=o.get("status"),
                )
                continue
            try:
                order_intents.append(OrderIntent.model_validate(mapped))
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


def _map_engine_order(o: dict) -> dict | None:
    """Translate a MangroveAI engine order into OrderIntent field names.

    The engine's evaluate() emits `new_orders` as
    ``{order_id, asset: "ETH-USD", side: "enter_long"|"exit_long",
    order_type, status, price, position_size, position_id, ...}``
    (managers OrderResponse; `position_size` is in ASSET units), which
    shares no required field with OrderIntent — before this mapping,
    every real engine order failed validation and was skipped, so
    cron-driven strategies never traded (#139). Dicts already in
    OrderIntent shape pass through untouched.

    Returns None for RESTING engine orders — anything not `status ==
    "filled"`. A tick that opens a position emits the entry as `filled`
    PLUS its bracket exits (stop_loss / take_profit) as `pending`;
    executing those brackets immediately would exit the position the
    moment it entered (#149, observed live 2026-07-12). The engine
    re-emits a bracket as `filled` on the tick where it actually
    triggers.
    """
    side = o.get("side")
    if side not in ("enter_long", "exit_long") or "position_size" not in o:
        return o
    if o.get("status") != "filled":
        return None
    symbol = (o.get("asset") or "").split("-")[0] or (o.get("asset") or "")
    return {
        "action": "enter" if side == "enter_long" else "exit",
        "side": "buy" if side == "enter_long" else "sell",
        "symbol": symbol,
        "amount": float(o["position_size"]),
        "reason": f"engine {o.get('order_type', 'market')} order {o.get('order_id', '')}".strip(),
        "ref_price": o.get("price"),
        "engine_position_id": o.get("position_id"),
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
    # Which MangroveAI evaluation lane this strategy's ticks use (#151):
    #   'server'    — by-id evaluation; the engine's DB is authoritative for
    #                 engine position state (default).
    #   'stateless' — object lane; THIS agent supplies and receives all
    #                 state (execution_state + open_positions) from its own
    #                 SQLite. Requires MangroveAI with #840/#848.
    # None falls back to the config default (EVALUATION_LANE, 'server').
    evaluation_lane: Literal["server", "stateless"] | None = None


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
    evaluation_lane: str | None = None,
) -> str:
    """Insert a row into local strategies cache. Returns our local UUID."""
    local_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    mangrove_id = str(getattr(mangrove_detail, "id", None) or getattr(mangrove_detail, "strategy_id", local_id))
    get_connection().execute(
        """INSERT INTO strategies
           (id, mangrove_id, name, asset, timeframe, status,
            entry_json, exit_json, execution_config_json,
            generation_report_json, evaluation_lane, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
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
            evaluation_lane,
            now, now,
        ),
    )
    get_connection().commit()
    return local_id


def _extract_timeframe(entry: list[dict]) -> str:
    if not entry:
        return "1h"
    return entry[0].get("timeframe") or "1h"


# Fresh-strategy engine account defaults for the stateless lane, matching
# the engine's own sim-account bootstrap (cash 10k; see MangroveAI
# execution_state contract).
_FRESH_EXECUTION_STATE = {
    "cash_balance": 10000.0,
    "account_value": 10000.0,
    "total_trades": 0,
    "num_open_positions": 0,
}


def _resolve_evaluation_lane(row: Any) -> str:
    """Per-strategy lane, falling back to the config default (#151).

    'server' (default): by-id evaluation, engine DB authoritative.
    'stateless': object lane, this agent owns all state (MangroveAI#840).
    """
    lane = None
    if hasattr(row, "get"):
        lane = row.get("evaluation_lane")
    else:
        try:
            lane = row["evaluation_lane"]
        except (KeyError, IndexError, TypeError):
            lane = None
    return lane or getattr(app_config, "EVALUATION_LANE", None) or "server"


def _stateless_strategy_dict(row: Any) -> dict[str, Any]:
    """Build the object-lane strategy payload from the LOCAL row.

    Everything the engine requires lives locally: rules and the full
    execution_config synced at create time, and the execution_state we
    persist after every tick (migration 005). A never-ticked strategy
    starts from the fresh sim-account defaults.
    """
    execution_config = json.loads(row["execution_config_json"] or "{}")
    execution_state = None
    try:
        execution_state = json.loads(row["execution_state_json"] or "null")
    except (KeyError, IndexError) as exc:
        # Missing execution_state_json in row shape (e.g., older/mismatched row);
        # keep execution_state=None so fresh defaults are used below.
        _log.debug("execution_state_json missing from strategy row; using fresh execution state defaults", exc_info=exc)
    return {
        "name": row["name"],
        "asset": row["asset"],
        "entry": json.loads(row["entry_json"] or "[]"),
        "exit": json.loads(row["exit_json"] or "[]"),
        "execution_config": execution_config,
        "execution_state": execution_state or dict(_FRESH_EXECUTION_STATE),
        "position_size_calc": execution_config.get("position_size_calc", "v2"),
    }


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
        evaluation_lane=req.evaluation_lane,
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

    # Portfolio kill switch (#146): the live-book composition just changed
    # (a strategy entered or left 'live'), so committed capital / realized P&L
    # in the aggregate shifted for a deliberate reason -- re-baseline the
    # high-water mark so it is not mistaken for drawdown. No-op while latched.
    if "live" in (target, current):
        from src.services import portfolio_risk_service
        portfolio_risk_service.rebaseline(reason=f"status_{current}_to_{target}")

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

        lane = _resolve_evaluation_lane(row)
        try:
            if lane == "stateless":
                # Object lane (#151/MangroveAI#840): THIS agent supplies and
                # receives all state. execution_state + open_positions come
                # from our SQLite (persisted below after every tick) and the
                # engine writes nothing server-side. Triggered exits arrive
                # in new_orders as status='filled', same as the by-id lane.
                sdk_resp = mangrove_ai_client().execution.evaluate_by_object(
                    _stateless_strategy_dict(row),
                    persist=False,
                    open_positions=json.loads(row["open_positions_json"] or "[]"),
                )
            else:
                # Server lane (default). persist=True for BOTH paper and
                # live (#149/#150): the engine only saves the position (and
                # therefore only re-evaluates its stop_loss/take_profit/time
                # exits on later ticks) when persist is set —
                # get_open_positions loads from its DB. Paper-vs-live parity
                # requires the same engine bookkeeping; paper stays
                # simulation-only on OUR side (no wallet, no broadcast).
                sdk_resp = mangrove_ai_client().execution.evaluate(
                    row["mangrove_id"], persist=True,
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

        # Persist the engine's execution_state first-class (#151) — the
        # agent's own record of account/risk state per strategy, and the
        # exact value the stateless evaluation lane (MangroveAI#840) will
        # round-trip. On the stateless lane, also persist the returned
        # open_positions blob verbatim — it is echoed on the next tick
        # (positions table stays the human audit trail; this is protocol
        # state). Never fail the tick over bookkeeping.
        exec_state = sdk_dump.get("execution_state")
        open_positions_blob = sdk_dump.get("open_positions")
        if isinstance(exec_state, dict) and exec_state:
            try:
                conn = get_connection()
                if open_positions_blob is not None:
                    conn.execute(
                        """UPDATE strategies SET execution_state_json = ?,
                           open_positions_json = ?, updated_at = ? WHERE id = ?""",
                        (json.dumps(exec_state, default=str),
                         json.dumps(open_positions_blob, default=str),
                         trade_log.now_utc().isoformat(), strategy_id),
                    )
                else:
                    conn.execute(
                        "UPDATE strategies SET execution_state_json = ?, updated_at = ? WHERE id = ?",
                        (json.dumps(exec_state, default=str),
                         trade_log.now_utc().isoformat(), strategy_id),
                    )
                conn.commit()
            except Exception as e:  # noqa: BLE001
                _log.error("strategy.execution_state.persist_failed",
                           strategy_id=strategy_id, exception=str(e))

        # Portfolio kill switch (#146): on every LIVE tick, update the live-book
        # high-water mark and trip if aggregate drawdown crosses the limit. A
        # trip pauses ALL live strategies (latched, human-reset only) -- so if
        # not allowed we must NOT place new orders this tick. Runs regardless of
        # whether THIS strategy produced intents, since a trip can be driven by
        # realized losses on OTHER live strategies.
        if mode == "live":
            from src.services import portfolio_risk_service
            pr = portfolio_risk_service.check_before_live_execution()
            if not pr["allowed"]:
                _log.warning("strategy.tick.portfolio_halt",
                             strategy_id=strategy_id, tick_id=tick_id,
                             drawdown=pr.get("drawdown"),
                             book_value=pr.get("book_value"),
                             high_water_mark=pr.get("high_water_mark"),
                             reason=pr.get("reason"))
                _log.info("strategy.tick.completed",
                          strategy_id=strategy_id, tick_id=tick_id,
                          order_count=0, duration_ms=duration_ms,
                          reason="portfolio_halt")
                return

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
