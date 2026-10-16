"""Mangrove evaluation -> Robinhood execution -> confirmed-fill reconciliation.

One process-wide lock covers OAuth refresh and venue reservations. Durable
pending-order rows also stop duplicate submission after a crash or restart.
"""

from __future__ import annotations

import json
import threading
import uuid
from decimal import Decimal
from typing import Literal

import anyio
from pydantic import BaseModel, ConfigDict, Field

from src.models.domain import Evaluation
from src.services import broker_store as store
from src.services import robinhood_client, trade_log
from src.services.robinhood_venue import RobinhoodVenue
from src.shared.errors import SdkError
from src.shared.logging import get_logger

_lock = threading.RLock()
_log = get_logger(__name__)


class VenueAllocation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    venue: Literal["robinhood"] = "robinhood"
    asset_class: Literal["equity", "crypto"]
    capital: Decimal = Field(gt=0, allow_inf_nan=False)
    max_order_usd: Decimal = Field(gt=0, allow_inf_nan=False)
    slippage_pct: Decimal = Field(gt=0, le=Decimal("0.0025"), allow_inf_nan=False)


async def _with_venue(operation, *args):
    async with robinhood_client.session() as client:
        venue = RobinhoodVenue(client, await robinhood_client.catalog(client))
        return await operation(venue, *args)


def run(operation, *args):
    with _lock:
        try:
            return anyio.run(_with_venue, operation, *args)
        except ExceptionGroup as group:
            errors = [group]
            while len(errors) == 1 and isinstance(errors[0], BaseExceptionGroup):
                errors = list(errors[0].exceptions)
            if len(errors) == 1 and isinstance(errors[0], SdkError):
                raise errors[0] from None
            raise SdkError(
                "Robinhood connection failed; existing order outcomes must be reconciled before retrying."
            ) from None


async def inspect(venue):
    account = await venue.account()
    setting = await venue.call("get_trade_approval_setting", account_number=account["account_number"])
    portfolio = await venue.call("get_portfolio", account_number=account["account_number"])
    return {
        "venue": "robinhood",
        "connected": True,
        "account": "****" + account["account_number"][-4:],
        "account_state": account["state"],
        "crypto_linked": bool(account.get("rhc_account_number")),
        "trade_approvals": setting["setting"]["human_must_approve_trades"],
        "buying_power": portfolio.get("buying_power"),
        "crypto_buying_power": portfolio.get("crypto_buying_power"),
        "tool_count": len(venue.tools),
    }


async def preflight(venue, route):
    await venue.account(route["account_number"])
    if await venue.balance(route) < 0:
        raise SdkError("Robinhood returned invalid buying power.")
    return await inspect(venue)


async def configure(venue, strategy_id, allocation):
    config = VenueAllocation.model_validate(allocation)
    if config.max_order_usd > config.capital:
        raise SdkError("Per-order maximum cannot exceed committed capital.")
    account = await venue.account()
    if config.asset_class == "crypto" and not account.get("rhc_account_number"):
        raise SdkError("Robinhood crypto account onboarding is incomplete.")
    with store.connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT status FROM strategies WHERE id=?", (strategy_id,)).fetchone()
        if not row or row["status"] not in {"draft", "inactive"}:
            raise SdkError("Configure the execution venue while the strategy is draft or inactive.")
        if (
            conn.execute("SELECT 1 FROM trades WHERE strategy_id=? LIMIT 1", (strategy_id,)).fetchone()
            or conn.execute("SELECT 1 FROM broker_orders WHERE strategy_id=? LIMIT 1", (strategy_id,)).fetchone()
        ):
            raise SdkError("A strategy with execution history cannot be reassigned; create a new strategy.")
        conn.execute(
            """INSERT INTO broker_routes(strategy_id,venue,account_number,rhs_account_number,asset_class,capital,
            max_order_usd,slippage_pct,created_at) VALUES (?,?,?,?,?,?,?,?,?)
            ON CONFLICT(strategy_id) DO UPDATE SET account_number=excluded.account_number,
            rhs_account_number=excluded.rhs_account_number,asset_class=excluded.asset_class,
            capital=excluded.capital,max_order_usd=excluded.max_order_usd,slippage_pct=excluded.slippage_pct""",
            (
                strategy_id,
                "robinhood",
                account["account_number"],
                account["rhs_account_number"],
                config.asset_class,
                str(config.capital),
                str(config.max_order_usd),
                str(config.slippage_pct),
                store.now(),
            ),
        )
        # Broker execution is always caller-owned, non-persisting evaluation.
        conn.execute(
            "UPDATE strategies SET evaluation_lane='stateless',execution_state_json=NULL,open_positions_json='[]' WHERE id=?",
            (strategy_id,),
        )
    return {
        "strategy_id": strategy_id,
        **config.model_dump(mode="json"),
        "account": "****" + account["account_number"][-4:],
        "active": False,
    }


