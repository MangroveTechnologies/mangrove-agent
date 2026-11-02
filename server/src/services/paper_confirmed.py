"""Stateless paper books actual simulated fills, never evaluation estimates.

Signals, sizing and risk remain in MangroveAI. This adapter only owns the
paper executor's cash, positions, templates and delivered close receipts.
"""

from __future__ import annotations

import json
import uuid

from src.models.domain import Evaluation
from src.services import broker_store as store
from src.services import confirmed_execution as confirmed
from src.services import order_executor, trade_log
from src.shared.errors import SdkError
from src.shared.logging import get_logger

_log = get_logger(__name__)


def has_exposure(strategy_id):
    with store.connection() as conn:
        return bool(conn.execute(
            "SELECT 1 FROM positions p JOIN trades t ON t.id=p.entry_trade_id "
            "WHERE p.strategy_id=? AND p.status='open' AND t.mode='paper' LIMIT 1", (strategy_id,),
        ).fetchone())


def inputs(strategy_id, row, mark):
    config = json.loads(row["execution_config_json"] or "{}")
    if config.get("position_size_calc", "v2") not in {"v1", "v2"}:
        raise SdkError("Stateless paper supports v1/v2 long positions only; v3 core allocations are unsupported.")
    capital = confirmed.decimal(config.get("initial_balance", 10000))
    if capital <= 0:
        raise SdkError("Paper initial capital must be positive.")
    with store.connection() as conn:
        conn.execute("INSERT OR IGNORE INTO paper_capital VALUES (?,?,?)", (strategy_id, str(capital), store.now()))
        capital = confirmed.decimal(conn.execute("SELECT amount FROM paper_capital WHERE strategy_id=?", (strategy_id,)).fetchone()[0])
        fills = conn.execute(
            "SELECT t.*,f.template_json,e.sdk_response_json FROM trades t LEFT JOIN paper_fills f ON f.trade_id=t.id "
            "LEFT JOIN evaluations e ON e.id=t.evaluation_id "
            "WHERE t.strategy_id=? AND t.mode='paper' ORDER BY t.executed_at,t.id", (strategy_id,),
        ).fetchall()
        positions = conn.execute(
            "SELECT p.*,t.mode FROM positions p JOIN trades t ON t.id=p.entry_trade_id WHERE p.strategy_id=?",
            (strategy_id,),
        ).fetchall()
    state = json.loads(row["paper_execution_state_json"] or "{}")
    templates = {p["id"]: p for p in json.loads(row["open_positions_json"] or "[]")}
    cash, complete = capital, True
    by_id = {}
    for fill in fills:
        by_id[fill["id"]] = fill
        if fill["template_json"] is None:
            # An earlier paper fill may predate the journal. Its recorded
            # evaluation supplies protective levels, never financial facts.
            response = json.loads(fill["sdk_response_json"] or "{}")
            for template in response.get("proposed_positions") or []:
                templates.setdefault(template["id"], template)
        if fill["status"] != "simulated" or fill["template_json"] is None or json.loads(fill["fees_json"] or "{}"):
            complete = False
            continue
        intent = json.loads(fill["order_intent_json"])
        if intent.get("action") == "enter" and intent.get("side") == "buy":
            cash -= confirmed.decimal(fill["input_amount"])
            templates[intent["engine_position_id"]] = json.loads(fill["template_json"])
        elif intent.get("action") == "exit" and intent.get("side") == "sell":
            cash += confirmed.decimal(fill["output_amount"])
            if fill["p_and_l"] is None:
                complete = False
        else:
            complete = False
    opened, closes = [], []
    for position in positions:
        if position["mode"] != "paper":
            if position["status"] == "open":
                complete = False
            continue
        entry = by_id.get(position["entry_trade_id"])
        if not entry or entry["status"] != "simulated":
            complete = False
            continue
        if position["status"] == "open":
            template = templates.get(position["id"], {})
            if not template:
                complete = False
            opened.append(confirmed.activate_template(template, dict(position)))
        else:
            exit_fill = by_id.get(position["exit_trade_id"])
            if not exit_fill or exit_fill["p_and_l"] is None or exit_fill["template_json"] is None:
                complete = False
                continue
            closes.append(dict(position_id=position["id"], asset=row["asset"],
                               exit_timestamp=position["exit_time"], booked_at=exit_fill["executed_at"],
                               realized_pnl=float(exit_fill["p_and_l"]), exit_type="paper_fill"))
    state.update(cash_balance=float(cash), account_value=float(cash) + sum(p["position_size"] * mark for p in opened),
                 num_open_positions=len(opened), total_trades=len(closes),
                 reference_capital=float(capital), cumulative_deposits=float(capital))
    if cash < 0:
        complete = False
    return state, opened, closes, complete


