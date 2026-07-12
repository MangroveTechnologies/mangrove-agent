"""trade_log — SQLite writers for evaluations, trades, positions.

Every cron tick produces an Evaluation row. Every executed OrderIntent
produces a Trade row. Positions are derived from entry/exit trade pairs
and kept in sync via update_position().

These functions are pure DB I/O. They do NOT emit structured log events
(that's the caller's responsibility — strategy_service.tick, order_executor)
so the same data doesn't show up twice in different log streams.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Literal

from src.models.domain import Evaluation, OrderIntent, Position, Trade
from src.shared.db.sqlite import get_connection


def log_evaluation(evaluation: Evaluation) -> str:
    """Insert an Evaluation row. Returns the (possibly auto-filled) id."""
    if not evaluation.id:
        evaluation = evaluation.model_copy(update={"id": str(uuid.uuid4())})
    conn = get_connection()
    conn.execute(
        """INSERT INTO evaluations
           (id, strategy_id, timestamp, market_snapshot_json, sdk_response_json,
            order_intents_json, duration_ms, status, error_msg)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (
            evaluation.id,
            evaluation.strategy_id,
            evaluation.timestamp.isoformat(),
            json.dumps(evaluation.market_snapshot, default=str),
            json.dumps(evaluation.sdk_response, default=str),
            json.dumps([oi.model_dump() for oi in evaluation.order_intents], default=str),
            evaluation.duration_ms,
            evaluation.status,
            evaluation.error_msg,
        ),
    )
    conn.commit()
    return evaluation.id


