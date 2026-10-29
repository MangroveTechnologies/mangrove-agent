"""Confirmed journal inputs and response fences; all trading rules stay in MangroveAI."""

from __future__ import annotations

import copy
import json
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal

from src.services import broker_store
from src.shared.errors import SdkError

_locks: dict[str, threading.Lock] = {}
_locks_lock = threading.Lock()
_RISK_FIELDS = ("risk_state", "halt", "bar_index", "last_evaluated_candle", "last_evaluated_timeframe")


@contextmanager
def tick_lock(strategy_id):
    with _locks_lock:
        lock = _locks.setdefault(strategy_id, threading.Lock())
    acquired = lock.acquire(blocking=False)
    try:
        yield acquired
    finally:
        if acquired:
            lock.release()


def decimal(value):
    try:
        number = Decimal(str(value))
        if not number.is_finite():
            raise ValueError
        return number
    except Exception:
        raise SdkError("Confirmed execution journal contains an invalid amount.") from None


def utc(value):
    if not value:
        raise SdkError("Confirmed execution journal is missing its settlement time.")
    stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if stamp.tzinfo is None:
        raise SdkError("Confirmed execution journal settlement time has no timezone.")
    return stamp.astimezone(timezone.utc).isoformat()


def receipt_ids(state):
    risk = (state or {}).get("risk_state") or {}
    return (
        set(risk.get("recent_close_ids") or [])
        | set(risk.get("recent_close_id_seen_at") or {})
        | set(risk.get("resume_close_ids") or [])
    )


def snapshot_revision(strategy_id):
    with broker_store.connection() as conn:
        row = conn.execute(
            "SELECT execution_revision,book_revision FROM strategies WHERE id=?", (strategy_id,)
        ).fetchone()
        if not row:
            raise SdkError("Strategy no longer exists.")
        return tuple(row)


def save_risk(strategy_id, before, response, closed_positions, revision, *, complete, state_field="execution_state_json"):
    """Persist only engine risk/clock fields; actual journal balances remain owned here.

    An incomplete financial book cannot acknowledge estimated closes or a new halt.
    A stale evaluation cannot authorize entries. Callers may retain valid exits.
    """
    if state_field not in {"execution_state_json", "paper_execution_state_json"}:
        raise ValueError("Unsupported risk state column")
    returned = response.get("execution_state")
    if not complete or not isinstance(returned, dict):
        return False
    allowed = receipt_ids(before) | {p["position_id"] for p in closed_positions}
    if not receipt_ids(returned).issubset(allowed):
        return False
    if not (returned.get("risk_state") or {}).get("close_history_bootstrapped"):
        return False
    # A response cannot advance the durable delivery cursor beyond booked facts.
    oldrisk, newrisk = before.get("risk_state") or {}, returned.get("risk_state") or {}
    bounds = [p["booked_at"] for p in closed_positions]
    if oldrisk.get("closes_booked_through"):
        bounds.append(oldrisk["closes_booked_through"])
    if newrisk.get("closes_booked_through"):
        try:
            if not bounds or utc(newrisk["closes_booked_through"]) > max(utc(v) for v in bounds):
                return False
        except (ValueError, TypeError, SdkError):
            return False
    with broker_store.connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(f"SELECT {state_field} FROM strategies WHERE id=?", (strategy_id,)).fetchone()
        if not row:
            return False
        state = json.loads(row[0] or "{}")
        for key in _RISK_FIELDS:
            if key in returned:
                state[key] = copy.deepcopy(returned[key])
        templates = []
        for proposed in response.get("open_positions") or []:
            position = conn.execute(
                "SELECT * FROM positions WHERE id=? AND strategy_id=? AND status='open'",
                (proposed.get("id"), strategy_id),
            ).fetchone()
            if position:
                templates.append(activate_template(proposed, dict(position)))
        result = conn.execute(
            f"""UPDATE strategies SET {state_field}=?,open_positions_json=?,execution_revision=execution_revision+1
               WHERE id=? AND execution_revision=? AND book_revision=?""",
            (json.dumps(state), json.dumps(templates), strategy_id, *revision),
        )
        return result.rowcount == 1


