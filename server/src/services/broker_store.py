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


def reserve(strategy_id, evaluation_id, intent, request, template, ref_id):
    key = intent.engine_order_id or f"{evaluation_id}:{intent.engine_position_id}:{intent.action}"
    if intent.side == "sell":
        key += ":" + str(intent.amount)
    with connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
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
        return True


def mark(order_id, status, *, broker_id=None, approval_id=None, error=None):
    with connection() as conn:
        conn.execute(
            """UPDATE broker_orders SET status=?,broker_order_id=COALESCE(?,broker_order_id),
            approval_id=COALESCE(?,approval_id),error_code=?,updated_at=? WHERE id=?""",
            (status, broker_id, approval_id, error, now(), order_id),
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
            next_state = proposed if old_qty == 0 else deepcopy(current_state)
            if not buy:
                if not pos or pos["status"] != "open" or held_qty < delta:
                    raise SdkError("Exit fill exceeds this strategy's confirmed holding.")
                exact_pnl = delta_gross - delta * held_cost - delta_fees
                pnl = float(exact_pnl)
                # Mangrove's risk history is retained conservatively. If a modeled
                # non-loss becomes a real loss, stop new entries until that risk
                # history can be reconciled; never silently erase a loss gate.
                if pnl < 0 and decimal(intent.ref_price) >= held_cost:
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
                        FROM broker_close_events WHERE strategy_id=?""",
                        (row["strategy_id"],),
                    ).fetchone()["next_sequence"]
                    # Emit only when this position is fully flat. The raw
                    # snapshot journal retains execution timestamps; a
                    # cumulative snapshot cannot always attribute a timestamp
                    # to a delta, so occurred_at stays null instead of being
                    # replaced with local observation time.
                    conn.execute(
                        """INSERT INTO broker_close_events
                        (sequence,event_id,final_order_id,strategy_id,position_id,asset,
                         realized_pnl,fees,occurred_at,observed_at)
                        VALUES (?,?,?,?,?,?,?, ?,NULL,?)""",
                        (
                            next_sequence,
                            f"close:{position_id}",
                            order_id,
                            row["strategy_id"],
                            position_id,
                            strategy_asset,
                            str(realized_pnl),
                            str(all_fees),
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
            FROM broker_close_events
            WHERE strategy_id=? AND acknowledged_at IS NULL ORDER BY sequence""",
            (strategy_id,),
        ).fetchall()
        return [dict(row) for row in rows]


def _broker_state_revision(conn, strategy_id):
    """Hash the confirmed broker book for ack compare-and-swap."""
    snapshot = {
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
        "close_events": [
            list(row)
            for row in conn.execute(
                """SELECT sequence,event_id,acknowledged_at FROM broker_close_events
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
):
    """Atomically save the risk state produced by close events and acknowledge them.

    This is an internal persistence seam only; no public route currently sends
    close events to Mangrove or invokes this acknowledgement. The expected
    state, position payload, event cursor, and broker-book fingerprint form a
    compare-and-swap guard against a stale evaluation overwriting later fills
    or another acknowledgement.
    """
    if not isinstance(event_ids, (list, tuple)) or not event_ids:
        raise SdkError("At least one confirmed close event is required for acknowledgement.")
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
    if type(response_cursor) is not int:
        raise SdkError("Invalid confirmed close response cursor.")

    with connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        rows = conn.execute(
            """SELECT event_id,acknowledged_at FROM broker_close_events
            WHERE strategy_id=? AND event_id IN (""" + ",".join("?" for _ in event_ids) + ")",
            [strategy_id, *event_ids],
        ).fetchall()
        if {row["event_id"] for row in rows} != set(event_ids):
            raise SdkError("Confirmed close acknowledgement contains an unknown event.")
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
        sequences = [
            row["sequence"]
            for row in conn.execute(
                """SELECT sequence FROM broker_close_events
                WHERE strategy_id=? AND event_id IN (""" + ",".join("?" for _ in event_ids) + ")",
                [strategy_id, *event_ids],
            ).fetchall()
        ]
        if all(row["acknowledged_at"] is not None for row in rows):
            # Exact duplicate acknowledgements are no-ops. Conflicting retries
            # cannot replace a later state or its confirmed positions.
            if current_state == candidate_state and current_positions == open_positions:
                return
            raise SdkError("Conflicting duplicate confirmed close acknowledgement.")

        current_external = current_state.get("external_execution") or {}
        if (
            current_state != expected_state
            or current_positions != expected_open_positions
            or current_external.get("applied_through", 0) != expected_applied_through
            or _broker_state_revision(conn, strategy_id) != expected_broker_revision
        ):
            raise SdkError("Stale confirmed close acknowledgement; broker or strategy state changed.")
        if response_cursor != max(sequences) or sorted(sequences) != list(
            range(expected_applied_through + 1, max(sequences) + 1)
        ):
            raise SdkError("Confirmed close acknowledgement has a sequence gap or stale cursor.")

        timestamp = now()
        conn.execute(
            "UPDATE strategies SET execution_state_json=?,open_positions_json=? WHERE id=?",
            (candidate_json, candidate_positions_json, strategy_id),
        )
        conn.execute(
            """UPDATE broker_close_events SET acknowledged_at=?
            WHERE strategy_id=? AND event_id IN (""" + ",".join("?" for _ in event_ids) + ")",
            [timestamp, strategy_id, *event_ids],
        )


def execution_inputs(strategy_id, mark_price, *, include_revision=False):
    """Build Mangrove's stateless inputs exclusively from broker-confirmed fills."""
    config = route(strategy_id)
    with connection() as conn:
        if include_revision:
            conn.execute("BEGIN")
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
        state = json.loads(strategy["execution_state_json"] or "{}")
        state.update(
            cash_balance=float(cash),
            account_value=float(cash) + sum(p["entry_amount"] * mark_price for p in positions),
            num_open_positions=len(positions),
            total_trades=sum(
                1
                for o in history
                if json.loads(o["intent_json"])["side"] == "sell" and decimal(o["filled_quantity"]) > 0
            ),
        )
        if state.get("risk_state"):
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
            return state, payloads, _broker_state_revision(conn, strategy_id)
        return state, payloads
