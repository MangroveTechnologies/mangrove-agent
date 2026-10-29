"""Local execution records, lifecycle and cron ticks.

Responsibilities:
- Strategy authoring is owned by MangroveAI and accessed over MCP.
- Internal execution-record reads and local status transitions
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

from pydantic import BaseModel, ConfigDict, Field

from src.config import app_config
from src.models.domain import Evaluation, OrderIntent
from src.services import (
    allocation_service,
    order_executor,
    scheduler_service,
    trade_log,
)
from src.shared.clients.mangrove import mangrove_ai_client
from src.shared.db.sqlite import get_connection
from src.shared.errors import (
    SdkError,
    StrategyInvalidStatusTransition,
    StrategyNotFound,
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
            strategy_id,
            row["timeframe"],
            "src.services.strategy_service:tick",
        )
    else:
        from src.services import confirmed_execution

        paper_monitor = row["exit_only_mode"] == "paper" or (current == "paper" and _resolve_evaluation_lane(row) == "stateless")
        if paper_monitor:
            from src.services import paper_confirmed

        exposure = paper_confirmed.has_exposure(strategy_id) if paper_monitor else confirmed_execution.has_exposure(strategy_id)
        if (current == "live" or row["exit_only_mode"] == "live" or paper_monitor) and exposure:
            conn = get_connection()
            conn.execute("UPDATE strategies SET exit_only_mode=? WHERE id=?", ("paper" if paper_monitor else "live", strategy_id))
            conn.commit()
            scheduler_service.register_job(strategy_id, row["timeframe"], "src.services.strategy_service:tick")
        else:
            scheduler_service.cancel_job(strategy_id)
            if current == "live" or row["exit_only_mode"] == "live":
                allocation_service.release_allocation(strategy_id)


def _extract_order_intents(sdk_resp: Any, *, strategy_id: str, tick_id: str) -> list[OrderIntent]:
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
        "engine_order_id": o.get("order_id"),
        "stop_loss": o.get("stop_loss_price"),
        "take_profit": o.get("take_profit_price"),
    }


def _market_snapshot(sdk_dump: dict[str, Any], *, asset: str) -> dict[str, Any]:
    """The market context an evaluation saw, lifted from the engine response.

    The engine fetches market data itself and reports the price it evaluated
    at (`current_price`) plus the evaluation `timestamp`. Before this, the
    evaluations table stored `market_snapshot: {}` even though both were in
    `sdk_response`. Returns {} when the response carries neither (e.g. a
    malformed/empty response) — never invents a price.
    """
    snapshot: dict[str, Any] = {}
    price = sdk_dump.get("current_price")
    if price is not None:
        snapshot["price"] = price
    if sdk_dump.get("timestamp"):
        snapshot["timestamp"] = sdk_dump["timestamp"]
    if snapshot:
        snapshot["asset"] = sdk_dump.get("asset") or asset
        snapshot["source"] = "mangroveai.execution.evaluate"
    return snapshot


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
    wrow = (
        get_connection()
        .execute(
            "SELECT chain_id FROM wallets WHERE address = ?",
            (wallet_address,),
        )
        .fetchone()
    )
    chain_id = wrow["chain_id"] if wrow else None
    return wallet_address, chain_id, slippage_pct, active_alloc.amount


# -- Pydantic request/response models ---------------------------------------


class StrategyMarketContext(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    base_token: str = Field(min_length=1, pattern=r"^[A-Z0-9._-]+$")
    quote_token: str = Field(min_length=1, pattern=r"^[A-Z0-9._-]+$")
    market_data_venue: str = Field(min_length=1, pattern=r"^[A-Z0-9._-]+$")


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
    slippage_pct: float = Field(
        ...,
        gt=0,
        le=0.0025,
        description=(
            "Slippage tolerance as DECIMAL (0.005 = 0.5%). Max 0.0025 (0.25%). "
            "No default — must be set at allocation time."
        ),
    )


class StrategyStatusUpdate(BaseModel):
    status: Literal["draft", "inactive", "paper", "live", "archived"]
    confirm: bool = False
    allocation: StrategyAllocationInput | None = None


class ExecutionVenueResponse(BaseModel):
    """Public broker mandate; never includes raw account IDs or credentials."""

    venue: str
    asset_class: str
    capital: str
    max_order_usd: str
    slippage_pct: str


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
    market_context: StrategyMarketContext | None = None
    execution_venue: ExecutionVenueResponse | None = None
    generation_report: dict[str, Any] | None = None
    created_at: datetime
    updated_at: datetime


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
        **_market_context(row),
    }


def _market_context(row: Any) -> dict[str, str]:
    raw = row["market_context_json"] if "market_context_json" in row.keys() else None
    return StrategyMarketContext.model_validate_json(raw).model_dump() if raw else {}


def configure_market(strategy_id: str, market: StrategyMarketContext) -> StrategyDetailResponse:
    """Set the market for a fresh inactive, caller-owned strategy. No evaluation or activation."""
    from src.services.broker_execution import _lock

    with _lock:
        row = _get_row(strategy_id)
        if row["status"] not in {"draft", "inactive"} or _resolve_evaluation_lane(row) != "stateless":
            raise StrategyInvalidStatusTransition("Market configuration requires an inactive stateless strategy.")
        if market.base_token != row["asset"]:
            raise SdkError("Market base token must match the strategy asset.")
        conn = get_connection()
        for table in ("trades", "broker_orders", "positions"):
            if conn.execute(f"SELECT 1 FROM {table} WHERE strategy_id=? LIMIT 1", (strategy_id,)).fetchone():
                raise SdkError("A strategy with execution history cannot change markets; create a new strategy.")
        conn.execute(
            "UPDATE strategies SET market_context_json=?, updated_at=? WHERE id=?",
            (market.model_dump_json(), datetime.now(timezone.utc).isoformat(), strategy_id),
        )
        conn.commit()
        return get_strategy(strategy_id)


def _row_to_response(row: Any) -> StrategyDetailResponse:
    route = (
        get_connection()
        .execute(
            "SELECT venue,asset_class,capital,max_order_usd,slippage_pct FROM broker_routes WHERE strategy_id=?",
            (row["id"],),
        )
        .fetchone()
    )
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
        market_context=_market_context(row) or None,
        execution_venue=ExecutionVenueResponse(**dict(route)) if route else None,
        generation_report=json.loads(row["generation_report_json"]) if row["generation_report_json"] else None,
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
    )


def _get_row(strategy_id: str) -> Any:
    row = (
        get_connection()
        .execute(
            "SELECT * FROM strategies WHERE id = ?",
            (strategy_id,),
        )
        .fetchone()
    )
    if not row:
        raise StrategyNotFound(
            f"Strategy {strategy_id} not found.",
            suggestion="Use the local execution record ID, not a MangroveAI saved-strategy ID.",
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


def get_strategy(strategy_id: str) -> StrategyDetailResponse:
    return _row_to_response(_get_row(strategy_id))


def update_status(strategy_id: str, update: StrategyStatusUpdate) -> StrategyDetailResponse:
    """Single source of truth for strategy lifecycle transitions."""
    from src.services.broker_execution import _lock
    from src.services.confirmed_execution import tick_lock

    with _lock:
        with tick_lock(strategy_id) as acquired:
            if not acquired:
                raise SdkError("Strategy evaluation is in progress; retry the status transition after it completes.")
            return _update_status(strategy_id, update)


def _update_status(strategy_id, update):
    row = _get_row(strategy_id)
    current = row["status"]
    target = update.status

    if target not in _VALID_STATUSES:
        raise StrategyInvalidStatusTransition(f"Unknown status '{target}'.")
    if target == current:
        return _row_to_response(row)  # no-op
    _validate_status_transition(current, target)
    from src.services.broker_store import route as broker_route

    if target == "paper" and row["exit_only_mode"] == "live":
        raise StrategyInvalidStatusTransition("Confirmed live exposure must settle flat before paper execution.")
    if target == "paper" and broker_route(strategy_id):
        raise StrategyInvalidStatusTransition(
            "Broker-bound strategies use live execution; test with a separate paper strategy before binding."
        )
    if target == "live":
        from src.services import paper_confirmed

        if row["exit_only_mode"] == "paper" or paper_confirmed.has_exposure(strategy_id):
            raise StrategyInvalidStatusTransition("Confirmed paper exposure must settle flat before live execution.")

    # Gate transitions that move real money or stop live execution.
    needs_confirm = target == "live" or (current == "live" and target in {"inactive", "archived"})
    if needs_confirm and not update.confirm:
        from src.shared.errors import ConfirmationRequired

        raise ConfirmationRequired(
            f"Transition {current} → {target} requires confirm=true.",
            suggestion="This transition affects real funds or stops live execution. Re-submit with confirm=true.",
        )

    prior_allocation = allocation_service.get_active_allocation(strategy_id)
    if target == "live":
        venue_route = broker_route(strategy_id)
        if venue_route:
            if update.allocation:
                raise StrategyInvalidStatusTransition(
                    "Robinhood uses its configured venue allocation, not a wallet allocation."
                )
            # Broker routes use caller-owned evaluation and confirmed fills.
            # Activation does not modify Robinhood's approval preference.
            from src.services import broker_execution

            broker_execution.run(broker_execution.preflight, venue_route)
        else:
            _record_dex_allocation(strategy_id, update)

    # Sync status upstream.
    try:
        mangrove_ai_client().strategies.update_status(row["mangrove_id"], target)
    except Exception as e:  # noqa: BLE001
        if target == "live" and prior_allocation is None:
            allocation_service.release_allocation(strategy_id)
        raise SdkError(f"strategies.update_status failed: {e}") from e

    _apply_scheduler_effects(strategy_id, row, target, current)
    _set_status(strategy_id, target)
    if "live" in (target, current):
        from src.services import portfolio_risk_service

        portfolio_risk_service.rebaseline(reason=f"status_{current}_to_{target}")
    _log.info(
        "strategy.status_changed",
        strategy_id=strategy_id,
        from_status=current,
        to_status=target,
        allocation=bool(update.allocation),
    )
    return _row_to_response(_get_row(strategy_id))


def _record_dex_allocation(strategy_id, update):
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


# -- Cron tick ---------------------------------------------------------------


def tick(strategy_id: str) -> None:
    from src.services import confirmed_execution

    with confirmed_execution.tick_lock(strategy_id) as acquired:
        if acquired:
            _tick_locked(strategy_id)


def _tick_locked(strategy_id: str) -> None:
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
    from src.services import broker_execution, broker_store

    if broker_store.route(strategy_id):
        broker_execution.tick(strategy_id)
        return
    row = _get_row(strategy_id)
    if row["exit_only_mode"] == "paper" or (row["status"] == "paper" and _resolve_evaluation_lane(row) == "stateless"):
        try:
            from src.services import paper_confirmed

            paper_confirmed.tick(strategy_id)
            _finish_exit_monitor_if_flat(strategy_id)
        except Exception as exc:
            trade_log.log_evaluation(Evaluation(id=str(uuid.uuid4()), strategy_id=strategy_id,
                                               timestamp=trade_log.now_utc(), duration_ms=0,
                                               status="error", error_msg=str(exc)))
            _log.error("strategy.tick.errored", strategy_id=strategy_id, exception=str(exc))
        return
    if row["status"] == "live" or row["exit_only_mode"] == "live":
        try:
            _tick_live(strategy_id)
        except Exception as exc:
            trade_log.log_evaluation(
                Evaluation(
                    id=str(uuid.uuid4()),
                    strategy_id=strategy_id,
                    timestamp=trade_log.now_utc(),
                    duration_ms=0,
                    status="error",
                    error_msg=str(exc),
                )
            )
            _log.error("strategy.tick.errored", strategy_id=strategy_id, exception=str(exc))
        return
    tick_id = str(uuid.uuid4())
    start_ns = time.monotonic()
    with with_correlation_id(tick_id):
        try:
            row = _get_row(strategy_id)
        except StrategyNotFound:
            _log.error("strategy.tick.errored", strategy_id=strategy_id, tick_id=tick_id, exception="StrategyNotFound")
            return
        mode = row["status"]
        _log.info(
            "strategy.tick.started", strategy_id=strategy_id, tick_id=tick_id, timeframe=row["timeframe"], mode=mode
        )

        # Only run the tick for paper or live strategies (defensive).
        if mode not in {"paper", "live"}:
            _log.info(
                "strategy.tick.completed",
                strategy_id=strategy_id,
                tick_id=tick_id,
                order_count=0,
                duration_ms=0,
                reason="not active",
            )
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
                    row["mangrove_id"],
                    persist=True,
                )
        except Exception as e:  # noqa: BLE001
            duration_ms = int((time.monotonic() - start_ns) * 1000)
            trade_log.log_evaluation(
                Evaluation(
                    id=str(uuid.uuid4()),
                    strategy_id=strategy_id,
                    timestamp=trade_log.now_utc(),
                    duration_ms=duration_ms,
                    status="error",
                    error_msg=f"SDK evaluate failed: {e}",
                )
            )
            _log.error(
                "strategy.tick.errored",
                strategy_id=strategy_id,
                tick_id=tick_id,
                exception=str(e),
                duration_ms=duration_ms,
            )
            return

        # Extract OrderIntents from the SDK response (defensive about
        # response shape; logs — never silently drops — malformed intents).
        order_intents = _extract_order_intents(sdk_resp, strategy_id=strategy_id, tick_id=tick_id)

        # Find the wallet + chain_id for live execution (allocation provides it).
        wallet_address = None
        chain_id = None
        slippage_pct = None
        allocation_amount = None
        if mode == "live":
            wallet_address, chain_id, slippage_pct, allocation_amount = _get_live_allocation(strategy_id)

        # Log the evaluation FIRST so trades can FK-reference it. The
        # evaluation describes the SDK call outcome, not the downstream
        # execution outcomes; those get their own rows in `trades`.
        evaluation_id = str(uuid.uuid4())
        duration_ms = int((time.monotonic() - start_ns) * 1000)
        sdk_dump = sdk_resp.model_dump() if hasattr(sdk_resp, "model_dump") else {}
        trade_log.log_evaluation(
            Evaluation(
                id=evaluation_id,
                strategy_id=strategy_id,
                timestamp=trade_log.now_utc(),
                market_snapshot=_market_snapshot(sdk_dump, asset=row["asset"]),
                sdk_response=sdk_dump,
                order_intents=order_intents,
                duration_ms=duration_ms,
                status="ok",
            )
        )

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
                        (
                            json.dumps(exec_state, default=str),
                            json.dumps(open_positions_blob, default=str),
                            trade_log.now_utc().isoformat(),
                            strategy_id,
                        ),
                    )
                else:
                    conn.execute(
                        "UPDATE strategies SET execution_state_json = ?, updated_at = ? WHERE id = ?",
                        (json.dumps(exec_state, default=str), trade_log.now_utc().isoformat(), strategy_id),
                    )
                conn.commit()
            except Exception as e:  # noqa: BLE001
                _log.error("strategy.execution_state.persist_failed", strategy_id=strategy_id, exception=str(e))

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
                _log.warning(
                    "strategy.tick.portfolio_halt",
                    strategy_id=strategy_id,
                    tick_id=tick_id,
                    drawdown=pr.get("drawdown"),
                    book_value=pr.get("book_value"),
                    high_water_mark=pr.get("high_water_mark"),
                    reason=pr.get("reason"),
                )
                _log.info(
                    "strategy.tick.completed",
                    strategy_id=strategy_id,
                    tick_id=tick_id,
                    order_count=0,
                    duration_ms=duration_ms,
                    reason="portfolio_halt",
                )
                return

        if order_intents:
            order_executor.execute_many(
                order_intents,
                mode=mode,
                strategy_id=strategy_id,
                evaluation_id=evaluation_id,
                wallet_address=wallet_address,
                chain_id=chain_id,
                slippage_pct=slippage_pct,
                max_input_amount=allocation_amount,
            )

        _log.info(
            "strategy.tick.completed",
            strategy_id=strategy_id,
            tick_id=tick_id,
            order_count=len(order_intents),
            duration_ms=duration_ms,
        )


def _finish_exit_monitor_if_flat(strategy_id):
    from src.services import confirmed_execution

    row = _get_row(strategy_id)
    if row["exit_only_mode"] == "paper":
        from src.services import paper_confirmed

    exposure = paper_confirmed.has_exposure(strategy_id) if row["exit_only_mode"] == "paper" else confirmed_execution.has_exposure(strategy_id)
    if row["exit_only_mode"] and not exposure:
        conn = get_connection()
        conn.execute("UPDATE strategies SET exit_only_mode=NULL WHERE id=?", (strategy_id,))
        conn.commit()
        if row["exit_only_mode"] == "live" and row["status"] != "live":
            allocation_service.release_allocation(strategy_id)
        if row["status"] not in {"live", "paper"}:
            scheduler_service.cancel_job(strategy_id)
        if row["exit_only_mode"] == "live":
            from src.services import portfolio_risk_service

            portfolio_risk_service.rebaseline(reason="confirmed_flat")


def _tick_live(strategy_id):
    """Externally executed LIVE always evaluates a confirmed, caller-owned book."""
    from src.services import confirmed_execution as confirmed
    from src.services import dex_confirmed, portfolio_risk_service

    dex_confirmed.reconcile(strategy_id)
    portfolio = portfolio_risk_service.check_before_live_execution()
    row = _get_row(strategy_id)
    wallet, chain_id, slippage, allocation = _get_live_allocation(strategy_id)
    if not wallet:
        raise SdkError("Live strategy has no retained execution allocation.")
    revision = confirmed.snapshot_revision(strategy_id)
    mark = order_executor._fetch_mark_price(row["asset"])
    state, positions, closes, complete = confirmed.dex_inputs(strategy_id, mark)
    if confirmed.snapshot_revision(strategy_id) != revision:
        raise SdkError("Confirmed book changed while preparing evaluation.")
    pending = dex_confirmed.orders(strategy_id, pending=True)
    state["allow_entries"] = bool(complete and not pending and portfolio["allowed"] and row["status"] == "live")
    delivered, bootstrap = confirmed.close_delivery(state, closes, complete=complete)
    payload = _stateless_strategy_dict(row)
    payload.update(execution_state=state, closed_positions=delivered, closed_positions_bootstrap_complete=bootstrap)
    if "reference_capital" in state:
        payload["execution_config"].update(
            initial_balance=state["reference_capital"], initial_cash_balance=state["reference_capital"]
        )
    result = mangrove_ai_client().execution.evaluate_by_object(payload, persist=False, open_positions=positions)
    dump = result.model_dump()
    if dump.get("success") is not True:
        raise SdkError("Mangrove evaluation did not succeed; no order submitted.")
    eid = str(uuid.uuid4())
    intents = _extract_order_intents(result, strategy_id=strategy_id, tick_id=eid)
    accepted = confirmed.save_risk(strategy_id, state, dump, delivered, revision, complete=complete and not pending)
    if not accepted or not state["allow_entries"]:
        intents = confirmed.entries_only_removed(intents)
    exits = [i for i in intents if i.action == "exit"]
    intents = exits or intents
    if not exits and len(intents) > 1:
        raise SdkError("Only one entry may be submitted per evaluation.")
    trade_log.log_evaluation(
        Evaluation(
            id=eid,
            strategy_id=strategy_id,
            timestamp=trade_log.now_utc(),
            duration_ms=0,
            status="ok" if accepted else "skipped",
            sdk_response=dump,
            order_intents=intents,
            error_msg=None if accepted else "Financial history or newer book prevented risk/entry commitment.",
        )
    )
    for intent in intents:
        if intent.symbol not in {row["asset"], row["asset"].split("-")[0]}:
            raise SdkError("Intent asset does not match the strategy.")
        if intent.input_token_address or intent.output_token_address:
            raise SdkError("Engine strategy intents must use asset quantities and the retained quote allocation.")
        template = next(
            (
                p
                for p in dump.get("proposed_positions") or []
                if p.get("id") == intent.engine_position_id and p.get("execution_confirmed") is False
            ),
            None,
        )
        if intent.action == "enter" and not template:
            raise SdkError("Mangrove did not return a prospective position template.")
        if intent.action == "enter" and (
            not portfolio_risk_service.check_before_live_execution()["allowed"]
            or _get_row(strategy_id)["status"] != "live"
        ):
            continue
        try:
            oid = dex_confirmed.reserve(
                strategy_id, eid, intent, template or {}, (revision[0] + 1, revision[1]) if accepted else None
            )
            if oid:
                dex_confirmed.execute(
                    oid,
                    intent,
                    wallet,
                    chain_id,
                    slippage,
                    min(allocation, state["cash_balance"]) if intent.action == "enter" else None,
                )
        except Exception as exc:
            if len(exits) < 2:
                raise
            _log.warning(
                "strategy.exit_batch.order_failed",
                strategy_id=strategy_id,
                position_id=intent.engine_position_id,
                error=str(exc),
            )

    _finish_exit_monitor_if_flat(strategy_id)