def broker_history(strategy_id):
    """One close per fully closed position, including every actual partial fill's net PnL."""
    with broker_store.connection() as conn:
        positions = conn.execute(
            "SELECT p.* FROM positions p JOIN trades t ON t.id=p.entry_trade_id WHERE p.strategy_id=? AND p.status='closed' AND t.mode='live'",
            (strategy_id,),
        ).fetchall()
        trades = conn.execute(
            "SELECT * FROM trades WHERE strategy_id=? AND venue='robinhood' ORDER BY confirmed_at,id", (strategy_id,)
        ).fetchall()
    closes, complete = [], True
    for position in positions:
        exits = [
            t
            for t in trades
            if json.loads(t["order_intent_json"]).get("engine_position_id") == position["id"]
            and json.loads(t["order_intent_json"]).get("side") == "sell"
            and t["status"] == "confirmed"
        ]
        if not exits or any(t["p_and_l"] is None or not t["confirmed_at"] for t in exits):
            complete = False
            continue
        booked = max(utc(t["confirmed_at"]) for t in exits)
        closes.append(
            {
                "position_id": position["id"],
                "asset": position["asset"],
                "realized_pnl": float(sum((decimal(t["p_and_l"]) for t in exits), Decimal(0))),
                "exit_timestamp": booked,
                "booked_at": booked,
                "exit_type": None,
            }
        )
    return sorted(closes, key=lambda p: (p["booked_at"], p["position_id"])), complete


def close_delivery(state, closes, *, complete):
    """Delivery receipts belong to booked events; an incomplete history sends no partial ACK."""
    if not complete:
        return [], False
    risk = state.get("risk_state") or {}
    if (
        not risk.get("close_history_bootstrapped")
        or risk.get("close_delivery_incomplete")
        or risk.get("close_ingestion_failed")
    ):
        return closes, True
    cursor = risk.get("closes_booked_through")
    if not cursor:
        return closes, True
    try:
        if any(p["position_id"] not in receipt_ids(state) and utc(p["booked_at"]) <= utc(cursor) for p in closes):
            return closes, True
        selected = [p for p in closes if utc(p["booked_at"]) > utc(cursor)]
        watermark = risk.get("last_close_seen_at")
        if watermark and any(
            p["position_id"] not in receipt_ids(state) and utc(p["exit_timestamp"]) < utc(watermark) for p in selected
        ):
            return closes, True
        return selected, False
    except (ValueError, TypeError, SdkError):
        return closes, True


def activate_template(template, position, orders=None):
    """Use a prospective engine template only after an actual fill owns the quantity."""
    payload = copy.deepcopy(template or {})
    payload.update(
        id=position["id"],
        asset=position["asset"],
        side="long",
        open=True,
        position_size=float(position["entry_amount"]),
        entry_price=float(position["entry_price"]),
        entry_timestamp=position["entry_time"],
        cost=float(decimal(position["entry_amount"]) * decimal(position["entry_price"])),
        exit_timestamp=None,
        exit_price=None,
        exit_reason=None,
        value_at_close=None,
    )
    payload.pop("execution_confirmed", None)
    for order in payload.get("orders") or []:
        order["position_size"] = float(position["entry_amount"])
        side = str(order.get("side") or "").lower()
        kind = str(order.get("order_type") or order.get("type") or "").lower()
        order["status"] = "filled" if "enter" in side or kind == "entry" else "pending"
    if orders is not None:
        payload["orders"] = orders
    return payload


def has_exposure(strategy_id):
    with broker_store.connection() as conn:
        return bool(
            conn.execute(
                "SELECT 1 FROM positions p JOIN trades t ON t.id=p.entry_trade_id WHERE p.strategy_id=? AND p.status='open' AND t.mode='live' LIMIT 1",
                (strategy_id,),
            ).fetchone()
            or conn.execute(
                "SELECT 1 FROM trades WHERE strategy_id=? AND status='pending' LIMIT 1", (strategy_id,)
            ).fetchone()
            or conn.execute(
                "SELECT 1 FROM dex_orders WHERE strategy_id=? AND status IN ('submitting','uncertain','pending') LIMIT 1",
                (strategy_id,),
            ).fetchone()
            or conn.execute(
                "SELECT 1 FROM broker_orders WHERE strategy_id=? AND status IN ('submitting','uncertain','open','partial','awaiting_approval','cancel_pending') LIMIT 1",
                (strategy_id,),
            ).fetchone()
        )


def entries_only_removed(intents):
    return [intent for intent in intents if intent.action == "exit"]