async def reconcile(venue, strategy_id=None):
    updated = []
    for order in store.orders(strategy_id, pending=True):
        route = store.route(order["strategy_id"])
        await venue.account(route["account_number"])
        if order["approval_id"]:
            approval = await venue.approval(route, order["approval_id"])
            if approval and approval["state"] in {"APPROVAL_STATE_DECLINED", "APPROVAL_STATE_EXPIRED"}:
                store.mark(order["id"], "rejected", error=approval["state"])
                updated.append(order["id"])
                continue
        snapshot = await venue.find_order(route, order)
        if snapshot:
            store.apply_snapshot(order["id"], snapshot, asset_class=route["asset_class"])
            updated.append(order["id"])
        elif order["status"] == "submitting":
            # A crash could occur on either side of the HTTP request. Absence
            # from history is not proof of rejection; never retry automatically.
            store.mark(order["id"], "uncertain", error="SUBMISSION_OUTCOME_UNKNOWN")
    return {"reconciled": updated, "pending": len(store.orders(strategy_id, pending=True))}


def journal(strategy_id):
    # Never expose private account IDs or the raw broker request.
    fields = (
        "id",
        "strategy_id",
        "evaluation_id",
        "broker_order_id",
        "status",
        "filled_quantity",
        "filled_notional",
        "fees",
        "error_code",
        "created_at",
        "updated_at",
    )
    return [{key: row[key] for key in fields} for row in store.orders(strategy_id)]


async def cancel(venue, order_id):
    order = next((o for o in store.orders(pending=True) if o["id"] == order_id), None)
    if not order or not order["broker_order_id"]:
        raise SdkError("Cancel requires a reconciled, pending broker order ID.")
    route = store.route(order["strategy_id"])
    await venue.account(route["account_number"])
    store.mark(order_id, "cancel_pending")
    await venue.cancel(route, order["broker_order_id"])
    await reconcile(venue, order["strategy_id"])
    return next(o for o in journal(order["strategy_id"]) if o["id"] == order_id)


