"""Mangrove evaluation -> Robinhood execution -> confirmed-fill reconciliation.

One process-wide lock covers OAuth refresh and venue reservations. Durable
pending-order rows also stop duplicate submission after a crash or restart.
"""

from __future__ import annotations

import inspect as py_inspect
import json
import threading
import uuid
from datetime import datetime, timezone
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
    external_execution_mode: Literal["legacy", "confirmed_v2"] = "legacy"


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
    if config.external_execution_mode == "confirmed_v2":
        from src.shared.clients.mangrove import mangrove_ai_client

        method = mangrove_ai_client().execution.evaluate_by_object
        if not _supports_external_execution(method):
            raise SdkError("Installed Mangrove SDK does not support confirmed execution feedback v2.")
    with store.connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT status FROM strategies WHERE id=?", (strategy_id,)).fetchone()
        if not row or row["status"] not in {"draft", "inactive"}:
            raise SdkError("Configure the execution venue while the strategy is draft or inactive.")
        has_history = any(
            conn.execute(f"SELECT 1 FROM {table} WHERE strategy_id=? LIMIT 1", (strategy_id,)).fetchone()
            for table in ("trades", "broker_orders", "evaluations", "positions", "broker_execution_events")
        )
        saved = conn.execute(
            "SELECT execution_state_json,open_positions_json FROM strategies WHERE id=?", (strategy_id,)
        ).fetchone()
        saved_state = json.loads(saved["execution_state_json"] or "{}")
        saved_positions = json.loads(saved["open_positions_json"] or "[]")
        old_route = conn.execute(
            "SELECT * FROM broker_routes WHERE strategy_id=?", (strategy_id,)
        ).fetchone()
        if (
            conn.execute("SELECT 1 FROM trades WHERE strategy_id=? LIMIT 1", (strategy_id,)).fetchone()
            or conn.execute("SELECT 1 FROM broker_orders WHERE strategy_id=? LIMIT 1", (strategy_id,)).fetchone()
        ):
            raise SdkError("A strategy with broker execution history cannot be reassigned.")
        if config.external_execution_mode == "confirmed_v2":
            valid_v2 = bool(
                old_route
                and old_route["external_execution_mode"] == "confirmed_v2"
                and isinstance(saved_state.get("external_execution"), dict)
                and type(saved_state["external_execution"].get("applied_through")) is int
            )
            pristine = not has_history and not saved_positions and not saved_state
            same_v2_config = bool(
                valid_v2
                and old_route["asset_class"] == config.asset_class
                and old_route["capital"] == str(config.capital)
                and old_route["max_order_usd"] == str(config.max_order_usd)
                and old_route["slippage_pct"] == str(config.slippage_pct)
                and old_route["account_number"] == account["account_number"]
                and old_route["rhs_account_number"] == account["rhs_account_number"]
            )
            if not (pristine or same_v2_config):
                raise SdkError("Confirmed execution feedback can only be enabled on a pristine strategy or re-saved unchanged.")
        conn.execute(
            """INSERT INTO broker_routes(strategy_id,venue,account_number,rhs_account_number,asset_class,capital,
            max_order_usd,slippage_pct,created_at,external_execution_mode) VALUES (?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(strategy_id) DO UPDATE SET account_number=excluded.account_number,
            rhs_account_number=excluded.rhs_account_number,asset_class=excluded.asset_class,
            capital=excluded.capital,max_order_usd=excluded.max_order_usd,slippage_pct=excluded.slippage_pct,
            external_execution_mode=excluded.external_execution_mode""",
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
                config.external_execution_mode,
            ),
        )
        # Broker execution is always caller-owned, non-persisting evaluation.
        if config.external_execution_mode == "legacy":
            conn.execute(
                "UPDATE strategies SET evaluation_lane='stateless',execution_state_json=NULL,open_positions_json='[]' WHERE id=?",
                (strategy_id,),
            )
        else:
            conn.execute("UPDATE strategies SET evaluation_lane='stateless' WHERE id=?", (strategy_id,))
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


