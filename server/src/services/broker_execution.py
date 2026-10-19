"""Mangrove evaluation -> Robinhood execution -> confirmed-fill reconciliation.

One process-wide lock covers OAuth refresh and venue reservations. Durable
pending-order rows also stop duplicate submission after a crash or restart.
"""

from __future__ import annotations

import inspect as py_inspect
import json
import threading
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Literal

import anyio
from pydantic import BaseModel, ConfigDict, Field

from src.models.domain import Evaluation, OrderIntent
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
    native_stop_loss_enabled: bool = False


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
    if config.native_stop_loss_enabled and config.asset_class != "crypto":
        raise SdkError("Native stop-loss protection is currently supported for crypto routes only.")
    if config.native_stop_loss_enabled and config.external_execution_mode != "confirmed_v2":
        raise SdkError("Native stop-loss protection requires confirmed execution feedback v2.")
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
        if config.native_stop_loss_enabled and not (old_route and old_route["native_stop_loss_enabled"]):
            if has_history or saved_positions or saved_state:
                raise SdkError("Native stop-loss protection must be enabled before strategy execution history exists.")
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
            max_order_usd,slippage_pct,created_at,external_execution_mode,native_stop_loss_enabled) VALUES (?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(strategy_id) DO UPDATE SET account_number=excluded.account_number,
            rhs_account_number=excluded.rhs_account_number,asset_class=excluded.asset_class,
            capital=excluded.capital,max_order_usd=excluded.max_order_usd,slippage_pct=excluded.slippage_pct,
            external_execution_mode=excluded.external_execution_mode,
            native_stop_loss_enabled=excluded.native_stop_loss_enabled""",
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
                int(config.native_stop_loss_enabled),
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
            try:
                store.apply_snapshot(order["id"], snapshot, asset_class=route["asset_class"])
            except SdkError:
                if order["order_kind"] != "protective_stop":
                    raise
                store.mark(order["id"], "uncertain", error="PROTECTIVE_ORDER_IDENTITY_MISMATCH")
                state = "unknown"
                store.set_protection_state(
                    order["strategy_id"], order["protected_position_id"],
                    stop_price=order["hosted_stop_price"], routed_stop_price=order["routed_stop_price"],
                    status=state, detail="Broker snapshot did not prove the requested stop identity; coverage is unknown.",
                    active_order_id=order["id"],
                )
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
        "order_kind",
        "protected_position_id",
        "hosted_stop_price",
        "routed_stop_price",
        "filled_quantity",
        "filled_notional",
        "fees",
        "error_code",
        "created_at",
        "updated_at",
    )
    return [{key: row[key] for key in fields} for row in store.orders(strategy_id)]


def protection_status(strategy_id):
    route = store.route(strategy_id)
    states = store.protection_states(strategy_id)
    orders_by_id = {row["id"]: row for row in store.orders(strategy_id)}
    positions = []
    for saved in states:
        item = dict(saved)
        order = orders_by_id.get(saved.get("active_order_id"))
        held = store.confirmed_position_quantity(strategy_id, saved["position_id"])
        item["confirmed_quantity"] = str(held)
        item["broker_acknowledged_standing"] = False
        if not route or not route.get("native_stop_loss_enabled"):
            item["status"] = "disabled"
        elif held <= 0:
            item["status"] = "flat"
        elif order:
            item["last_broker_observation_at"] = order["updated_at"]
            if order["updated_at"] > saved["updated_at"]:
                item["status"] = "reconciliation_required"
            elif order["status"] not in {"open", "partial"} or not order["broker_order_id"]:
                item["status"] = "uncovered_or_unknown"
            else:
                remaining = store.decimal(json.loads(order["request_json"]).get("quantity", "0")) - store.decimal(order["filled_quantity"])
                accepted_quantity = store.protective_snapshot_quantity(order["id"])
                desired_level_matches = (
                    order["order_kind"] == "protective_stop"
                    and store.decimal(order["hosted_stop_price"]) == store.decimal(saved["desired_stop_price"])
                    and store.decimal(order["routed_stop_price"]) == store.decimal(saved["routed_stop_price"])
                )
                exact_level = (
                    desired_level_matches
                    and accepted_quantity == store.decimal(json.loads(order["request_json"]).get("quantity", "0"))
                )
                if remaining == held and exact_level and saved["status"] == "standing_coverage":
                    item["status"] = "standing_coverage_last_observed"
                    item["broker_acknowledged_standing"] = True
                elif accepted_quantity is None and remaining == held and desired_level_matches:
                    item["status"] = "coverage_unverified"
                else:
                    item["status"] = "quantity_or_level_mismatch"
        else:
            item["status"] = saved["status"]
        positions.append(item)
    deferred = [
        {key: row[key] for key in (
            "id", "position_id", "evaluation_id", "engine_order_id", "intent_json",
            "decision_quantity", "status", "detail", "reserved_order_id", "created_at", "updated_at",
        )}
        for row in store.deferred_exits(strategy_id)
    ]
    return {
        "enabled": bool(route and route.get("native_stop_loss_enabled")),
        "positions": positions,
        "deferred_exits": deferred,
    }


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


def _hosted_stop_price(template, asset):
    """Return one unambiguous pending stop from a hosted position template."""
    if not isinstance(template, dict) or template.get("asset") != asset:
        return None, "hosted_position_template_unavailable"
    stops = [
        order for order in template.get("orders", [])
        if isinstance(order, dict) and order.get("order_type") == "stop_loss"
    ]
    pending = [order for order in stops if order.get("status") == "pending"]
    if len(pending) != 1:
        return None, "hosted_stop_missing_or_ambiguous"
    order = pending[0]
    if order.get("side") != "exit_long" or order.get("position_id") != template.get("id"):
        return None, "hosted_stop_identity_mismatch"
    if order.get("asset") != asset:
        return None, "hosted_stop_asset_mismatch"
    try:
        price = store.decimal(order.get("price"))
        top_level = template.get("stop_loss_price")
        if top_level is not None and store.decimal(top_level) != price:
            return None, "hosted_stop_price_conflict"
    except Exception:
        return None, "hosted_stop_price_invalid"
    if price <= 0:
        return None, "hosted_stop_price_invalid"
    return price, None


async def _sync_native_protection(venue, strategy_id, route, evaluation_id, templates, positions):
    """Install/reconcile only hosted pending stop orders for confirmed holdings.

    The existing one-pending-order-per-strategy index serializes a stop against
    entries and exits. A partially filled entry is canceled and reconciled
    before its confirmed quantity is protected. Replacements wait for terminal
    cancellation; unknown outcomes never fall through to a second sell.
    """
    if not route.get("native_stop_loss_enabled"):
        return
    if route.get("asset_class") != "crypto" or route.get("external_execution_mode") != "confirmed_v2":
        return
    positions_by_id = {item["id"]: item for item in positions}
    templates_by_id = {item.get("id"): item for item in templates or [] if isinstance(item, dict)}
    all_protective = store.orders(strategy_id, order_kind="protective_stop")
    pending = store.orders(strategy_id, pending=True)

    # A partially filled entry still owns the global order slot. Cancel its
    # unfilled remainder, then wait for terminal reconciliation before placing
    # coverage against the now-stable broker-confirmed quantity.
    entry_pending = next((
        order for order in pending
        if order["order_kind"] == "strategy"
        and json.loads(order["intent_json"]).get("side") == "buy"
        and order["protected_position_id"] is None
        and order["status"] in {"partial", "open"}
        and store.decimal(order["filled_quantity"]) > 0
    ), None)
    if entry_pending:
        for position_id, position in positions_by_id.items():
            store.set_protection_state(
                strategy_id, position_id, quantity=position["position_size"],
                status="entry_remainder_pending", detail="Cancel requested; coverage waits for terminal entry reconciliation.",
            )
        await _cancel_if_freshly_active(venue, entry_pending)
        return

    # Any pending order consumes the strategy's sole reservation. Existing
    # stops are reported with their actual broker state; no replacement can be
    # submitted until terminal reconciliation releases the slot.
    if pending:
        active_stop = next((o for o in pending if o["order_kind"] == "protective_stop"), None)
        if active_stop:
            position_id = active_stop["protected_position_id"]
            position = positions_by_id.get(position_id)
            hosted_stop, error = _hosted_stop_price(
                templates_by_id.get(position_id), position.get("asset") if position else ""
            )
            remaining = store.decimal(json.loads(active_stop["request_json"]).get("quantity", "0")) - store.decimal(active_stop["filled_quantity"])
            same_target = (
                not error and position
                and store.decimal(active_stop["hosted_stop_price"]) == hosted_stop
                and remaining == store.decimal(position["position_size"])
                and active_stop["status"] in {"open", "partial"} and active_stop["broker_order_id"]
            )
            accepted_quantity = store.protective_snapshot_quantity(active_stop["id"])
            requested_quantity = store.decimal(json.loads(active_stop["request_json"]).get("quantity", "0"))
            if same_target and accepted_quantity == requested_quantity:
                state, detail = "standing_coverage", "Robinhood reports a standing stop for the confirmed quantity."
            elif same_target and accepted_quantity is None:
                state, detail = "coverage_unverified", "Latest broker snapshot omits the original stop quantity."
            elif active_stop["status"] in {"submitting", "uncertain"}:
                state, detail = "unknown", "Stop submission outcome is unresolved; no replacement sent."
            elif active_stop["status"] == "cancel_pending":
                state, detail = "cancel_pending", "Cancellation is awaiting terminal broker reconciliation."
            elif active_stop["broker_order_id"]:
                state, detail = "cancel_pending", "Old stop must reach terminal state before replacement."
                store.set_protection_state(
                    strategy_id, position_id, stop_price=hosted_stop if not error else None,
                    quantity=position["position_size"] if position else "0", status=state,
                    detail=detail, active_order_id=active_stop["id"],
                )
                await _cancel_if_freshly_active(venue, active_stop)
                return
            else:
                state, detail = "unknown", "Stop has no reconciled broker ID; no replacement sent."
            store.set_protection_state(
                strategy_id, position_id, stop_price=hosted_stop if not error else active_stop["hosted_stop_price"],
                routed_stop_price=active_stop["routed_stop_price"],
                quantity=position["position_size"] if position else "0", status=state,
                detail=detail,
                active_order_id=active_stop["id"],
            )
            if active_stop["status"] == "cancel_pending":
                await _cancel_if_freshly_active(venue, active_stop)
            for other_id, other in positions_by_id.items():
                if other_id != position_id:
                    store.set_protection_state(
                        strategy_id, other_id, quantity=other["position_size"],
                        status="uncovered_serialization_limit",
                        detail="The strategy-wide order slot is occupied by another position's stop.",
                    )
        else:
            for position_id, position in positions_by_id.items():
                store.set_protection_state(
                    strategy_id, position_id, quantity=position["position_size"],
                    status="strategy_order_pending", detail="Protection waits for the strategy order to reach a terminal broker state.",
                )
        return

    for position_id, position in positions_by_id.items():
        held = store.decimal(position["position_size"])
        template = templates_by_id.get(position_id)
        hosted_stop, error = _hosted_stop_price(template, position.get("asset"))
        if error:
            store.set_protection_state(
                strategy_id, position_id, quantity=held, status="unavailable", detail=error,
            )
            continue

        previous = next((o for o in reversed(all_protective) if o["protected_position_id"] == position_id), None)
        if previous and previous["status"] in {"submitting", "uncertain", "open", "partial", "cancel_pending", "awaiting_approval"}:
            remaining = store.decimal(json.loads(previous["request_json"]).get("quantity", "0")) - store.decimal(previous["filled_quantity"])
            same_target = (
                store.decimal(previous["hosted_stop_price"]) == hosted_stop and remaining == held
                and previous["status"] in {"open", "partial"} and previous["broker_order_id"]
            )
            accepted_quantity = store.protective_snapshot_quantity(previous["id"])
            requested_quantity = store.decimal(json.loads(previous["request_json"]).get("quantity", "0"))
            if same_target and accepted_quantity == requested_quantity:
                store.set_protection_state(
                    strategy_id, position_id, stop_price=hosted_stop, routed_stop_price=previous["routed_stop_price"],
                    quantity=held, status="standing_coverage", detail="Robinhood reports a standing stop for the confirmed quantity.",
                    active_order_id=previous["id"],
                )
                continue
            if same_target and accepted_quantity is None:
                store.set_protection_state(
                    strategy_id, position_id, stop_price=hosted_stop, routed_stop_price=previous["routed_stop_price"],
                    quantity=held, status="coverage_unverified", detail="Latest broker snapshot omits the original stop quantity.",
                    active_order_id=previous["id"],
                )
                return
            if previous["broker_order_id"]:
                store.set_protection_state(
                    strategy_id, position_id, stop_price=hosted_stop, quantity=held,
                    status="cancel_pending", detail="Old stop must reach terminal state before replacement.",
                    active_order_id=previous["id"],
                )
                await _cancel_if_freshly_active(venue, previous)
            else:
                store.set_protection_state(
                    strategy_id, position_id, stop_price=hosted_stop, quantity=held,
                    status="unknown", detail="Stop submission or cancellation outcome is unresolved; no replacement sent.",
                    active_order_id=previous["id"],
                )
            return

        try:
            args, routed_stop = await venue.prepare_protective_stop(
                route, position["asset"].removesuffix("-USD"), held, hosted_stop
            )
        except SdkError as exc:
            store.set_protection_state(
                strategy_id, position_id, stop_price=hosted_stop, quantity=held,
                status="unavailable", detail=exc.message,
            )
            continue
        ref_id = str(uuid.uuid4())
        if not store.reserve_protective_stop(
            strategy_id, evaluation_id, position_id, position["asset"], held, hosted_stop,
            routed_stop, args, template, ref_id,
        ):
            store.set_protection_state(
                strategy_id, position_id, stop_price=hosted_stop, quantity=held,
                status="reservation_lost", detail="Position or strategy order reservation changed before stop submit.",
            )
            return
        try:
            response = await venue.submit(route, args, ref_id)
            if response.get("order"):
                store.apply_snapshot(ref_id, response["order"], asset_class="crypto")
                current = next(row for row in store.orders(strategy_id) if row["id"] == ref_id)
                if (current["status"] in {"open", "partial"} and current["broker_order_id"]
                        and store.protective_snapshot_quantity_matches(ref_id)):
                    state, detail = "standing_coverage", "Robinhood acknowledged a standing stop for the confirmed quantity."
                else:
                    state, detail = "reconciling", "Stop submit was acknowledged; reconcile broker state before claiming coverage."
                store.set_protection_state(
                    strategy_id, position_id, stop_price=hosted_stop, routed_stop_price=routed_stop,
                    quantity=held, status=state, detail=detail, active_order_id=ref_id,
                )
            else:
                store.mark(ref_id, "uncertain", error="UNRECOGNIZED_SUBMISSION_RESPONSE")
                store.set_protection_state(
                    strategy_id, position_id, stop_price=hosted_stop, routed_stop_price=routed_stop,
                    quantity=held, status="unknown", detail="Robinhood response did not identify the stop outcome.",
                    active_order_id=ref_id,
                )
        except BaseException:
            store.mark(ref_id, "uncertain", error="SUBMISSION_OUTCOME_UNKNOWN")
            store.set_protection_state(
                strategy_id, position_id, stop_price=hosted_stop, routed_stop_price=routed_stop,
                quantity=held, status="unknown", detail="Stop submission outcome is unresolved; no duplicate sent.",
                active_order_id=ref_id,
            )
            raise

    for old in store.protection_states(strategy_id):
        if old["position_id"] not in positions_by_id and old["status"] not in {"closed", "disabled"}:
            store.set_protection_state(
                strategy_id, old["position_id"], quantity="0", status="closed",
                detail="No broker-confirmed open quantity remains.", active_order_id=old["active_order_id"],
            )


async def _process_deferred_exit(venue, strategy_id, route):
    """Execute a saved hosted exit only after the old protective stop is terminal."""
    from src.services import portfolio_risk_service, strategy_service

    pending_deferred = store.deferred_exits(strategy_id, pending_only=True)
    if not pending_deferred:
        return False
    deferred = pending_deferred[0]
    if store.route_fingerprint(strategy_id) != deferred["route_fingerprint"]:
        store.set_deferred_exit_status(deferred["id"], "blocked", "Venue mandate changed; preserved exit requires review.")
        return True
    pending = store.orders(strategy_id, pending=True)
    if pending:
        blocker = next((order for order in pending if (
            order["order_kind"] == "protective_stop"
            or json.loads(order["intent_json"]).get("side") == "buy"
        )), None)
        store.set_deferred_exit_status(
            deferred["id"], "waiting_stop_terminal", "Earlier order remains pending; exit is not resubmitted."
        )
        if blocker:
            await _cancel_if_freshly_active(venue, blocker)
        return True
    held = store.confirmed_position_quantity(strategy_id, deferred["position_id"])
    if held <= 0:
        store.set_deferred_exit_status(
            deferred["id"], "cleared", "Position is flat after protective-order reconciliation."
        )
        return False
    original = OrderIntent.model_validate_json(deferred["intent_json"])
    decision_quantity = store.decimal(deferred["decision_quantity"])
    authorized = min(store.decimal(original.amount), decision_quantity)
    blocker_delta = Decimal(0)
    if deferred["blocker_order_id"]:
        blocker_order = next((o for o in store.orders(strategy_id)
                              if o["id"] == deferred["blocker_order_id"]), None)
        blocker_intent = json.loads(blocker_order["intent_json"]) if blocker_order else {}
        same_position_reduction = (
            blocker_order and (
                blocker_order["order_kind"] == "protective_stop"
                and blocker_order["protected_position_id"] == deferred["position_id"]
                or blocker_order["order_kind"] == "strategy"
                and blocker_intent.get("side") == "sell"
                and blocker_intent.get("engine_position_id") == deferred["position_id"]
            )
        )
        if same_position_reduction:
            blocker_delta = store.decimal(blocker_order["filled_quantity"]) - store.decimal(
                deferred["blocker_filled_at_decision"]
            )
    remaining_authorized = min(held, max(Decimal(0), authorized - blocker_delta))
    if remaining_authorized <= 0:
        store.set_deferred_exit_status(
            deferred["id"], "cleared", "Protective fills satisfied the hosted exit quantity."
        )
        return False
    intent = original.model_copy(update={"amount": float(remaining_authorized)})
    row = strategy_service._get_row(strategy_id)
    if (
        row["status"] != "live"
        or not portfolio_risk_service.check_before_live_execution()["allowed"]
    ):
        store.set_deferred_exit_status(deferred["id"], "blocked", "Strategy is inactive or execution is halted; no new order was authorized.")
        return True
    symbol = row["asset"].removesuffix("-USD")
    # Existing venue constraints apply unchanged: the original hosted ref_price
    # and configured slippage tolerance are honored against this fresh quote.
    try:
        args, required = await venue.prepare(route, intent)
    except SdkError as exc:
        store.set_deferred_exit_status(deferred["id"], "blocked", exc.message)
        return True
    qty = store.decimal(args["quantity"])
    if qty <= 0 or qty > held or qty > await venue.sellable(route, symbol):
        store.set_deferred_exit_status(
            deferred["id"], "blocked", "Current confirmed or sellable quantity is below the preserved hosted exit."
        )
        return True
    if required > store.decimal(route["max_order_usd"]):
        store.set_deferred_exit_status(deferred["id"], "blocked", "Preserved exit exceeds the configured order cap.")
        return True
    if (
        strategy_service._get_row(strategy_id)["status"] != "live"
        or not portfolio_risk_service.check_before_live_execution()["allowed"]
    ):
        store.set_deferred_exit_status(deferred["id"], "blocked", "Strategy stopped before exit reservation.")
        return True
    ref_id = str(uuid.uuid4())
    if not store.reserve_deferred_exit(strategy_id, deferred, intent, args, ref_id):
        store.set_deferred_exit_status(
            deferred["id"], "blocked", "Exit reservation lost a concurrent order or position-state race."
        )
        return True
    try:
        response = await venue.submit(route, args, ref_id)
        if response.get("order"):
            store.apply_snapshot(ref_id, response["order"], asset_class=route["asset_class"])
        elif response.get("approval"):
            store.mark(ref_id, "awaiting_approval", approval_id=response["approval"]["approval_id"])
        else:
            store.mark(ref_id, "uncertain", error="UNRECOGNIZED_SUBMISSION_RESPONSE")
    except BaseException:
        store.mark(ref_id, "uncertain", error="SUBMISSION_OUTCOME_UNKNOWN")
        raise
    return True


async def _cancel_if_freshly_active(venue, order):
    """Cancel only when a persisted broker observation still proves it open."""
    if not order.get("broker_order_id"):
        return False
    observation = store.latest_order_observation(order["id"])
    if not observation:
        return False
    if order.get("cancel_requested_at") and observation["observed_at"] <= order["cancel_requested_at"]:
        return False
    state = observation["snapshot"].get("state")
    if state not in {"new", "queued", "unconfirmed", "confirmed", "partially_filled", "pending_cancelled"}:
        return False
    await cancel(venue, order["id"])
    return True


async def _native_protection_maintenance(venue):
    """Reconcile confirmed fills into protection without evaluating a candle."""
    from src.services import portfolio_risk_service, strategy_service

    for strategy_id in store.native_protection_strategies():
        route = store.route(strategy_id)
        if not route or strategy_service._get_row(strategy_id)["status"] != "live":
            continue
        if not portfolio_risk_service.check_before_live_execution()["allowed"]:
            continue
        if await _process_deferred_exit(venue, strategy_id, route):
            continue
        symbol = strategy_service._get_row(strategy_id)["asset"].removesuffix("-USD")
        quote = await venue.quote(route, symbol)
        mark = float(store.decimal(quote["bid_price"]))
        _, positions = store.execution_inputs(strategy_id, mark)
        templates = positions
        evaluations = trade_log.list_evaluations(strategy_id, limit=1)
        if not evaluations:
            continue
        await _sync_native_protection(
            venue, strategy_id, route, evaluations[0].id, templates, positions
        )


async def _scheduled_reconcile_and_maintain(venue):
    await reconcile(venue)
    await _native_protection_maintenance(venue)


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
    if route.get("native_stop_loss_enabled") and confirmed_v2:
        if await _process_deferred_exit(venue, strategy_id, route):
            return
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
    eid = str(uuid.uuid4())
    evaluation_started = time.monotonic()
    evaluation_context = {
        "strategy_id": strategy_id,
        "evaluation_id": eid,
        "execution_venue": "robinhood",
        "base_token": payload.get("base_token"),
        "quote_token": payload.get("quote_token"),
        "market_data_venue": payload.get("market_data_venue"),
        "timeframe": row["timeframe"],
        "external_execution_mode": "confirmed_v2" if confirmed_v2 else "legacy",
    }
    _log.info("broker.evaluation.started", **evaluation_context)
    # All strategy logic, research and sizing remain in Mangrove.
    try:
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
    except Exception:
        # Upstream exception text can contain request/account data.
        _log.warning(
            "broker.evaluation.failed", **evaluation_context,
            duration_ms=int((time.monotonic() - evaluation_started) * 1000),
        )
        raise
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
    intents = strategy_service._extract_order_intents(result, strategy_id=strategy_id, tick_id=eid)
    if confirmed_v2 and dump.get("external_execution_disposition") == "duplicate_bar" and (
        intents or any(o.get("status") == "filled" for o in dump.get("new_orders") or [])
    ):
        raise SdkError("Mangrove marked a bar duplicate while returning a new order intent; execution stopped.")
    blocked = bool(pending and intents)
    duration_ms = int((time.monotonic() - evaluation_started) * 1000)
    trade_log.log_evaluation(
        Evaluation(
            id=eid,
            strategy_id=strategy_id,
            timestamp=trade_log.now_utc(),
            duration_ms=duration_ms,
            status="skipped" if blocked else "ok",
            error_msg="Execution deferred until pending broker orders reconcile." if blocked else None,
            sdk_response=dump,
            order_intents=intents,
            market_snapshot=strategy_service._market_snapshot(dump, asset=row["asset"]),
        )
    )
    _log.info(
        "broker.evaluation.completed", **evaluation_context,
        response_market_data_venue=dump.get("market_data_venue"),
        duration_ms=duration_ms, order_count=len(intents),
        execution_deferred=blocked,
    )
    def commit_ack(reservation=None, deferred_exit=None):
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
            deferred_exit=deferred_exit,
        )
    if confirmed_v2 and dump.get("external_execution_disposition") == "duplicate_bar":
        commit_ack()
        if route.get("native_stop_loss_enabled"):
            await _sync_native_protection(
                venue, strategy_id, route, eid,
                dump.get("position_templates") or dump.get("open_positions"), positions,
            )
        return
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
        cancel_blocker = next((
            order for order in pending
            if order["order_kind"] == "protective_stop"
            or json.loads(order["intent_json"]).get("side") == "buy"
        ), None)
        if cancel_blocker is None and route.get("native_stop_loss_enabled") and confirmed_v2 and pending:
            # Preserve a newer hosted exit behind any same-strategy sell too.
            # Let that sell reach terminal state; then subtract its actual
            # same-position fills from the new authorization before reserving.
            cancel_blocker = pending[0]
        if (
            needs_exit and cancel_blocker and confirmed_v2 and route.get("native_stop_loss_enabled")
            and strategy_service._get_row(strategy_id)["status"] == "live"
        ):
            exit_intents = [
                item for item in intents
                if item.action == "exit" and item.side == "sell" and item.symbol == symbol
                and any(position["id"] == item.engine_position_id for position in positions)
            ]
            if len(exit_intents) != 1:
                raise SdkError("Cannot safely preserve multiple hosted exits behind a pending broker order.")
            exit_intent = exit_intents[0]
            templates = dump.get("position_templates") or []
            template = next((p for p in templates if p.get("id") == exit_intent.engine_position_id), None)
            position = next(p for p in positions if p["id"] == exit_intent.engine_position_id)
            if not template:
                raise SdkError("Hosted position template is required to preserve a blocked exit.")
            deferred_exit = {
                "id": str(uuid.uuid4()),
                "position_id": exit_intent.engine_position_id,
                "evaluation_id": eid,
                "engine_order_id": exit_intent.engine_order_id,
                "intent_json": exit_intent.model_dump_json(),
                "position_template": template,
                "decision_quantity": position["position_size"],
                "blocker_order_id": cancel_blocker["id"],
                "blocker_filled_at_decision": cancel_blocker["filled_quantity"],
                "route_fingerprint": store.route_fingerprint(strategy_id),
            }
            if commit_ack(deferred_exit=deferred_exit) is not True:
                raise SdkError("Hosted exit deferral was not committed; broker cancellation stopped.")
            blocker_side = json.loads(cancel_blocker["intent_json"]).get("side")
            should_cancel_blocker = cancel_blocker["order_kind"] == "protective_stop" or blocker_side == "buy"
            if should_cancel_blocker and cancel_blocker["broker_order_id"] and cancel_blocker["status"] != "cancel_pending":
                await cancel(venue, cancel_blocker["id"])
                if not store.orders(strategy_id, pending=True):
                    await _process_deferred_exit(venue, strategy_id, route)
            return
        commit_ack()
        if needs_exit and strategy_service._get_row(strategy_id)["status"] == "live":
            for order in pending:
                if json.loads(order["intent_json"]).get("side") == "buy" and order["broker_order_id"] and order["status"] != "cancel_pending":
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
        if route.get("native_stop_loss_enabled") and confirmed_v2:
            await _sync_native_protection(
                venue, strategy_id, route, eid, dump.get("position_templates") or dump.get("open_positions"), positions
            )
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
            if intent.side == "buy" and confirmed_v2 and route.get("native_stop_loss_enabled"):
                _, confirmed_positions = store.execution_inputs(strategy_id, mark)
                protection_templates = list(dump.get("position_templates") or [])
                if not any(p.get("id") == template.get("id") for p in protection_templates):
                    protection_templates.append(template)
                await _sync_native_protection(
                    venue, strategy_id, route, eid, protection_templates, confirmed_positions
                )
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
    if not store.orders(pending=True) and not store.native_protection_strategies():
        return
    try:
        run(_scheduled_reconcile_and_maintain)
    except Exception:
        _log.warning("broker.reconciliation.unavailable")