def fill(strategy_id, evaluation_id, intent, template, revision):
    """Persist one simulated fill, position and dedup key in one transaction."""
    if not intent.engine_position_id or not intent.engine_order_id:
        raise SdkError("Paper fills require engine position and order identities.")
    if (intent.action, intent.side) not in {("enter", "buy"), ("exit", "sell")}:
        raise SdkError("Stateless paper supports long entry and exit intents only.")
    quantity = confirmed.decimal(intent.amount)
    if quantity <= 0:
        raise SdkError("Paper quantity must be positive.")
    trade = order_executor._paper_fill(intent, strategy_id, evaluation_id, record=False)
    price = confirmed.decimal(trade.fill_price)
    if price <= 0:
        raise SdkError("Paper fill price must be positive.")
    with store.connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT status,exit_only_mode,execution_revision,book_revision FROM strategies WHERE id=?", (strategy_id,)).fetchone()
        if not row or tuple(row[k] for k in ("execution_revision", "book_revision")) != tuple(revision):
            raise SdkError("Paper book changed before fill; no fill booked.")
        if conn.execute("SELECT 1 FROM paper_fills WHERE strategy_id=? AND intent_key=?", (strategy_id, intent.engine_order_id)).fetchone():
            return False
        position = conn.execute("SELECT * FROM positions WHERE id=?", (intent.engine_position_id,)).fetchone()
        pnl = None
        if intent.action == "enter":
            if row["status"] != "paper" or position:
                raise SdkError("Paper entry no longer owns an active empty position identity.")
            if template.get("id") != intent.engine_position_id or template.get("execution_confirmed") is not False:
                raise SdkError("Paper entry requires its prospective position template.")
            if conn.execute("SELECT 1 FROM trades t LEFT JOIN paper_fills f ON f.trade_id=t.id WHERE t.strategy_id=? AND t.mode='paper' AND f.trade_id IS NULL LIMIT 1", (strategy_id,)).fetchone():
                raise SdkError("Paper fill history is incomplete; new entries are held.")
            capital = confirmed.decimal(conn.execute("SELECT amount FROM paper_capital WHERE strategy_id=?", (strategy_id,)).fetchone()[0])
            cash = capital
            for prior in conn.execute("SELECT t.* FROM trades t JOIN paper_fills f ON f.trade_id=t.id WHERE f.strategy_id=?", (strategy_id,)):
                action = json.loads(prior["order_intent_json"])["action"]
                cash += confirmed.decimal(prior["output_amount"]) if action == "exit" else -confirmed.decimal(prior["input_amount"])
            if quantity * price > cash:
                raise SdkError("Paper fill exceeds confirmed cash.")
        else:
            if row["status"] != "paper" and row["exit_only_mode"] != "paper":
                raise SdkError("Paper exit monitor is inactive.")
            if not position or position["strategy_id"] != strategy_id or position["status"] != "open":
                raise SdkError("Paper exit does not own an open position.")
            entry = conn.execute("SELECT mode,status FROM trades WHERE id=?", (position["entry_trade_id"],)).fetchone()
            if not entry or tuple(entry) != ("paper", "simulated") or quantity != confirmed.decimal(position["entry_amount"]):
                raise SdkError("Paper exit requires the exact paper-owned quantity.")
            pnl = float(quantity * (price - confirmed.decimal(position["entry_price"])))
        conn.execute(
            "INSERT INTO trades(id,strategy_id,evaluation_id,order_intent_json,mode,input_token,input_amount,output_token,output_amount,fill_price,fees_json,status,executed_at,p_and_l) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (trade.id, strategy_id, evaluation_id, intent.model_dump_json(), "paper", trade.input_token,
             trade.input_amount, trade.output_token, trade.output_amount, trade.fill_price, "{}", "simulated", trade.executed_at.isoformat(), pnl),
        )
        if intent.action == "enter":
            conn.execute(
                "INSERT INTO positions(id,strategy_id,asset,entry_trade_id,entry_price,entry_amount,entry_time,status,stop_loss,take_profit) VALUES (?,?,?,?,?,?,?,'open',?,?)",
                (intent.engine_position_id, strategy_id, intent.symbol, trade.id, trade.fill_price, float(quantity),
                 trade.executed_at.isoformat(), intent.stop_loss, intent.take_profit),
            )
        else:
            conn.execute("UPDATE positions SET status='closed',exit_trade_id=?,exit_price=?,exit_amount=?,exit_time=? WHERE id=? AND strategy_id=?",
                         (trade.id, trade.fill_price, float(quantity), trade.executed_at.isoformat(), intent.engine_position_id, strategy_id))
        conn.execute("INSERT INTO paper_fills VALUES (?,?,?,?)", (strategy_id, intent.engine_order_id, trade.id, json.dumps(template)))
        conn.execute("UPDATE strategies SET book_revision=book_revision+1 WHERE id=?", (strategy_id,))
    return True