def dex_inputs(strategy_id, mark_price):
    """Actual quote cash and physical quantities; missing history denies entries."""
    from src.services import dex_confirmed

    with broker_store.connection() as conn:
        row = conn.execute("SELECT * FROM strategies WHERE id=?", (strategy_id,)).fetchone()
        capital = conn.execute("SELECT * FROM execution_capital WHERE strategy_id=?", (strategy_id,)).fetchone()
        trades = conn.execute("SELECT * FROM trades WHERE strategy_id=? AND mode='live'", (strategy_id,)).fetchall()
        positions = conn.execute(
            "SELECT p.*,h.quantity,h.unit_cost,h.financial_complete FROM positions p JOIN dex_holdings h ON h.position_id=p.id WHERE p.strategy_id=? AND p.status='open'",
            (strategy_id,),
        ).fetchall()
        legacy = conn.execute(
            "SELECT 1 FROM trades WHERE strategy_id=? AND mode='live' AND id NOT IN (SELECT id FROM dex_orders) LIMIT 1",
            (strategy_id,),
        ).fetchone()
        unproven_positions = conn.execute(
            "SELECT 1 FROM positions p JOIN trades t ON t.id=p.entry_trade_id WHERE p.strategy_id=? AND t.mode='live' AND p.id NOT IN (SELECT position_id FROM dex_holdings) LIMIT 1",
            (strategy_id,),
        ).fetchone()
        state = json.loads(row["execution_state_json"] or "{}")
    history = dex_confirmed.orders(strategy_id)
    latest = {p["id"]: p for p in json.loads(row["open_positions_json"] or "[]")}
    complete = bool(
        capital and capital["quote_token_symbol"].upper() == "USDC" and not legacy and not unproven_positions
    )
    cash = decimal(capital["amount"]) if capital else Decimal(0)
    for order in history:
        context = json.loads(order["context_json"])
        if order["status"] == "failed" and context.get("no_broadcast_confirmed"):
            continue
        if order["status"] == "fee_only":
            complete = complete and context.get("financial_complete", False)
            continue
        if order["status"] != "confirmed":
            complete = False
            continue
        if not context.get("financial_complete"):
            complete = False
        fill = context["settlement"]
        intent = json.loads(order["intent_json"])
        cash += -decimal(fill["input_amount"]) if intent["action"] == "enter" else decimal(fill["output_amount"])
    payloads = []
    for pos in positions:
        entry = next(
            (
                o
                for o in history
                if json.loads(o["intent_json"]).get("engine_position_id") == pos["id"]
                and json.loads(o["intent_json"])["action"] == "enter"
            ),
            None,
        )
        if not entry:
            complete = False
            continue
        position = dict(pos)
        position.update(entry_amount=float(decimal(pos["quantity"])), entry_price=float(decimal(pos["unit_cost"])))
        payload = activate_template(latest.get(pos["id"]) or json.loads(entry["template_json"]), position)
        payload["asset"] = row["asset"]
        payloads.append(payload)
    closes = []
    with broker_store.connection() as conn:
        closed = conn.execute(
            "SELECT p.* FROM positions p JOIN trades t ON t.id=p.entry_trade_id WHERE p.strategy_id=? AND p.status='closed' AND t.mode='live'",
            (strategy_id,),
        ).fetchall()
    for pos in closed:
        exits = [
            t
            for t in trades
            if json.loads(t["order_intent_json"]).get("engine_position_id") == pos["id"]
            and json.loads(t["order_intent_json"]).get("action") == "exit"
            and t["status"] == "confirmed"
        ]
        if not exits or any(t["p_and_l"] is None for t in exits):
            complete = False
            continue
        booked = max(utc(json.loads(t["execution_context_json"])["booked_at"]) for t in exits)
        closes.append(
            dict(
                position_id=pos["id"],
                asset=pos["asset"],
                realized_pnl=float(sum((decimal(t["p_and_l"]) for t in exits), Decimal(0))),
                exit_timestamp=utc(pos["exit_time"]),
                booked_at=booked,
                exit_type=None,
            )
        )
    state.update(
        cash_balance=float(cash),
        account_value=float(cash) + sum(p["position_size"] * mark_price for p in payloads),
        num_open_positions=len(payloads),
        total_trades=len(closes),
        realized_pnl=float(sum((decimal(t["p_and_l"]) for t in trades if t["p_and_l"] is not None), Decimal(0))),
    )
    if capital:
        state.update(
            reference_capital=float(decimal(capital["amount"])), cumulative_deposits=float(decimal(capital["amount"]))
        )
    state["financial_complete"] = complete
    return state, payloads, sorted(closes, key=lambda p: (p["booked_at"], p["position_id"])), complete