async def _tick(venue, strategy_id):
    from src.services import portfolio_risk_service, strategy_service
    from src.shared.clients.mangrove import mangrove_ai_client
    from src.shared.db.sqlite import get_connection

    route = store.route(strategy_id)
    await reconcile(venue, strategy_id)
    row = strategy_service._get_row(strategy_id)
    if row["status"] != "live":
        return
    if not portfolio_risk_service.check_before_live_execution()["allowed"]:
        return
    pending = store.orders(strategy_id, pending=True)
    symbol = row["asset"].removesuffix("-USD")
    quote = await venue.quote(route, symbol)
    mark = float((store.decimal(quote["ask_price"]) + store.decimal(quote["bid_price"])) / 2)
    state, positions = store.execution_inputs(strategy_id, mark)
    payload = strategy_service._stateless_strategy_dict(row)
    payload["execution_state"] = state
    payload["execution_config"]["initial_balance"] = float(route["capital"])
    payload["execution_config"]["initial_cash_balance"] = float(route["capital"])
    # All strategy logic, research and sizing remain in Mangrove.
    result = await anyio.to_thread.run_sync(
        lambda: mangrove_ai_client().execution.evaluate_by_object(payload, persist=False, open_positions=positions)
    )
    dump = result.model_dump()
    if dump.get("success") is not True:
        raise SdkError("Mangrove evaluation did not succeed; no broker order submitted.")
    eid = str(uuid.uuid4())
    intents = strategy_service._extract_order_intents(result, strategy_id=strategy_id, tick_id=eid)
    blocked = bool(pending and intents)
    trade_log.log_evaluation(
        Evaluation(
            id=eid,
            strategy_id=strategy_id,
            timestamp=trade_log.now_utc(),
            duration_ms=0,
            status="skipped" if blocked else "ok",
            error_msg="Execution deferred until pending broker orders reconcile." if blocked else None,
            sdk_response=dump,
            order_intents=intents,
            market_snapshot=strategy_service._market_snapshot(dump, asset=row["asset"]),
        )
    )
    if not intents and any(o.get("status") == "filled" for o in dump.get("new_orders") or []):
        raise SdkError("Mangrove returned an unsupported filled intent; execution stopped.")
    if blocked:
        # Keep evaluating confirmed holdings, but never submit against an
        # uncertain book. An exit takes priority over the unfilled remainder of
        # an entry. Cancellation can race a fill: reconcile, then evaluate the
        # resulting holdings afresh on the next scheduled tick.
        needs_exit = any(
            i.action == "exit" and i.side == "sell" and i.symbol == symbol
            and any(p["id"] == i.engine_position_id for p in positions)
            for i in intents
        )
        if needs_exit and strategy_service._get_row(strategy_id)["status"] == "live":
            for order in pending:
                if json.loads(order["intent_json"])["side"] == "buy" and order["broker_order_id"] and order["status"] != "cancel_pending":
                    await cancel(venue, order["id"])
        return
    if not intents:
        # Advance non-execution risk/cooldown state only when no modeled trade
        # occurred. Broker cash and positions always overwrite it next tick.
        conn = get_connection()
        conn.execute(
            "UPDATE strategies SET execution_state_json=?,open_positions_json=? WHERE id=?",
            (
                json.dumps(dump.get("execution_state") or state),
                json.dumps(dump.get("open_positions") or positions),
                strategy_id,
            ),
        )
        conn.commit()
        return
    if len(intents) != 1:
        raise SdkError("Broker execution currently requires one Mangrove intent per evaluation.")
    intent = intents[0]
    if intent.symbol != symbol:
        raise SdkError("Mangrove intent does not match the strategy asset.")
    # Reject missing engine position state BEFORE a potentially irreversible
    # broker operation. Entry bracket templates are retained for future ticks.
    template = next((p for p in dump.get("open_positions") or [] if p.get("id") == intent.engine_position_id), {})
    if intent.side == "buy" and not template:
        raise SdkError("Mangrove did not return the entry position template needed for reconciliation.")
    args, required = await venue.prepare(route, intent)
    qty = store.decimal(args["quantity"])
    if intent.side == "buy":
        if (
            required > store.decimal(route["max_order_usd"])
            or required > store.decimal(state["cash_balance"])
            or required > await venue.balance(route)
        ):
            raise SdkError("Mangrove order exceeds available venue capital or buying power.")
    else:
        position = next((p for p in positions if p["id"] == intent.engine_position_id), None)
        if not position or qty > store.decimal(position["position_size"]) or qty > await venue.sellable(route, symbol):
            raise SdkError("Mangrove exit exceeds confirmed holdings available to this strategy.")
    # Recheck activation after network/evaluation work; a pause must prevent a
    # not-yet-submitted order. No per-order human approval is added here.
    if (
        strategy_service._get_row(strategy_id)["status"] != "live"
        or not portfolio_risk_service.check_before_live_execution()["allowed"]
    ):
        return
    ref_id = str(uuid.uuid4())
    if not store.reserve(strategy_id, eid, intent, args, template, ref_id):
        return
    try:
        response = await venue.submit(route, args, ref_id)
        if response.get("order"):
            store.apply_snapshot(ref_id, response["order"], asset_class=route["asset_class"])
        elif response.get("approval"):
            store.mark(ref_id, "awaiting_approval", approval_id=response["approval"]["approval_id"])
        else:
            store.mark(ref_id, "uncertain", error="UNRECOGNIZED_SUBMISSION_RESPONSE")
    except BaseException:
        # Includes cancellation/process interruption. Unknown != failed.
        store.mark(ref_id, "uncertain", error="SUBMISSION_OUTCOME_UNKNOWN")
        raise


def tick(strategy_id):
    try:
        run(_tick, strategy_id)
    except Exception as exc:
        message = (
            exc.message
            if isinstance(exc, SdkError)
            else "Broker execution stopped; inspect venue status and order journal."
        )
        trade_log.log_evaluation(
            Evaluation(
                id=str(uuid.uuid4()),
                strategy_id=strategy_id,
                timestamp=trade_log.now_utc(),
                duration_ms=0,
                status="error",
                error_msg=message,
            )
        )
        _log.warning("broker.tick.stopped", strategy_id=strategy_id)


def reconcile_pending():
    if not store.orders(pending=True):
        return
    try:
        run(reconcile)
    except Exception:
        _log.warning("broker.reconciliation.unavailable")