def log_trade(trade: Trade) -> str:
    """Insert a Trade row. Returns the (possibly auto-filled) id."""
    if not trade.id:
        trade = trade.model_copy(update={"id": str(uuid.uuid4())})
    conn = get_connection()
    conn.execute(
        """INSERT INTO trades
           (id, strategy_id, evaluation_id, order_intent_json, mode, tx_hash,
            input_token, input_amount, output_token, output_amount,
            fill_price, fees_json, status, executed_at, confirmed_at, p_and_l)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            trade.id,
            trade.strategy_id,
            trade.evaluation_id,
            json.dumps(trade.order_intent.model_dump(), default=str),
            trade.mode,
            trade.tx_hash,
            trade.input_token,
            trade.input_amount,
            trade.output_token,
            trade.output_amount,
            trade.fill_price,
            json.dumps(trade.fees, default=str),
            trade.status,
            trade.executed_at.isoformat(),
            trade.confirmed_at.isoformat() if trade.confirmed_at else None,
            trade.p_and_l,
        ),
    )
    conn.commit()
    return trade.id


def update_position(position: Position) -> None:
    """Upsert a position. Matches on `position.id` (UUID)."""
    conn = get_connection()
    conn.execute(
        """INSERT INTO positions
           (id, strategy_id, asset, entry_trade_id, exit_trade_id,
            entry_price, entry_amount, entry_time, exit_price, exit_amount,
            exit_time, status, stop_loss, take_profit)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(id) DO UPDATE SET
             exit_trade_id=excluded.exit_trade_id,
             exit_price=excluded.exit_price,
             exit_amount=excluded.exit_amount,
             exit_time=excluded.exit_time,
             status=excluded.status,
             stop_loss=excluded.stop_loss,
             take_profit=excluded.take_profit""",
        (
            position.id,
            position.strategy_id,
            position.asset,
            position.entry_trade_id,
            position.exit_trade_id,
            position.entry_price,
            position.entry_amount,
            position.entry_time.isoformat(),
            position.exit_price,
            position.exit_amount,
            position.exit_time.isoformat() if position.exit_time else None,
            position.status,
            position.stop_loss,
            position.take_profit,
        ),
    )
    conn.commit()


def set_trade_pnl(trade_id: str, p_and_l: float) -> None:
    """Fill a trade's p_and_l after its position closes (exit trades)."""
    conn = get_connection()
    conn.execute("UPDATE trades SET p_and_l = ? WHERE id = ?", (p_and_l, trade_id))
    conn.commit()


# -- Query helpers -----------------------------------------------------------


def _row_to_position(r) -> Position:
    return Position(
        id=r["id"],
        strategy_id=r["strategy_id"],
        asset=r["asset"],
        entry_trade_id=r["entry_trade_id"],
        exit_trade_id=r["exit_trade_id"],
        entry_price=r["entry_price"],
        entry_amount=r["entry_amount"],
        entry_time=datetime.fromisoformat(r["entry_time"]),
        exit_price=r["exit_price"],
        exit_amount=r["exit_amount"],
        exit_time=datetime.fromisoformat(r["exit_time"]) if r["exit_time"] else None,
        status=r["status"],
        stop_loss=r["stop_loss"],
        take_profit=r["take_profit"],
    )


def get_position(position_id: str) -> Position | None:
    r = get_connection().execute(
        "SELECT * FROM positions WHERE id = ?", (position_id,)
    ).fetchone()
    return _row_to_position(r) if r else None


def find_open_position(strategy_id: str, asset: str) -> Position | None:
    """Oldest open position for (strategy, asset) — FIFO fallback for exits
    whose intent carries no engine_position_id."""
    r = get_connection().execute(
        """SELECT * FROM positions
           WHERE strategy_id = ? AND asset = ? AND status = 'open'
           ORDER BY entry_time ASC LIMIT 1""",
        (strategy_id, asset),
    ).fetchone()
    return _row_to_position(r) if r else None


def list_positions(strategy_id: str, status: str | None = None, limit: int = 50) -> list[Position]:
    if status:
        rows = get_connection().execute(
            """SELECT * FROM positions WHERE strategy_id = ? AND status = ?
               ORDER BY entry_time DESC LIMIT ?""",
            (strategy_id, status, limit),
        ).fetchall()
    else:
        rows = get_connection().execute(
            """SELECT * FROM positions WHERE strategy_id = ?
               ORDER BY entry_time DESC LIMIT ?""",
            (strategy_id, limit),
        ).fetchall()
    return [_row_to_position(r) for r in rows]


def _row_to_evaluation(r) -> Evaluation:
    return Evaluation(
        id=r["id"],
        strategy_id=r["strategy_id"],
        timestamp=datetime.fromisoformat(r["timestamp"]),
        market_snapshot=json.loads(r["market_snapshot_json"] or "{}"),
        sdk_response=json.loads(r["sdk_response_json"] or "{}"),
        order_intents=[OrderIntent(**oi) for oi in json.loads(r["order_intents_json"] or "[]")],
        duration_ms=r["duration_ms"],
        status=r["status"],
        error_msg=r["error_msg"],
    )


def _row_to_trade(r) -> Trade:
    return Trade(
        id=r["id"],
        strategy_id=r["strategy_id"],
        evaluation_id=r["evaluation_id"],
        order_intent=OrderIntent(**json.loads(r["order_intent_json"])),
        mode=r["mode"],
        tx_hash=r["tx_hash"],
        input_token=r["input_token"],
        input_amount=r["input_amount"],
        output_token=r["output_token"],
        output_amount=r["output_amount"],
        fill_price=r["fill_price"],
        fees=json.loads(r["fees_json"] or "{}"),
        status=r["status"],
        executed_at=datetime.fromisoformat(r["executed_at"]),
        confirmed_at=datetime.fromisoformat(r["confirmed_at"]) if r["confirmed_at"] else None,
        p_and_l=r["p_and_l"],
    )


def list_evaluations(strategy_id: str, limit: int = 50, offset: int = 0) -> list[Evaluation]:
    rows = get_connection().execute(
        """SELECT * FROM evaluations
           WHERE strategy_id = ?
           ORDER BY timestamp DESC
           LIMIT ? OFFSET ?""",
        (strategy_id, limit, offset),
    ).fetchall()
    return [_row_to_evaluation(r) for r in rows]


def list_trades(strategy_id: str, limit: int = 50, offset: int = 0) -> list[Trade]:
    rows = get_connection().execute(
        """SELECT * FROM trades
           WHERE strategy_id = ?
           ORDER BY executed_at DESC
           LIMIT ? OFFSET ?""",
        (strategy_id, limit, offset),
    ).fetchall()
    return [_row_to_trade(r) for r in rows]


def list_all_trades(
    limit: int = 50,
    strategy_id: str | None = None,
    mode: Literal["live", "paper"] | None = None,
) -> list[Trade]:
    sql = "SELECT * FROM trades WHERE 1=1"
    params: list = []
    if strategy_id is not None:
        sql += " AND strategy_id = ?"
        params.append(strategy_id)
    if mode is not None:
        sql += " AND mode = ?"
        params.append(mode)
    sql += " ORDER BY executed_at DESC LIMIT ?"
    params.append(limit)
    rows = get_connection().execute(sql, params).fetchall()
    return [_row_to_trade(r) for r in rows]


def now_utc() -> datetime:
    """Helper for callers who want a timezone-aware 'now' matching the schema."""
    return datetime.now(timezone.utc)
