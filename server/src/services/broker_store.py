"""Transactional broker journal and fill-derived strategy state.

Amounts stay decimal strings in the journal. Updates to journal, trades, and
positions commit together; repeated cumulative fill snapshots are harmless.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
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


def orders(strategy_id=None, *, pending=False):
    sql, values = "SELECT * FROM broker_orders WHERE 1=1", []
    if strategy_id:
        sql += " AND strategy_id=?"
        values.append(strategy_id)
    if pending:
        sql += " AND status IN (" + ",".join("?" for _ in PENDING) + ")"
        values.extend(PENDING)
    with connection() as conn:
        return [dict(r) for r in conn.execute(sql + " ORDER BY created_at", values).fetchall()]


def reserve(strategy_id, evaluation_id, intent, request, template, ref_id, *, expected_revision=None):
    key = intent.engine_order_id or f"{evaluation_id}:{intent.engine_position_id}:{intent.action}"
    if intent.side == "sell":
        key += ":" + str(intent.amount)
    with connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        active = conn.execute(
            "SELECT status,exit_only_mode,execution_revision,book_revision FROM strategies WHERE id=?", (strategy_id,)
        ).fetchone()
        if not active or not intent.engine_position_id:
            return False
        if intent.action == "enter":
            existing_position = conn.execute(
                "SELECT status,strategy_id FROM positions WHERE id=?", (intent.engine_position_id,)
            ).fetchone()
            if existing_position and (
                existing_position["status"] != "open" or existing_position["strategy_id"] != strategy_id
            ):
                return False
            if active["status"] != "live" or expected_revision is None or tuple(active)[2:] != tuple(expected_revision):
                return False
        elif active["status"] != "live" and active["exit_only_mode"] != "live":
            return False
        pending = conn.execute(
            "SELECT intent_json FROM broker_orders WHERE strategy_id=? AND status IN ('submitting','uncertain','open','partial','awaiting_approval','cancel_pending')",
            (strategy_id,),
        ).fetchall()
        for row in pending:
            other = json.loads(row[0])
            if (
                not other.get("engine_position_id")
                or other["engine_position_id"] == intent.engine_position_id
                or intent.action == "enter"
            ):
                return False
        if intent.action == "exit":
            held = conn.execute(
                "SELECT h.quantity,p.status FROM broker_holdings h JOIN positions p ON p.id=h.position_id WHERE h.strategy_id=? AND h.position_id=?",
                (strategy_id, intent.engine_position_id),
            ).fetchone()
            if not held or held["status"] != "open" or decimal(request["quantity"]) > decimal(held["quantity"]):
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
        return True


def mark(order_id, status, *, broker_id=None, approval_id=None, error=None):
    with connection() as conn:
        conn.execute(
            """UPDATE broker_orders SET status=?,broker_order_id=COALESCE(?,broker_order_id),
            approval_id=COALESCE(?,approval_id),error_code=?,updated_at=? WHERE id=?""",
            (status, broker_id, approval_id, error, now(), order_id),
        )


def apply_snapshot(order_id, snapshot, *, asset_class):
    """Persist a cumulative fill once, including partial-then-cancel outcomes."""
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
            saved = conn.execute(
                "SELECT execution_state_json FROM strategies WHERE id=?", (row["strategy_id"],)
            ).fetchone()
            next_state = json.loads(saved[0] or "{}")
            if not buy:
                if not pos or pos["status"] != "open" or held_qty < delta:
                    raise SdkError("Exit fill exceeds this strategy's confirmed holding.")
                pnl = float(delta_gross - delta * held_cost - delta_fees)
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
            else:
                remaining = held_qty - delta
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
                "UPDATE strategies SET execution_state_json=?,book_revision=book_revision+1 WHERE id=?",
                (json.dumps(next_state), row["strategy_id"]),
            )
        elif delta_gross or delta_fees:
            raise SdkError("Broker revised fill cash without a new fill; reconciliation required.")
        conn.execute(
            """UPDATE broker_orders SET broker_order_id=?,status=?,filled_quantity=?,filled_notional=?,
            fees=?,updated_at=?,error_code=NULL WHERE id=?""",
            (snapshot["id"], state, str(quantity), str(gross), str(fees), timestamp, order_id),
        )
        conn.execute(
            "INSERT INTO broker_events(order_id,status,filled_quantity,created_at) VALUES (?,?,?,?)",
            (order_id, state, str(quantity), timestamp),
        )


def execution_inputs(strategy_id, mark_price):
    """Build Mangrove's stateless inputs exclusively from broker-confirmed fills."""
    config = route(strategy_id)
    with connection() as conn:
        strategy = dict(conn.execute("SELECT * FROM strategies WHERE id=?", (strategy_id,)).fetchone())
        positions = conn.execute(
            "SELECT p.* FROM positions p JOIN trades t ON t.id=p.entry_trade_id WHERE p.strategy_id=? AND p.status='open' AND t.mode='live'",
            (strategy_id,),
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
            from src.services.confirmed_execution import activate_template

            payloads.append(activate_template(payload, dict(pos)))
        state = json.loads(strategy["execution_state_json"] or "{}")
        state.update(
            cash_balance=float(cash),
            account_value=float(cash) + sum(p["entry_amount"] * mark_price for p in positions),
            num_open_positions=len(positions),
            total_trades=conn.execute(
                "SELECT count(*) FROM positions p JOIN broker_holdings h ON h.position_id=p.id WHERE p.strategy_id=? AND p.status='closed'",
                (strategy_id,),
            ).fetchone()[0],
        )
        # Capital is a recorded opening budget: configure() forbids reassignment
        # after any execution journal exists. It is not reconstructed from PnL.
        state["reference_capital"] = float(decimal(config["capital"]))
        state["cumulative_deposits"] = float(decimal(config["capital"]))
        realized = conn.execute(
            "SELECT p_and_l FROM trades WHERE strategy_id=? AND venue='robinhood' AND status='confirmed' AND p_and_l IS NOT NULL",
            (strategy_id,),
        ).fetchall()
        state["realized_pnl"] = float(sum((decimal(p[0]) for p in realized), Decimal(0)))
        return state, payloads