def _plain(value):
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json", exclude_none=True)
    return value


def _require_external_execution_sdk():
    from src.shared.clients.mangrove import mangrove_ai_client

    method = mangrove_ai_client().execution.evaluate_by_object
    if not _supports_external_execution(method):
        raise SdkError("Installed Mangrove SDK does not support confirmed execution feedback v2.")
    return method


def _supports_external_execution(method):
    try:
        return "external_execution" in py_inspect.signature(method).parameters
    except (TypeError, ValueError):
        return False


def _confirmed_position_view(hosted_positions, confirmed_positions):
    """Keep hosted risk templates while broker fill facts remain authoritative."""
    if not isinstance(hosted_positions, list):
        raise SdkError("Mangrove v2 response is missing broker-confirmed open positions.")
    if any(not isinstance(item, dict) or not item.get("id") for item in hosted_positions):
        raise SdkError("Mangrove v2 response contains an invalid open position.")
    hosted_ids = [item["id"] for item in hosted_positions]
    if len(set(hosted_ids)) != len(hosted_ids):
        raise SdkError("Mangrove v2 response contains duplicate open position IDs.")
    hosted = {item["id"]: item for item in hosted_positions}
    actual_ids = [item["id"] for item in confirmed_positions]
    if len(set(actual_ids)) != len(actual_ids) or set(hosted_ids) != set(actual_ids):
        raise SdkError("Mangrove v2 open positions do not match broker-confirmed holdings.")
    actual = []
    for confirmed in confirmed_positions:
        item = dict(hosted.get(confirmed["id"]) or confirmed)
        for key in ("id", "asset", "side", "position_size", "entry_price", "cost", "entry_timestamp", "open"):
            if key in confirmed:
                item[key] = confirmed[key]
        actual.append(item)
    return actual


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

    route = store.route(strategy_id)
    await reconcile(venue, strategy_id)
    row = strategy_service._get_row(strategy_id)
    if row["status"] != "live":
        return
    if not portfolio_risk_service.check_before_live_execution()["allowed"]:
        return
    confirmed_v2 = route.get("external_execution_mode", "legacy") == "confirmed_v2"
    sdk_method = _require_external_execution_sdk() if confirmed_v2 else None
    pending = store.orders(strategy_id, pending=True)
    symbol = row["asset"].removesuffix("-USD")
    quote = await venue.quote(route, symbol)
    mark = float((store.decimal(quote["ask_price"]) + store.decimal(quote["bid_price"])) / 2)
    if confirmed_v2:
        state, positions, broker_revision, external_events, expected_state, expected_positions = store.execution_inputs(
            strategy_id, mark, include_revision=True, include_external_events=True
        )
        expected_external = expected_state.get("external_execution") or {}
        expected_cursor = expected_external.get("applied_through", 0)
        if type(expected_cursor) is not int or expected_cursor < 0:
            raise SdkError("Saved confirmed execution cursor is invalid; execution stopped.")
    else:
        state, positions = store.execution_inputs(strategy_id, mark)
    payload = strategy_service._stateless_strategy_dict(row)
    payload["execution_state"] = state
    payload["execution_config"]["initial_balance"] = float(route["capital"])
    payload["execution_config"]["initial_cash_balance"] = float(route["capital"])
    # All strategy logic, research and sizing remain in Mangrove.
    if confirmed_v2:
        observed_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        external_request = {"contract_version": 2, "observed_at": observed_at, "events": external_events}
        result = await anyio.to_thread.run_sync(
            lambda: sdk_method(
                payload, persist=False, open_positions=positions, external_execution=external_request
            )
        )
    else:
        result = await anyio.to_thread.run_sync(
            lambda: mangrove_ai_client().execution.evaluate_by_object(payload, persist=False, open_positions=positions)
        )
    dump = result.model_dump()
    if dump.get("success") is not True:
        raise SdkError("Mangrove evaluation did not succeed; no broker order submitted.")
    ack_data = None
    if confirmed_v2:
        ack = _plain(getattr(result, "external_execution_ack", None))
        if not isinstance(ack, dict) or ack.get("contract_version") != 2:
            raise SdkError("Mangrove server did not acknowledge confirmed execution feedback v2.")
        receipts = ack.get("receipts")
        if not isinstance(receipts, list):
            raise SdkError("Mangrove v2 acknowledgement is missing typed event receipts.")
        expected_last = external_events[-1]["sequence"] if external_events else expected_cursor
        if ack.get("applied_through") != expected_last:
            raise SdkError("Mangrove v2 acknowledgement did not cover the submitted event batch.")
        hosted_state = dump.get("execution_state")
        hosted_positions = dump.get("open_positions")
        if not isinstance(hosted_state, dict) or not isinstance(hosted_positions, list):
            raise SdkError("Mangrove v2 response omitted state required for durable acknowledgement.")
        confirmed_positions = _confirmed_position_view(hosted_positions, positions)
        active_ids = ack.get("active_position_ids")
        if (
            not isinstance(active_ids, list)
            or len(set(active_ids)) != len(active_ids)
            or set(active_ids) != {item["id"] for item in confirmed_positions}
        ):
            raise SdkError("Mangrove v2 acknowledgement position IDs do not match broker-confirmed holdings.")
        event_ids = [event["event_id"] for event in external_events]
        disposition = dump.get("external_execution_disposition")
        if disposition not in {"evaluated", "duplicate_bar"}:
            raise SdkError("Mangrove v2 response has an unknown evaluation disposition.")
        ack_data = (
            event_ids, hosted_state, confirmed_positions, receipts,
            expected_state, expected_positions, expected_cursor, broker_revision, external_events,
        )
    eid = str(uuid.uuid4())
    intents = strategy_service._extract_order_intents(result, strategy_id=strategy_id, tick_id=eid)
    if confirmed_v2 and dump.get("external_execution_disposition") == "duplicate_bar" and (
        intents or any(o.get("status") == "filled" for o in dump.get("new_orders") or [])
    ):
        raise SdkError("Mangrove marked a bar duplicate while returning a new order intent; execution stopped.")
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
    def commit_ack(reservation=None):
        if ack_data is None:
            return None
        (event_ids, hosted_state, confirmed_positions, receipts, expected_state,
         expected_positions, expected_cursor, broker_revision, external_events) = ack_data
        return store.acknowledge_confirmed_close_events(
            strategy_id,
            event_ids,
            hosted_state,
            open_positions=confirmed_positions,
            expected_state=expected_state,
            expected_open_positions=expected_positions,
            expected_applied_through=expected_cursor,
            expected_broker_revision=broker_revision,
            receipts=receipts,
            submitted_events=external_events,
            reservation=reservation,
        )
    if confirmed_v2 and dump.get("external_execution_disposition") == "duplicate_bar":
        commit_ack()
        return
    if not intents and any(o.get("status") == "filled" for o in dump.get("new_orders") or []):
        raise SdkError("Mangrove returned an unsupported filled intent; execution stopped.")
    if blocked:
        commit_ack()
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
        if not confirmed_v2:
            store.save_evaluation_state(
                strategy_id,
                dump.get("execution_state") or state,
                dump.get("open_positions") or positions,
            )
        else:
            commit_ack()
        return
    if len(intents) != 1:
        raise SdkError("Broker execution currently requires one Mangrove intent per evaluation.")
    intent = intents[0]
    if intent.symbol != symbol:
        raise SdkError("Mangrove intent does not match the strategy asset.")
    # Reject missing engine position state BEFORE a potentially irreversible
    # broker operation. Entry bracket templates are retained for future ticks.
    templates = dump.get("position_templates") if confirmed_v2 else dump.get("open_positions")
    template = next((p for p in templates or [] if p.get("id") == intent.engine_position_id), {})
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
    if confirmed_v2:
        reserved = commit_ack({
            "evaluation_id": eid,
            "intent": intent,
            "request": args,
            "template": template,
            "ref_id": ref_id,
        })
    else:
        reserved = store.reserve(strategy_id, eid, intent, args, template, ref_id)
    if not reserved:
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
