"""Transactional broker journal and fill-derived strategy state.

Amounts stay decimal strings in the journal. Updates to journal, trades, and
positions commit together; repeated cumulative fill snapshots are harmless.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal

from src.config import app_config
from src.models.domain import OrderIntent
from src.shared.errors import SdkError

PENDING = ("submitting", "uncertain", "open", "partial", "awaiting_approval", "cancel_pending")


def now():
    return datetime.now(timezone.utc).isoformat()


def decimal(value):
    try:
        result = Decimal(str(value))
        if not result.is_finite():
            raise ValueError
        return result
    except Exception:
        raise SdkError("Broker returned an invalid numeric value.") from None


def _exact_decimal_text(value):
    amount = decimal(value)
    if amount == 0:
        return "0"
    text = format(amount, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _snapshot_journal(snapshot):
    """Serialize JSON broker evidence without changing optional metadata.

    ``created_at``/``observed_at`` on broker_events records when this process
    reconciled the snapshot. Any Robinhood executions retain their own original
    UTC timestamps inside snapshot_json.
    """
    if not isinstance(snapshot, dict):
        raise SdkError("Robinhood returned an invalid order snapshot.")
    try:
        encoded = json.dumps(snapshot, sort_keys=True, separators=(",", ":"), allow_nan=False)
        receipt_data = {
            key: value for key, value in snapshot.items()
            if key not in {"state", "derived_state", "state_group", "updated_at"}
        }
        receipt_encoded = json.dumps(receipt_data, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError):
        raise SdkError("Robinhood returned a non-JSON order snapshot.") from None
    key = hashlib.sha256(receipt_encoded.encode("utf-8")).hexdigest()
    return encoded, key


@contextmanager
def connection():
    # Independent connections prevent the scheduler's transactions from sharing
    # the legacy global SQLite connection with unrelated HTTP requests.
    if str(app_config.DB_PATH) == ":memory:":
        from src.shared.db.sqlite import get_connection

        with get_connection() as conn:
            yield conn
        return
    conn = sqlite3.connect(str(app_config.DB_PATH), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def route(strategy_id):
    with connection() as conn:
        row = conn.execute("SELECT * FROM broker_routes WHERE strategy_id=?", (strategy_id,)).fetchone()
        return dict(row) if row else None


def native_protection_strategies():
    with connection() as conn:
        rows = conn.execute(
            """SELECT r.strategy_id FROM broker_routes r JOIN strategies s ON s.id=r.strategy_id
               WHERE r.native_stop_loss_enabled=1 AND r.external_execution_mode='confirmed_v2'
               AND r.asset_class='crypto' AND s.status='live' ORDER BY r.strategy_id"""
        ).fetchall()
        return [row["strategy_id"] for row in rows]


def orders(strategy_id=None, *, pending=False, order_kind=None):
    sql, values = "SELECT * FROM broker_orders WHERE 1=1", []
    if strategy_id:
        sql += " AND strategy_id=?"
        values.append(strategy_id)
    if pending:
        sql += " AND status IN (" + ",".join("?" for _ in PENDING) + ")"
        values.extend(PENDING)
    if order_kind:
        sql += " AND order_kind=?"
        values.append(order_kind)
    with connection() as conn:
        return [dict(r) for r in conn.execute(sql + " ORDER BY created_at", values).fetchall()]


def _reserve_in_transaction(conn, strategy_id, evaluation_id, intent, request, template, ref_id,
                           deferred_exit_id=None):
    key = intent.engine_order_id or f"{evaluation_id}:{intent.engine_position_id}:{intent.action}"
    if intent.side == "sell":
        key += ":" + str(intent.amount)
    active = conn.execute("SELECT status FROM strategies WHERE id=?", (strategy_id,)).fetchone()
    if not active or active["status"] != "live":
        return False
    existing = conn.execute(
        "SELECT id FROM broker_orders WHERE strategy_id=? AND intent_key=?", (strategy_id, key)
    ).fetchone()
    if existing:
        return False
    # The unique pending index serializes submissions across processes too.
    conn.execute(
        """INSERT INTO broker_orders
        (id,strategy_id,evaluation_id,intent_key,intent_json,position_template_json,
         request_json,status,created_at,updated_at) VALUES (?,?,?,?,?,?,?,'submitting',?,?)""",
        (
            ref_id,
            strategy_id,
            evaluation_id,
            key,
            intent.model_dump_json(),
            json.dumps(template),
            json.dumps(request),
            now(),
            now(),
        ),
    )
    if deferred_exit_id:
        changed = conn.execute(
            """UPDATE broker_deferred_exits SET status='submitted',detail=NULL,reserved_order_id=?,updated_at=?
            WHERE id=? AND strategy_id=? AND status IN ('waiting_stop_terminal','blocked')""",
            (ref_id, now(), deferred_exit_id, strategy_id),
        ).rowcount
        if changed != 1:
            raise SdkError("Deferred exit changed before its broker reservation committed.")
    return True


def reserve(strategy_id, evaluation_id, intent, request, template, ref_id):
    with connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        return _reserve_in_transaction(conn, strategy_id, evaluation_id, intent, request, template, ref_id)


def reserve_deferred_exit(strategy_id, deferred, intent, request, ref_id):
    """Atomically reserve a preserved hosted exit and retire its deferral."""
    with connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            """SELECT d.*,p.status AS position_status,h.quantity AS held_quantity
            FROM broker_deferred_exits d
            JOIN positions p ON p.id=d.position_id
            JOIN broker_holdings h ON h.position_id=d.position_id
            WHERE d.id=? AND d.strategy_id=?""",
            (deferred["id"], strategy_id),
        ).fetchone()
        risk = conn.execute("SELECT tripped FROM portfolio_risk WHERE id=1").fetchone()
        if (
            not row or row["status"] not in {"waiting_stop_terminal", "blocked"}
            or row["position_status"] != "open"
            or not risk or risk["tripped"]
            or _route_fingerprint_in_conn(conn, strategy_id) != row["route_fingerprint"]
            or decimal(intent.amount) <= 0 or decimal(intent.amount) > decimal(row["held_quantity"])
        ):
            return False
        original = OrderIntent.model_validate_json(row["intent_json"])
        if (
            intent.side != "sell" or intent.action != "exit"
            or intent.engine_position_id != original.engine_position_id
            or intent.symbol != original.symbol or intent.engine_order_id != original.engine_order_id
            or intent.reason != original.reason
            or decimal(request.get("quantity", "-1")) != decimal(intent.amount)
            or request.get("side") != "sell"
        ):
            return False
        authorized = min(decimal(original.amount), decimal(row["decision_quantity"]))
        blocker_delta = Decimal(0)
        if row["blocker_order_id"]:
            blocker = conn.execute(
                "SELECT order_kind,protected_position_id,intent_json,filled_quantity FROM broker_orders WHERE id=? AND strategy_id=?",
                (row["blocker_order_id"], strategy_id),
            ).fetchone()
            blocker_intent = json.loads(blocker["intent_json"]) if blocker else {}
            same_position_reduction = (
                blocker and (
                    blocker["order_kind"] == "protective_stop"
                    and blocker["protected_position_id"] == row["position_id"]
                    or blocker["order_kind"] == "strategy"
                    and blocker_intent.get("side") == "sell"
                    and blocker_intent.get("engine_position_id") == row["position_id"]
                )
            )
            if same_position_reduction:
                blocker_delta = decimal(blocker["filled_quantity"]) - decimal(row["blocker_filled_at_decision"])
                if blocker_delta < 0:
                    return False
        expected_amount = min(decimal(row["held_quantity"]), max(Decimal(0), authorized - blocker_delta))
        if decimal(intent.amount) != expected_amount:
            return False
        return _reserve_in_transaction(
            conn, strategy_id, deferred["evaluation_id"], intent, request,
            json.loads(row["position_template_json"]), ref_id, deferred_exit_id=deferred["id"],
        )


def reserve_protective_stop(strategy_id, evaluation_id, position_id, asset, quantity, stop_price,
                            routed_stop_price, request, template, ref_id):
    """Reserve a hosted-template crypto stop under the strategy order lock.

    The held quantity is rechecked in the same transaction as reservation. A
    stop never reserves more than the exact broker-confirmed position, and it
    cannot overlap an entry or exit submission.
    """
    quantity, stop_price, routed_stop_price = map(decimal, (quantity, stop_price, routed_stop_price))
    if quantity <= 0 or stop_price <= 0 or routed_stop_price <= 0:
        raise SdkError("Protective stop requires positive confirmed quantity and hosted stop price.")
    intent = OrderIntent(
        action="exit", side="sell", symbol=asset.removesuffix("-USD"), amount=float(quantity),
        reason="hosted native stop-loss", ref_price=float(routed_stop_price), engine_position_id=position_id,
    )
    with connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        route = conn.execute(
            "SELECT asset_class,native_stop_loss_enabled,external_execution_mode FROM broker_routes WHERE strategy_id=?",
            (strategy_id,),
        ).fetchone()
        strategy = conn.execute("SELECT status,asset FROM strategies WHERE id=?", (strategy_id,)).fetchone()
        risk = conn.execute("SELECT tripped FROM portfolio_risk WHERE id=1").fetchone()
        holding = conn.execute(
            "SELECT quantity FROM broker_holdings WHERE strategy_id=? AND position_id=?",
            (strategy_id, position_id),
        ).fetchone()
        if (
            not route or route["asset_class"] != "crypto" or not route["native_stop_loss_enabled"]
            or route["external_execution_mode"] != "confirmed_v2"
            or not risk or risk["tripped"]
            or not strategy or strategy["status"] != "live" or strategy["asset"] != asset
            or not holding or decimal(holding["quantity"]) != quantity
        ):
            return False
        pending = conn.execute(
            "SELECT 1 FROM broker_orders WHERE strategy_id=? AND status IN (" +
            ",".join("?" for _ in PENDING) + ") LIMIT 1",
            (strategy_id, *PENDING),
        ).fetchone()
        if pending:
            return False
        intent_key = f"native-stop:{position_id}:{ref_id}"
        conn.execute(
            """INSERT INTO broker_orders
            (id,strategy_id,evaluation_id,intent_key,intent_json,position_template_json,request_json,
             status,created_at,updated_at,order_kind,protected_position_id,hosted_stop_price,routed_stop_price)
            VALUES (?,?,?,?,?,?,?,'submitting',?,?,'protective_stop',?,?,?)""",
            (ref_id, strategy_id, evaluation_id, intent_key, intent.model_dump_json(), json.dumps(template),
             json.dumps(request), now(), now(), position_id, _exact_decimal_text(stop_price),
             _exact_decimal_text(routed_stop_price)),
        )
        conn.execute(
            """INSERT INTO broker_protection_state
            (strategy_id,position_id,desired_stop_price,routed_stop_price,desired_quantity,status,detail,active_order_id,updated_at)
            VALUES (?,?,?,?,?,'submitting',NULL,?,?)
            ON CONFLICT(strategy_id,position_id) DO UPDATE SET
            desired_stop_price=excluded.desired_stop_price,desired_quantity=excluded.desired_quantity,
            routed_stop_price=excluded.routed_stop_price,
            status='submitting',detail=NULL,active_order_id=excluded.active_order_id,updated_at=excluded.updated_at""",
            (strategy_id, position_id, _exact_decimal_text(stop_price), _exact_decimal_text(routed_stop_price),
             _exact_decimal_text(quantity), ref_id, now()),
        )
    return True


def set_protection_state(strategy_id, position_id, *, stop_price=None, routed_stop_price=None, quantity="0", status, detail=None,
                         active_order_id=None):
    with connection() as conn:
        conn.execute(
            """INSERT INTO broker_protection_state
            (strategy_id,position_id,desired_stop_price,routed_stop_price,desired_quantity,status,detail,active_order_id,updated_at)
            VALUES (?,?,?,?,?,?,?,?,?)
            ON CONFLICT(strategy_id,position_id) DO UPDATE SET
            desired_stop_price=excluded.desired_stop_price,desired_quantity=excluded.desired_quantity,
            routed_stop_price=COALESCE(excluded.routed_stop_price,broker_protection_state.routed_stop_price),
            status=excluded.status,detail=excluded.detail,
            active_order_id=COALESCE(excluded.active_order_id,broker_protection_state.active_order_id),
            updated_at=excluded.updated_at""",
            (strategy_id, position_id, None if stop_price is None else _exact_decimal_text(stop_price),
             None if routed_stop_price is None else _exact_decimal_text(routed_stop_price),
             _exact_decimal_text(quantity), status, detail, active_order_id, now()),
        )


def protection_states(strategy_id):
    with connection() as conn:
        return [dict(row) for row in conn.execute(
            "SELECT * FROM broker_protection_state WHERE strategy_id=? ORDER BY position_id", (strategy_id,)
        ).fetchall()]


def route_fingerprint(strategy_id):
    with connection() as conn:
        route = conn.execute("SELECT * FROM broker_routes WHERE strategy_id=?", (strategy_id,)).fetchone()
        strategy = conn.execute("SELECT asset FROM strategies WHERE id=?", (strategy_id,)).fetchone()
        if not route or not strategy:
            return None
        return _route_fingerprint_in_conn(conn, strategy_id)


def _route_fingerprint_in_conn(conn, strategy_id):
    route = conn.execute("SELECT * FROM broker_routes WHERE strategy_id=?", (strategy_id,)).fetchone()
    strategy = conn.execute("SELECT asset FROM strategies WHERE id=?", (strategy_id,)).fetchone()
    if not route or not strategy:
        return None
    fields = (
        "venue", "account_number", "rhs_account_number", "asset_class", "capital",
        "max_order_usd", "slippage_pct", "external_execution_mode", "native_stop_loss_enabled",
    )
    encoded = json.dumps({**{key: route[key] for key in fields}, "strategy_asset": strategy["asset"]},
                         sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def deferred_exits(strategy_id, *, pending_only=False):
    sql = "SELECT * FROM broker_deferred_exits WHERE strategy_id=?"
    values = [strategy_id]
    if pending_only:
        sql += " AND status IN ('waiting_stop_terminal','blocked')"
    with connection() as conn:
        return [dict(row) for row in conn.execute(sql + " ORDER BY created_at", values).fetchall()]


def latest_order_snapshot(order_id):
    with connection() as conn:
        row = conn.execute(
            "SELECT snapshot_json FROM broker_events WHERE order_id=? ORDER BY observed_at DESC,id DESC LIMIT 1",
            (order_id,),
        ).fetchone()
        return json.loads(row["snapshot_json"]) if row else None


def latest_order_observation(order_id):
    with connection() as conn:
        row = conn.execute(
            """SELECT snapshot_json,observed_at FROM broker_events
               WHERE order_id=? ORDER BY observed_at DESC,id DESC LIMIT 1""",
            (order_id,),
        ).fetchone()
    return {"snapshot": json.loads(row["snapshot_json"]), "observed_at": row["observed_at"]} if row else None


def protective_snapshot_quantity(order_id):
    """Return broker-accepted quantity, or None when the snapshot omits it."""
    latest = latest_order_snapshot(order_id)
    if not latest or latest.get("quantity") is None:
        return None
    try:
        return decimal(latest["quantity"])
    except SdkError:
        return None


def protective_snapshot_quantity_matches(order_id):
    """Whether the latest broker observation proves the requested stop size."""
    with connection() as conn:
        row = conn.execute("SELECT request_json FROM broker_orders WHERE id=?", (order_id,)).fetchone()
    if not row:
        return False
    accepted = protective_snapshot_quantity(order_id)
    try:
        return accepted is not None and accepted == decimal(json.loads(row["request_json"])["quantity"])
    except (KeyError, TypeError, ValueError, SdkError):
        return False


def confirmed_position_quantity(strategy_id, position_id):
    with connection() as conn:
        row = conn.execute(
            "SELECT quantity FROM broker_holdings WHERE strategy_id=? AND position_id=?",
            (strategy_id, position_id),
        ).fetchone()
        return decimal(row["quantity"]) if row else Decimal(0)


def set_deferred_exit_status(exit_id, status, detail=None):
    with connection() as conn:
        conn.execute(
            "UPDATE broker_deferred_exits SET status=?,detail=?,updated_at=? WHERE id=?",
            (status, detail, now(), exit_id),
        )


def mark(order_id, status, *, broker_id=None, approval_id=None, error=None):
    with connection() as conn:
        conn.execute(
            """UPDATE broker_orders SET status=?,broker_order_id=COALESCE(?,broker_order_id),
            approval_id=COALESCE(?,approval_id),error_code=?,updated_at=?,
            cancel_requested_at=CASE WHEN ?='cancel_pending' THEN ? ELSE cancel_requested_at END
            WHERE id=?""",
            (status, broker_id, approval_id, error, now(), status, now(), order_id),
        )


def _preserve_risk_halt(candidate, current):
    """Automatic state writes cannot clear a latched engine/broker risk halt.

    A delayed first fill carries the evaluation from order submission, which
    can predate a halt recorded by a later tick. Preserve the original cause
    and measurements; this is persistence of the existing gate, not a new risk
    calculation. Legacy top-level halts and flag-only latches also survive.
    """
    state = dict(candidate)
    risk = dict(state.get("risk_state") or {})
    previous_risk = current.get("risk_state") or {}
    halt = previous_risk.get("halt") or current.get("halt") or risk.get("halt") or state.get("halt")
    if halt or previous_risk.get("hard_floor_tripped") or risk.get("hard_floor_tripped"):
        risk["hard_floor_tripped"] = True
        if halt:
            risk["halt"] = halt
            state["halt"] = halt
        state["risk_state"] = risk
    return state


def save_evaluation_state(strategy_id, state, positions):
    """Persist a no-intent evaluation without clearing a committed risk halt."""
    with connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT execution_state_json FROM strategies WHERE id=?", (strategy_id,)).fetchone()
        current = json.loads(row[0] or "{}")
        conn.execute(
            "UPDATE strategies SET execution_state_json=?,open_positions_json=? WHERE id=?",
            (json.dumps(_preserve_risk_halt(state, current)), json.dumps(positions), strategy_id),
        )


def apply_snapshot(order_id, snapshot, *, asset_class):
    """Persist a cumulative fill once, including partial-then-cancel outcomes."""
    snapshot_json, snapshot_key = _snapshot_journal(snapshot)
    states = {
        "filled": "filled",
        "cancelled": "canceled",
        "canceled": "canceled",
        "partially_filled_rest_cancelled": "canceled",
        "rejected": "rejected",
        "failed": "rejected",
        "voided": "canceled",
        "pending_cancelled": "cancel_pending",
        "queued": "open",
        "unconfirmed": "open",
        "confirmed": "open",
        "new": "open",
        "partially_filled": "partial",
    }
    state = states.get(snapshot.get("state"))
    if state is None:
        raise SdkError("Unknown Robinhood order state; reconciliation required.")
    quantity = decimal(snapshot["cumulative_quantity"])
    if quantity < 0:
        raise SdkError("Negative Robinhood fill quantity.")
    price = decimal(snapshot["average_price"]) if quantity else Decimal(0)
    if quantity and price <= 0:
        raise SdkError("Missing Robinhood fill price.")
    gross = quantity * price
    if asset_class == "crypto" and quantity:
        # Net cash includes fees and withholding; missing cash data must not be
        # treated as zero fees or replaced by an estimated preview amount.
        if snapshot.get("rounded_executed_notional") is not None:
            gross = decimal(snapshot["rounded_executed_notional"])
        net = decimal(snapshot["net_rounded_executed_notional"])
        fees = net - gross if snapshot["side"] == "buy" else gross - net
        # The net value is rounded by the broker to cents.
        fees = max(Decimal(0), fees)
    elif asset_class == "crypto":
        fees = Decimal(0)
    else:
        fees = decimal(snapshot.get("fees", "0"))
    if fees < 0:
        raise SdkError("Invalid Robinhood fees.")
    with connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM broker_orders WHERE id=?", (order_id,)).fetchone()
        if row is None:
            raise SdkError("Unknown local broker order.")
        intent = OrderIntent.model_validate_json(row["intent_json"])
        expected = json.loads(row["request_json"])
        if row["order_kind"] == "protective_stop":
            if (
                snapshot.get("type") != "stop_loss"
                or snapshot.get("side") != "sell"
                or decimal(snapshot.get("stop_price")) != decimal(row["routed_stop_price"])
            ):
                raise SdkError("Robinhood protective order type or stop price does not match the hosted instruction.")
        if (
            snapshot.get("side") != intent.side
            or snapshot.get("symbol", snapshot.get("currency_code", intent.symbol)).removesuffix("-USD")
            != intent.symbol
            or snapshot.get("ref_id") not in (None, "", row["id"], row["approval_id"])
            or (row["broker_order_id"] and snapshot["id"] != row["broker_order_id"])
        ):
            raise SdkError("Robinhood order identity mismatch.")
        if quantity > decimal(expected["quantity"]):
            raise SdkError("Robinhood filled more than the requested quantity.")
        old_qty, old_gross, old_fees = map(decimal, (row["filled_quantity"], row["filled_notional"], row["fees"]))
        if quantity < old_qty or gross < old_gross or fees < old_fees:
            raise SdkError("Robinhood returned a regressing fill snapshot.")
        if row["status"] not in PENDING and (quantity != old_qty or state != row["status"]):
            raise SdkError("Terminal Robinhood order changed; reconcile manually.")
        if state == "filled" and quantity <= 0:
            raise SdkError("Filled order has no filled quantity.")
        if row["status"] == "cancel_pending" and state in PENDING:
            # An open/partial snapshot is not a cancellation failure. Keep the
            # outstanding cancellation visible until a terminal broker outcome.
            state = "cancel_pending"
        delta, delta_gross, delta_fees = quantity - old_qty, gross - old_gross, fees - old_fees
        timestamp = now()
        if delta:
            fill_id = f"{order_id}:{quantity}"
            delta_price = delta_gross / delta
            if delta_price <= 0:
                raise SdkError("Invalid incremental fill price.")
            buy = intent.side == "buy"
            position_id = intent.engine_position_id or order_id
            pos = conn.execute("SELECT * FROM positions WHERE id=?", (position_id,)).fetchone()
            if pos and pos["strategy_id"] != row["strategy_id"]:
                raise SdkError("Position identity belongs to another strategy.")
            holding = conn.execute("SELECT * FROM broker_holdings WHERE position_id=?", (position_id,)).fetchone()
            if pos and not holding:
                raise SdkError("Broker position has no exact holding record.")
            held_qty = decimal(holding["quantity"]) if holding else Decimal(0)
            held_cost = decimal(holding["unit_cost"]) if holding else Decimal(0)
            pnl = None
            evaluation = conn.execute(
                "SELECT sdk_response_json FROM evaluations WHERE id=?", (row["evaluation_id"],)
            ).fetchone()
            proposed = json.loads(evaluation[0]).get("execution_state") or {}
            saved = conn.execute(
                "SELECT execution_state_json FROM strategies WHERE id=?", (row["strategy_id"],)
            ).fetchone()
            current_state = json.loads(saved[0] or "{}")
            execution_mode = conn.execute(
                "SELECT external_execution_mode FROM broker_routes WHERE strategy_id=?",
                (row["strategy_id"],),
            ).fetchone()[0]
            next_state = (
                deepcopy(current_state)
                if execution_mode == "confirmed_v2" or old_qty != 0
                else proposed
            )
            if not buy:
                if not pos or pos["status"] != "open" or held_qty < delta:
                    raise SdkError("Exit fill exceeds this strategy's confirmed holding.")
                exact_pnl = delta_gross - delta * held_cost - delta_fees
                pnl = float(exact_pnl)
                # Mangrove's risk history is retained conservatively. If a modeled
                # non-loss becomes a real loss, stop new entries until that risk
                # history can be reconciled; never silently erase a loss gate.
                if execution_mode != "confirmed_v2" and pnl < 0 and decimal(intent.ref_price) >= held_cost:
                    risk = next_state.setdefault("risk_state", {})
                    risk.update(hard_floor_tripped=True, halt={"reason": "broker_loss_diverged_from_model"})
                    next_state["halt"] = risk["halt"]
            conn.execute(
                """INSERT INTO trades (id,strategy_id,evaluation_id,order_intent_json,mode,input_token,
                input_amount,output_token,output_amount,fill_price,fees_json,status,executed_at,confirmed_at,
                p_and_l,venue,venue_order_ref,venue_trade_ref) VALUES (?,?,?,?,'live',?,?,?,?,?,?,'confirmed',?,?,?,?,?,?)""",
                (
                    fill_id,
                    row["strategy_id"],
                    row["evaluation_id"],
                    row["intent_json"],
                    "USD" if buy else intent.symbol,
                    float(delta_gross) if buy else float(delta),
                    intent.symbol if buy else "USD",
                    float(delta) if buy else float(delta_gross),
                    float(delta_price),
                    json.dumps({"broker": str(delta_fees)}),
                    timestamp,
                    timestamp,
                    pnl,
                    "robinhood",
                    snapshot["id"],
                    fill_id,
                ),
            )
            if buy:
                if pos and pos["status"] != "open":
                    raise SdkError("Entry fill targets a closed position.")
                prev_qty = held_qty
                prev_cost = prev_qty * held_cost
                total = prev_qty + delta
                # Entry fees are part of cost basis; price used by the engine is
                # actual broker fill price, with risk accounting using net cost.
                avg = (prev_cost + delta_gross + delta_fees) / total
                conn.execute(
                    """INSERT INTO positions(id,strategy_id,asset,entry_trade_id,entry_price,entry_amount,
                    entry_time,status,stop_loss,take_profit) VALUES (?,?,?,?,?,?,?,'open',?,?)
                    ON CONFLICT(id) DO UPDATE SET entry_price=excluded.entry_price,entry_amount=excluded.entry_amount""",
                    (
                        position_id,
                        row["strategy_id"],
                        intent.symbol,
                        fill_id,
                        float(avg),
                        float(total),
                        timestamp,
                        intent.stop_loss,
                        intent.take_profit,
                    ),
                )
                opened = conn.execute(
                    "SELECT 1 FROM broker_execution_events WHERE strategy_id=? AND position_id=? AND kind='opened'",
                    (row["strategy_id"], position_id),
                ).fetchone()
                if execution_mode == "confirmed_v2" and not opened:
                    sequence = conn.execute(
                        "SELECT COALESCE(MAX(sequence),0)+1 FROM broker_execution_events WHERE strategy_id=?",
                        (row["strategy_id"],),
                    ).fetchone()[0]
                    strategy_asset = conn.execute(
                        "SELECT asset FROM strategies WHERE id=?", (row["strategy_id"],)
                    ).fetchone()[0]
                    conn.execute(
                        """INSERT INTO broker_execution_events
                        (sequence,event_id,source_order_id,strategy_id,position_id,asset,kind,
                         filled_quantity,occurred_at,observed_at)
                        VALUES (?,?,?,?,?,?,'opened',?,NULL,?)""",
                        (sequence, f"open:{position_id}", order_id, row["strategy_id"], position_id,
                         strategy_asset, _exact_decimal_text(delta), timestamp),
                    )
            else:
                remaining = held_qty - delta
                if remaining == 0:
                    bought = sold = Decimal(0)
                    buy_outflow = sell_proceeds = Decimal(0)
                    all_fees = Decimal(0)
                    for fee_row in conn.execute(
                        """SELECT id,intent_json,filled_quantity,filled_notional,fees
                        FROM broker_orders WHERE strategy_id=?""",
                        (row["strategy_id"],),
                    ):
                        linked_intent = json.loads(fee_row["intent_json"])
                        linked_position = linked_intent.get("engine_position_id")
                        if linked_position != position_id and not (
                            not linked_position and fee_row["id"] == position_id
                        ):
                            continue
                        order_qty = quantity if fee_row["id"] == order_id else decimal(fee_row["filled_quantity"])
                        order_gross = gross if fee_row["id"] == order_id else decimal(fee_row["filled_notional"])
                        order_fee = fees if fee_row["id"] == order_id else decimal(fee_row["fees"])
                        all_fees += order_fee
                        if linked_intent["side"] == "buy":
                            bought += order_qty
                            buy_outflow += order_gross + order_fee
                        else:
                            sold += order_qty
                            sell_proceeds += order_gross - order_fee
                    if bought <= 0 or bought != sold:
                        raise SdkError("Cannot confirm a flat close without complete broker fill provenance.")
                    realized_pnl = sell_proceeds - buy_outflow
                    strategy_asset = conn.execute(
                        "SELECT asset FROM strategies WHERE id=?", (row["strategy_id"],)
                    ).fetchone()["asset"]
                    next_sequence = conn.execute(
                        """SELECT COALESCE(MAX(sequence),0)+1 AS next_sequence
                        FROM broker_execution_events WHERE strategy_id=?""",
                        (row["strategy_id"],),
                    ).fetchone()["next_sequence"]
                    # Emit only when this position is fully flat. The raw
                    # snapshot journal retains execution timestamps; a
                    # cumulative snapshot cannot always attribute a timestamp
                    # to a delta, so occurred_at stays null instead of being
                    # replaced with local observation time.
                    conn.execute(
                        """INSERT INTO broker_execution_events
                        (sequence,event_id,source_order_id,strategy_id,position_id,asset,kind,
                         realized_pnl,fees,occurred_at,observed_at)
                        VALUES (?,?,?,?,?,?,'closed',?,?,NULL,?)""",
                        (
                            next_sequence,
                            f"close:{position_id}",
                            order_id,
                            row["strategy_id"],
                            position_id,
                            strategy_asset,
                            _exact_decimal_text(realized_pnl),
                            _exact_decimal_text(all_fees),
                            timestamp,
                        ),
                    )
                conn.execute(
                    """UPDATE positions SET entry_amount=?,exit_trade_id=?,exit_price=?,exit_amount=?,
                    exit_time=?,status=? WHERE id=?""",
                    (
                        float(remaining),
                        fill_id,
                        float(delta_price),
                        float(delta),
                        timestamp,
                        "closed" if remaining == 0 else "open",
                        position_id,
                    ),
                )
            conn.execute(
                """INSERT INTO broker_holdings(position_id,strategy_id,quantity,unit_cost) VALUES (?,?,?,?)
                ON CONFLICT(position_id) DO UPDATE SET quantity=excluded.quantity,unit_cost=excluded.unit_cost""",
                (position_id, row["strategy_id"], str(total if buy else remaining), str(avg if buy else held_cost)),
            )
            conn.execute(
                "UPDATE strategies SET execution_state_json=? WHERE id=?",
                (json.dumps(_preserve_risk_halt(next_state, current_state)), row["strategy_id"]),
            )
        elif delta_gross or delta_fees:
            raise SdkError("Broker revised fill cash without a new fill; reconciliation required.")
        conn.execute(
            """UPDATE broker_orders SET broker_order_id=?,status=?,filled_quantity=?,filled_notional=?,
            fees=?,updated_at=?,error_code=NULL WHERE id=?""",
            (snapshot["id"], state, str(quantity), str(gross), str(fees), timestamp, order_id),
        )
        if state not in PENDING:
            conn.execute(
                """UPDATE broker_deferred_exits SET status='completed',detail=?,updated_at=?
                WHERE reserved_order_id=? AND status='submitted'""",
                (f"Preserved hosted exit reached terminal broker state: {state}.", timestamp, order_id),
            )
        conn.execute(
            """INSERT OR IGNORE INTO broker_events
            (order_id,status,filled_quantity,created_at,observed_at,snapshot_json,snapshot_key)
            VALUES (?,?,?,?,?,?,?)""",
            (order_id, state, str(quantity), timestamp, timestamp, snapshot_json, snapshot_key),
        )


def pending_confirmed_close_events(strategy_id):
    """Return durable, ordered sell-fill events awaiting state acknowledgement."""
    with connection() as conn:
        rows = conn.execute(
            """SELECT sequence,event_id,position_id,asset,realized_pnl,fees,occurred_at,observed_at
            FROM broker_execution_events
            WHERE strategy_id=? AND kind='closed' AND acknowledged_at IS NULL ORDER BY sequence""",
            (strategy_id,),
        ).fetchall()
        return [dict(row) for row in rows]


def pending_external_execution_events(strategy_id):
    """Return all unacknowledged broker-confirmed events for the v2 contract."""
    with connection() as conn:
        rows = conn.execute(
            """SELECT sequence,event_id,position_id,asset,kind,filled_quantity,
                      realized_pnl,fees,occurred_at
               FROM broker_execution_events WHERE strategy_id=? AND acknowledged_at IS NULL
               ORDER BY sequence LIMIT 512""",
            (strategy_id,),
        ).fetchall()
    events = []
    for row in rows:
        event = dict(row)
        if event["occurred_at"] is None:
            event.pop("occurred_at")
        if event["kind"] == "opened":
            event.pop("realized_pnl")
            event.pop("fees")
        else:
            event.pop("filled_quantity")
        events.append(event)
    return events


def _broker_state_revision(conn, strategy_id):
    """Hash the confirmed broker book for ack compare-and-swap."""
    route = conn.execute(
        """SELECT venue,account_number,rhs_account_number,asset_class,capital,max_order_usd,
                  slippage_pct,external_execution_mode
           FROM broker_routes WHERE strategy_id=?""",
        (strategy_id,),
    ).fetchone()
    strategy = conn.execute(
        """SELECT name,status,asset,timeframe,entry_json,exit_json,execution_config_json,
                  market_context_json,evaluation_lane
           FROM strategies WHERE id=?""",
        (strategy_id,),
    ).fetchone()
    snapshot = {
        "route": list(route) if route else None,
        "strategy_config": list(strategy) if strategy else None,
        "orders": [
            list(row)
            for row in conn.execute(
                """SELECT id,broker_order_id,status,filled_quantity,filled_notional,fees
                FROM broker_orders WHERE strategy_id=? ORDER BY id""",
                (strategy_id,),
            )
        ],
        "positions": [
            list(row)
            for row in conn.execute(
                """SELECT id,asset,entry_amount,entry_price,entry_time,status,exit_price,exit_amount,exit_time
                FROM positions WHERE strategy_id=? ORDER BY id""",
                (strategy_id,),
            )
        ],
        "holdings": [
            list(row)
            for row in conn.execute(
                """SELECT position_id,quantity,unit_cost FROM broker_holdings
                WHERE strategy_id=? ORDER BY position_id""",
                (strategy_id,),
            )
        ],
        "execution_events": [
            list(row)
            for row in conn.execute(
                """SELECT sequence,event_id,acknowledged_at,receipt_json FROM broker_execution_events
                WHERE strategy_id=? ORDER BY sequence""",
                (strategy_id,),
            )
        ],
    }
    encoded = json.dumps(snapshot, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def acknowledge_confirmed_close_events(
    strategy_id,
    event_ids,
    execution_state,
    *,
    open_positions,
    expected_state,
    expected_open_positions,
    expected_applied_through,
    expected_broker_revision,
    receipts=None,
    submitted_events=None,
    reservation=None,
    deferred_exit=None,
):
    """Atomically save the risk state produced by close events and acknowledge them.

    This is an internal persistence seam only; no public route currently sends
    close events to Mangrove or invokes this acknowledgement. The expected
    state, position payload, event cursor, and broker-book fingerprint form a
    compare-and-swap guard against a stale evaluation overwriting later fills
    or another acknowledgement.
    """
    if not isinstance(event_ids, (list, tuple)):
        raise SdkError("Invalid confirmed execution event identifiers.")
    if any(not isinstance(item, str) for item in event_ids) or len(set(event_ids)) != len(event_ids):
        raise SdkError("Invalid confirmed close event identifiers.")
    if not isinstance(execution_state, dict) or not isinstance(expected_state, dict):
        raise SdkError("Invalid execution state acknowledgement.")
    if not isinstance(open_positions, list) or not isinstance(expected_open_positions, list):
        raise SdkError("Invalid position state acknowledgement.")
    if (
        isinstance(expected_applied_through, bool)
        or not isinstance(expected_applied_through, int)
        or expected_applied_through < 0
    ):
        raise SdkError("Invalid expected confirmed close sequence.")
    if not isinstance(expected_broker_revision, str) or len(expected_broker_revision) != 64:
        raise SdkError("Invalid expected broker state revision.")
    try:
        json.dumps(execution_state, allow_nan=False)
        json.dumps(expected_state, allow_nan=False)
        json.dumps(open_positions, allow_nan=False)
        json.dumps(expected_open_positions, allow_nan=False)
    except (TypeError, ValueError):
        raise SdkError("Invalid state acknowledgement.") from None

    external = execution_state.get("external_execution") or {}
    response_cursor = external.get("applied_through") if isinstance(external, dict) else None
    expected_external = expected_state.get("external_execution") or {}
    expected_cursor_in_state = (
        expected_external.get("applied_through", 0) if isinstance(expected_external, dict) else 0
    )
    if expected_cursor_in_state != expected_applied_through:
        raise SdkError("Expected state cursor does not match the supplied cursor.")
    if type(response_cursor) is not int or response_cursor < 0:
        raise SdkError("Invalid confirmed execution response cursor.")
    if receipts is not None:
        if not isinstance(receipts, (list, tuple)) or not isinstance(submitted_events, (list, tuple)):
            raise SdkError("Invalid confirmed execution receipts.")
        if [item.get("event_id") for item in submitted_events] != list(event_ids):
            raise SdkError("Submitted events do not match the acknowledgement batch.")
        by_id = {item.get("event_id"): item for item in receipts if isinstance(item, dict)}
        if len(by_id) != len(receipts) or any(event_id not in by_id for event_id in event_ids):
            raise SdkError("Hosted acknowledgement is missing a receipt for a submitted event.")
        for event in submitted_events:
            receipt = by_id[event["event_id"]]
            expected_hash = _external_event_hash(event)
            if (
                receipt.get("sequence") != event.get("sequence")
                or receipt.get("position_id") != event.get("position_id")
                or receipt.get("kind") != event.get("kind")
                or receipt.get("sha256") != expected_hash
                or not isinstance(receipt.get("accounting_at"), str)
            ):
                raise SdkError("Hosted acknowledgement receipt does not match the submitted event.")

    with connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        rows = []
        if event_ids:
            rows = conn.execute(
                """SELECT event_id,sequence,position_id,asset,kind,filled_quantity,realized_pnl,fees,
                          occurred_at,acknowledged_at,receipt_json FROM broker_execution_events
                WHERE strategy_id=? AND event_id IN (""" + ",".join("?" for _ in event_ids) + ")",
                [strategy_id, *event_ids],
            ).fetchall()
        if {row["event_id"] for row in rows} != set(event_ids):
            raise SdkError("Confirmed execution acknowledgement contains an unknown event.")
        if receipts is not None:
            stored_by_id = {row["event_id"]: row for row in rows}
            for submitted in submitted_events:
                stored = stored_by_id[submitted["event_id"]]
                expected_event = {
                    "sequence": stored["sequence"],
                    "event_id": stored["event_id"],
                    "position_id": stored["position_id"],
                    "asset": stored["asset"],
                    "kind": stored["kind"],
                }
                if stored["occurred_at"] is not None:
                    expected_event["occurred_at"] = stored["occurred_at"]
                field = "filled_quantity" if stored["kind"] == "opened" else None
                if field:
                    expected_event[field] = stored[field]
                else:
                    expected_event.update(realized_pnl=stored["realized_pnl"], fees=stored["fees"])
                if _external_event_hash(expected_event) != _external_event_hash(submitted):
                    raise SdkError("Submitted execution event does not match broker-confirmed journal data.")
        current = conn.execute(
            "SELECT execution_state_json FROM strategies WHERE id=?", (strategy_id,)
        ).fetchone()
        if current is None:
            raise SdkError("Unknown strategy for confirmed close acknowledgement.")
        strategy_row = conn.execute(
            "SELECT execution_state_json,open_positions_json FROM strategies WHERE id=?",
            (strategy_id,),
        ).fetchone()
        current_state = json.loads(strategy_row["execution_state_json"] or "{}")
        current_positions = json.loads(strategy_row["open_positions_json"] or "[]")
        candidate_state = _preserve_risk_halt(execution_state, current_state)
        candidate_json = json.dumps(candidate_state, allow_nan=False)
        candidate_positions_json = json.dumps(open_positions, allow_nan=False)
        sequences = [row["sequence"] for row in rows]
        if event_ids and all(row["acknowledged_at"] is not None for row in rows):
            # Exact duplicate acknowledgements are no-ops. Conflicting retries
            # cannot replace a later state or its confirmed positions.
            if current_state == candidate_state and current_positions == open_positions:
                if deferred_exit:
                    raise SdkError("A duplicate acknowledgement cannot add a new deferred exit.")
                if reservation:
                    # A retry of an already committed response cannot introduce
                    # a new decision or reserve a changed intent without CAS.
                    return False
                return None
            raise SdkError("Conflicting duplicate confirmed execution acknowledgement.")

        current_external = current_state.get("external_execution") or {}
        if (
            current_state != expected_state
            or current_positions != expected_open_positions
            or current_external.get("applied_through", 0) != expected_applied_through
            or _broker_state_revision(conn, strategy_id) != expected_broker_revision
        ):
            raise SdkError("Stale confirmed close acknowledgement; broker or strategy state changed.")
        if event_ids:
            if response_cursor != max(sequences) or sorted(sequences) != list(
                range(expected_applied_through + 1, max(sequences) + 1)
            ):
                raise SdkError("Confirmed execution acknowledgement has a sequence gap or stale cursor.")
        elif response_cursor != expected_applied_through:
            raise SdkError("Empty acknowledgement changed the confirmed execution cursor.")

        timestamp = now()
        conn.execute(
            "UPDATE strategies SET execution_state_json=?,open_positions_json=? WHERE id=?",
            (candidate_json, candidate_positions_json, strategy_id),
        )
        if event_ids:
            for event_id in event_ids:
                receipt_json = json.dumps(by_id[event_id], sort_keys=True, separators=(",", ":")) if receipts is not None else None
                conn.execute(
                    """UPDATE broker_execution_events SET acknowledged_at=?,receipt_json=?
                    WHERE strategy_id=? AND event_id=?""",
                    (timestamp, receipt_json, strategy_id, event_id),
                )
        if deferred_exit:
            intent = OrderIntent.model_validate_json(deferred_exit["intent_json"])
            if (
                intent.side != "sell" or intent.action != "exit"
                or intent.engine_position_id != deferred_exit.get("position_id")
                or deferred_exit.get("evaluation_id") is None
            ):
                raise SdkError("Deferred exit does not match its hosted position decision.")
            if _route_fingerprint_in_conn(conn, strategy_id) != deferred_exit.get("route_fingerprint"):
                raise SdkError("Deferred exit route changed before acknowledgement.")
            conn.execute(
                """INSERT INTO broker_deferred_exits
                (id,strategy_id,position_id,evaluation_id,engine_order_id,intent_json,
                 position_template_json,decision_quantity,blocker_order_id,blocker_filled_at_decision,
                 route_fingerprint,status,detail,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,'waiting_stop_terminal',NULL,?,?)""",
                (deferred_exit["id"], strategy_id, deferred_exit["position_id"], deferred_exit["evaluation_id"],
                 deferred_exit.get("engine_order_id"), deferred_exit["intent_json"],
                 json.dumps(deferred_exit["position_template"], allow_nan=False),
                 _exact_decimal_text(deferred_exit["decision_quantity"]), deferred_exit.get("blocker_order_id"),
                 _exact_decimal_text(deferred_exit.get("blocker_filled_at_decision", "0")),
                 deferred_exit["route_fingerprint"],
                 timestamp, timestamp),
            )
        if reservation:
            if not _reserve_in_transaction(conn, strategy_id, **reservation):
                raise SdkError("Confirmed execution ACK and order reservation could not commit together.")
            return True
        if deferred_exit:
            return True
        return None


def _external_event_hash(event):
    """Match the hosted v2 canonical event hash before accepting a receipt."""
    canonical = dict(event)
    canonical.setdefault("occurred_at", None)
    for key in ("filled_quantity", "realized_pnl", "fees"):
        if key in canonical:
            amount = decimal(canonical[key])
            text = format(amount, "f")
            if "." in text:
                text = text.rstrip("0").rstrip(".")
            canonical[key] = "0" if amount == 0 else (text or "0")
    if canonical.get("occurred_at") is not None:
        try:
            parsed = datetime.fromisoformat(canonical["occurred_at"].replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                raise ValueError
            canonical["occurred_at"] = parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        except (AttributeError, ValueError):
            raise SdkError("Invalid broker event occurrence timestamp.") from None
    encoded = json.dumps(canonical, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def execution_inputs(strategy_id, mark_price, *, include_revision=False, include_external_events=False):
    """Build Mangrove's stateless inputs exclusively from broker-confirmed fills."""
    with connection() as conn:
        if include_revision:
            conn.execute("BEGIN")
        route_row = conn.execute("SELECT * FROM broker_routes WHERE strategy_id=?", (strategy_id,)).fetchone()
        config = dict(route_row) if route_row else None
        if config is None:
            raise SdkError("Strategy has no configured broker route.")
        strategy = dict(conn.execute("SELECT * FROM strategies WHERE id=?", (strategy_id,)).fetchone())
        positions = conn.execute(
            "SELECT * FROM positions WHERE strategy_id=? AND status='open'", (strategy_id,)
        ).fetchall()
        history = conn.execute("SELECT * FROM broker_orders WHERE strategy_id=?", (strategy_id,)).fetchall()
        cash = decimal(config["capital"])
        for order in history:
            intent = json.loads(order["intent_json"])
            gross, fee = decimal(order["filled_notional"]), decimal(order["fees"])
            cash += (-gross if intent["side"] == "buy" else gross) - fee
        payloads = []
        latest = {p["id"]: p for p in json.loads(strategy["open_positions_json"] or "[]")}
        for pos in positions:
            entries = [
                o
                for o in history
                if json.loads(o["intent_json"]).get("engine_position_id") == pos["id"]
                and json.loads(o["intent_json"])["side"] == "buy"
            ]
            if not entries:
                raise SdkError("Confirmed position has no broker provenance.")
            payload = latest.get(pos["id"]) or json.loads(entries[0]["position_template_json"])
            if not payload:
                raise SdkError("Missing Mangrove position template.")
            holding = conn.execute("SELECT * FROM broker_holdings WHERE position_id=?", (pos["id"],)).fetchone()
            if not holding:
                raise SdkError("Missing exact broker holding.")
            payload.update(
                id=pos["id"],
                asset=strategy["asset"],
                side="long",
                open=True,
                position_size=float(holding["quantity"]),
                entry_price=float(holding["unit_cost"]),
                cost=pos["entry_amount"] * pos["entry_price"],
                entry_timestamp=pos["entry_time"],
            )
            for order in payload.get("orders", []):
                order["position_size"] = pos["entry_amount"]
            payloads.append(payload)
        raw_state = json.loads(strategy["execution_state_json"] or "{}")
        raw_positions = json.loads(strategy["open_positions_json"] or "[]")
        state = deepcopy(raw_state)
        state.update(
            cash_balance=float(cash),
            account_value=float(cash) + sum(p["entry_amount"] * mark_price for p in positions),
            num_open_positions=len(positions),
        )
        if config.get("external_execution_mode", "legacy") != "confirmed_v2":
            state["total_trades"] = sum(
                1
                for o in history
                if json.loads(o["intent_json"])["side"] == "sell" and decimal(o["filled_quantity"]) > 0
            )
        else:
            state.setdefault("total_trades", 0)
        if state.get("risk_state") and config.get("external_execution_mode", "legacy") != "confirmed_v2":
            risk = state["risk_state"]
            risk["open_position_count"] = len(positions)
            # A GTC exit may fill on a later day than its modeled evaluation.
            # Count actual exit orders on the reconciliation day too, so the
            # engine cannot reset away a real fill's daily trade budget.
            counts = risk.setdefault("daily_trade_counter", {})
            fills = conn.execute(
                """SELECT substr(t.confirmed_at,1,10) AS day, count(DISTINCT o.id) AS count
                FROM trades t JOIN broker_orders o ON t.venue_order_ref=o.broker_order_id AND t.strategy_id=o.strategy_id
                WHERE o.strategy_id=? AND json_extract(o.intent_json,'$.side')='sell'
                GROUP BY day""",
                (strategy_id,),
            ).fetchall()
            for fill in fills:
                counts[fill["day"]] = max(counts.get(fill["day"], 0), fill["count"])
            risk["today"] = now()[:10]
        if include_revision:
            revision = _broker_state_revision(conn, strategy_id)
            if include_external_events:
                events = [dict(row) for row in conn.execute(
                    """SELECT sequence,event_id,position_id,asset,kind,filled_quantity,
                              realized_pnl,fees,occurred_at
                       FROM broker_execution_events WHERE strategy_id=? AND acknowledged_at IS NULL
                       ORDER BY sequence LIMIT 512""",
                    (strategy_id,),
                ).fetchall()]
                encoded_events = []
                for event in events:
                    if event["occurred_at"] is None:
                        event.pop("occurred_at")
                    if event["kind"] == "opened":
                        event.pop("realized_pnl")
                        event.pop("fees")
                    else:
                        event.pop("filled_quantity")
                    encoded_events.append(event)
                return state, payloads, revision, encoded_events, raw_state, raw_positions
            return state, payloads, revision
        return state, payloads