def tick(strategy_id):
    from src.services import strategy_service as service

    row = service._get_row(strategy_id)
    revision = confirmed.snapshot_revision(strategy_id)
    state, positions, closes, complete = inputs(strategy_id, row, order_executor._fetch_mark_price(row["asset"]))
    state["allow_entries"] = bool(complete and row["status"] == "paper")
    delivered, bootstrap = confirmed.close_delivery(state, closes, complete=complete)
    payload = service._stateless_strategy_dict(row)
    payload.update(execution_state=state, closed_positions=delivered, closed_positions_bootstrap_complete=bootstrap)
    payload["execution_config"].update(initial_balance=state["reference_capital"], initial_cash_balance=state["reference_capital"])
    response = service.mangrove_ai_client().execution.evaluate_by_object(payload, persist=False, open_positions=positions)
    data = response.model_dump()
    if data.get("success") is not True or data.get("core_orders"):
        raise SdkError("Paper evaluation failed or returned unsupported core allocations; no fill booked.")
    eid = uuid.uuid4().hex
    intents = service._extract_order_intents(response, strategy_id=strategy_id, tick_id=eid)
    accepted = confirmed.save_risk(strategy_id, state, data, delivered, revision, complete=complete,
                                   state_field="paper_execution_state_json")
    if not accepted or not state["allow_entries"]:
        intents = confirmed.entries_only_removed(intents)
    exits = [intent for intent in intents if intent.action == "exit"]
    intents = exits or intents
    trade_log.log_evaluation(Evaluation(id=eid, strategy_id=strategy_id, timestamp=trade_log.now_utc(),
                                      duration_ms=0, status="ok" if accepted else "skipped", sdk_response=data, order_intents=intents,
                                      error_msg=None if accepted else "Incomplete paper history or a newer book prevented risk and entry commitment."))
    expected = (revision[0] + int(accepted), revision[1])
    for intent in intents:
        if intent.symbol not in {row["asset"], row["asset"].split("-")[0]}:
            raise SdkError("Paper intent asset does not match the strategy.")
        template = next((p for p in data.get("proposed_positions") or [] if p.get("id") == intent.engine_position_id), {})
        try:
            if fill(strategy_id, eid, intent, template, expected):
                expected = (expected[0], expected[1] + 1)
        except Exception as exc:
            if len(exits) < 2:
                raise
            _log.warning("paper.exit_batch.fill_failed", strategy_id=strategy_id,
                         position_id=intent.engine_position_id, error=str(exc))
