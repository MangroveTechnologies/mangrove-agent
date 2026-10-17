"""Offline broker tests: real journal/SQLite, injected broker and Mangrove I/O."""

import asyncio
import json
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.config import app_config
from src.models.domain import Evaluation, OrderIntent
from src.services import broker_execution as execution
from src.services import broker_store as store
from src.services import strategy_service, trade_log
from src.shared.db import sqlite as db
from src.shared.errors import SdkError


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    db.reset_connection()
    monkeypatch.setattr(app_config, "DB_PATH", str(tmp_path / "broker.db"))
    db.init_db()
    with store.connection() as conn:
        for sid in ("s1", "s2"):
            conn.execute(
                """INSERT INTO strategies(id,mangrove_id,name,asset,timeframe,status,entry_json,
                exit_json,execution_config_json,created_at,updated_at,evaluation_lane)
                VALUES (?,?,?,'BTC-USD','1h','live','[]','[]','{}',?,?,'stateless')""",
                (sid, "mg-" + sid, sid, store.now(), store.now()),
            )
            conn.execute(
                """INSERT INTO broker_routes
                (strategy_id,venue,account_number,rhs_account_number,asset_class,capital,max_order_usd,slippage_pct,created_at)
                VALUES (?,'robinhood','eligible','numeric','crypto','1000','500','0.0025',?)""",
                (sid, store.now()),
            )
        conn.execute("UPDATE broker_routes SET asset_class='equity' WHERE strategy_id='s2'")
    trade_log.log_evaluation(
        Evaluation(
            id="eval",
            strategy_id="s1",
            timestamp=trade_log.now_utc(),
            duration_ms=0,
            status="ok",
            sdk_response={
                "execution_state": {"bar_index": 1, "risk_state": {"daily_trade_counter": {"2026-10-15": 1}}}
            },
        )
    )
    trade_log.log_evaluation(
        Evaluation(id="eval2", strategy_id="s2", timestamp=trade_log.now_utc(), duration_ms=0, status="ok")
    )
    yield
    db.reset_connection()


def intent(side="buy", qty=2, order="entry", position_id="p1"):
    return OrderIntent(
        action="enter" if side == "buy" else "exit",
        side=side,
        symbol="BTC",
        amount=qty,
        ref_price=100,
        engine_position_id=position_id,
        engine_order_id=order,
    )


def reserve(side="buy", qty=2, oid="local", key="entry", sid="s1", position_id="p1"):
    return store.reserve(
        sid,
        "eval" if sid == "s1" else "eval2",
        intent(side, qty, key, position_id),
        {"quantity": str(qty)},
        {"id": position_id, "asset": "BTC-USD", "position_size": qty, "orders": [{"position_size": qty, "price": 90}]},
        oid,
    )


def snapshot(qty="1", price="100", state="partially_filled", side="buy", oid="local", net=None, executions=None):
    result = dict(
        id="broker-" + oid,
        ref_id=oid,
        currency_code="BTC",
        symbol="BTC",
        side=side,
        state=state,
        cumulative_quantity=qty,
        average_price=price,
        fees="0",
        net_rounded_executed_notional=net or str(Decimal(qty) * Decimal(price or 0)),
    )
    if executions is not None:
        result["executions"] = executions
    return result


def row(table):
    with store.connection() as conn:
        return dict(conn.execute("SELECT * FROM " + table).fetchone())


def test_partial_fill_repeat_cancel_and_exit(ledger):
    assert reserve()
    store.apply_snapshot("local", snapshot(net="101"), asset_class="crypto")
    store.apply_snapshot("local", snapshot(net="101"), asset_class="crypto")
    assert row("positions")["entry_amount"] == 1
    assert row("positions")["entry_price"] == 101
    with store.connection() as conn:
        assert conn.execute("SELECT count(*) FROM trades").fetchone()[0] == 1
    # A canceled remainder is not a second fill.
    store.apply_snapshot("local", snapshot(state="canceled", net="101"), asset_class="crypto")
    assert not store.orders(pending=True)
    state, positions = store.execution_inputs("s1", 110)
    assert state["cash_balance"] == 899
    assert state["account_value"] == 1009
    assert positions[0]["position_size"] == 1
    assert positions[0]["orders"][0]["position_size"] == 1
    assert state["risk_state"]["daily_trade_counter"] == {"2026-10-15": 1}
    assert reserve(side="sell", qty=1, oid="exit", key="exit")
    store.apply_snapshot(
        "exit", snapshot(qty="1", price="110", state="filled", side="sell", oid="exit", net="109"), asset_class="crypto"
    )
    assert row("positions")["status"] == "closed"
    state, positions = store.execution_inputs("s1", 110)
    assert state["cash_balance"] == 1008
    assert positions == []
    with store.connection() as conn:
        assert conn.execute("SELECT sum(p_and_l) FROM trades").fetchone()[0] == 8
        close_event = dict(conn.execute("SELECT * FROM broker_close_events").fetchone())
    assert close_event["event_id"] == "close:p1"
    assert close_event["position_id"] == "p1"
    assert close_event["asset"] == "BTC-USD"
    assert Decimal(close_event["realized_pnl"]) == Decimal("8")
    assert Decimal(close_event["fees"]) == Decimal("2")
    assert close_event["occurred_at"] is None
    assert close_event["observed_at"]
    assert [event["event_id"] for event in store.pending_confirmed_close_events("s1")] == ["close:p1"]


def test_confirmed_v2_outbox_orders_open_and_close_in_shared_sequence(ledger):
    with store.connection() as conn:
        conn.execute("UPDATE broker_routes SET external_execution_mode='confirmed_v2' WHERE strategy_id='s1'")
        conn.execute(
            "UPDATE strategies SET execution_state_json=? WHERE id='s1'",
            (json.dumps({"total_trades": 4, "risk_state": {"daily_trade_counter": {"2026-10-15": 2}}}),),
        )
    assert reserve(side="buy", qty=1, oid="entry", key="entry")
    store.apply_snapshot(
        "entry", snapshot(qty="1", price="100", state="filled", net="101", oid="entry"),
        asset_class="crypto",
    )
    store.apply_snapshot(
        "entry", snapshot(qty="1", price="100", state="filled", net="101", oid="entry"),
        asset_class="crypto",
    )
    assert reserve(side="sell", qty=1, oid="exit", key="exit")
    store.apply_snapshot(
        "exit", snapshot(qty="1", price="110", state="filled", side="sell", oid="exit", net="109"),
        asset_class="crypto",
    )
    events = store.pending_external_execution_events("s1")
    assert [(event["sequence"], event["kind"], event["event_id"]) for event in events] == [
        (1, "opened", "open:p1"), (2, "closed", "close:p1")
    ]
    assert events[0]["filled_quantity"] == "1"
    assert events[1]["realized_pnl"] == "8"
    assert events[1]["fees"] == "2"
    state, _ = store.execution_inputs("s1", 110)
    assert state["total_trades"] == 4
    assert state["risk_state"]["daily_trade_counter"] == {"2026-10-15": 2}
    assert "bar_index" not in state  # fills never import the modeled evaluation state


def test_confirmed_v2_ack_receipt_is_bound_to_broker_event(ledger):
    with store.connection() as conn:
        conn.execute("UPDATE broker_routes SET external_execution_mode='confirmed_v2' WHERE strategy_id='s1'")
    assert reserve(side="buy", qty=1, oid="entry", key="entry")
    store.apply_snapshot(
        "entry", snapshot(qty="1", price="100", state="filled", net="101", oid="entry"),
        asset_class="crypto",
    )
    state, _, revision, events, raw_state, raw_positions = store.execution_inputs(
        "s1", 100, include_revision=True, include_external_events=True
    )
    del state
    event = events[0]
    receipt = {
        "sequence": event["sequence"], "event_id": event["event_id"],
        "position_id": event["position_id"], "kind": event["kind"],
        "sha256": store._external_event_hash(event),
        "occurred_at": None, "accounting_at": "2026-10-16T23:00:00Z",
    }
    next_state = {"external_execution": {"applied_through": 1, "receipts": [receipt]}}
    wrong_receipt = {**receipt, "sha256": "0" * 64}
    with pytest.raises(SdkError, match="receipt does not match"):
        store.acknowledge_confirmed_close_events(
            "s1", [event["event_id"]], next_state, open_positions=[{"id": "p1", "open": True}],
            expected_state=raw_state, expected_open_positions=raw_positions,
            expected_applied_through=0, expected_broker_revision=revision,
            receipts=[wrong_receipt], submitted_events=events,
        )
    assert store.pending_external_execution_events("s1") == events
    store.acknowledge_confirmed_close_events(
        "s1", [event["event_id"]], next_state, open_positions=[{"id": "p1", "open": True}],
        expected_state=raw_state, expected_open_positions=raw_positions,
        expected_applied_through=0, expected_broker_revision=revision,
        receipts=[receipt], submitted_events=events,
    )
    assert store.pending_external_execution_events("s1") == []
    with store.connection() as conn:
        stored = json.loads(conn.execute(
            "SELECT receipt_json FROM broker_execution_events WHERE event_id='open:p1'"
        ).fetchone()[0])
    assert stored["sha256"] == receipt["sha256"]
    assert store.acknowledge_confirmed_close_events(
        "s1", [event["event_id"]], next_state, open_positions=[{"id": "p1", "open": True}],
        expected_state=raw_state, expected_open_positions=raw_positions,
        expected_applied_through=0, expected_broker_revision=revision,
        receipts=[receipt], submitted_events=events,
        reservation={
            "evaluation_id": "eval", "intent": intent(order="changed-key"), "request": {"quantity": "1"},
            "template": {"id": "p1"}, "ref_id": "must-not-be-reserved",
        },
    ) is False
    assert not any(item["id"] == "must-not-be-reserved" for item in store.orders("s1"))


def test_empty_confirmed_v2_ack_and_order_reservation_commit_together(ledger):
    with store.connection() as conn:
        conn.execute("UPDATE broker_routes SET external_execution_mode='confirmed_v2' WHERE strategy_id='s1'")
        conn.execute("UPDATE strategies SET status='live' WHERE id='s1'")
    _, _, revision, events, raw_state, raw_positions = store.execution_inputs(
        "s1", 100, include_revision=True, include_external_events=True
    )
    assert events == []
    reservation = {
        "evaluation_id": "eval", "intent": intent(), "request": {"quantity": "1"},
        "template": {"id": "p1", "asset": "BTC-USD"}, "ref_id": "reserved-before-submit",
    }
    saved = store.acknowledge_confirmed_close_events(
        "s1", [], {"bar_index": 1, "external_execution": {"applied_through": 0}},
        open_positions=[], expected_state=raw_state, expected_open_positions=raw_positions,
        expected_applied_through=0, expected_broker_revision=revision,
        receipts=[], submitted_events=[], reservation=reservation,
    )
    assert saved is True
    order = next(item for item in store.orders("s1") if item["id"] == "reserved-before-submit")
    assert order["status"] == "submitting"

    class NoBrokerOrder:
        async def account(self, *_args):
            return None

        async def find_order(self, *_args):
            return None

    # Simulate a process stop after the atomic commit and before the broker
    # HTTP call. Reconciliation marks the outcome uncertain and never retries.
    asyncio.run(execution.reconcile(NoBrokerOrder(), "s1"))
    order = next(item for item in store.orders("s1") if item["id"] == "reserved-before-submit")
    assert order["status"] == "uncertain"
    assert not store.reserve(
        "s1", "eval", intent(), {"quantity": "1"}, {"id": "p1"}, "retry-must-not-submit"
    )


def test_confirmed_v2_ack_rejects_changed_route_allocation(ledger):
    with store.connection() as conn:
        conn.execute("UPDATE broker_routes SET external_execution_mode='confirmed_v2' WHERE strategy_id='s1'")
    _, _, revision, _, raw_state, raw_positions = store.execution_inputs(
        "s1", 100, include_revision=True, include_external_events=True
    )
    with store.connection() as conn:
        conn.execute("UPDATE broker_routes SET max_order_usd='400' WHERE strategy_id='s1'")
    with pytest.raises(SdkError, match="Stale confirmed close acknowledgement"):
        store.acknowledge_confirmed_close_events(
            "s1", [], {"external_execution": {"applied_through": 0}}, open_positions=[],
            expected_state=raw_state, expected_open_positions=raw_positions,
            expected_applied_through=0, expected_broker_revision=revision,
            receipts=[], submitted_events=[],
        )


def test_failed_atomic_reservation_rolls_back_v2_ack(ledger):
    with store.connection() as conn:
        conn.execute("UPDATE broker_routes SET external_execution_mode='confirmed_v2' WHERE strategy_id='s1'")
        conn.execute("UPDATE strategies SET status='inactive' WHERE id='s1'")
    _, _, revision, _, raw_state, raw_positions = store.execution_inputs(
        "s1", 100, include_revision=True, include_external_events=True
    )
    reservation = {
        "evaluation_id": "eval", "intent": intent(), "request": {"quantity": "1"},
        "template": {"id": "p1"}, "ref_id": "inactive-must-not-reserve",
    }
    with pytest.raises(SdkError, match="could not commit together"):
        store.acknowledge_confirmed_close_events(
            "s1", [], {"bar_index": 9, "external_execution": {"applied_through": 0}},
            open_positions=[], expected_state=raw_state, expected_open_positions=raw_positions,
            expected_applied_through=0, expected_broker_revision=revision,
            receipts=[], submitted_events=[], reservation=reservation,
        )
    with store.connection() as conn:
        state = json.loads(conn.execute(
            "SELECT execution_state_json FROM strategies WHERE id='s1'"
        ).fetchone()[0] or "{}")
        assert conn.execute("SELECT count(*) FROM broker_orders WHERE id='inactive-must-not-reserve'").fetchone()[0] == 0
    assert state == {}


def test_v2_tick_sends_empty_envelope_and_commits_typed_empty_ack(ledger, monkeypatch):
    from src.services import portfolio_risk_service

    with store.connection() as conn:
        conn.execute("UPDATE broker_routes SET external_execution_mode='confirmed_v2' WHERE strategy_id='s1'")
        conn.execute("UPDATE strategies SET status='live' WHERE id='s1'")
    monkeypatch.setattr(execution, "reconcile", AsyncMock())
    monkeypatch.setattr(portfolio_risk_service, "check_before_live_execution", lambda: {"allowed": True})
    calls = {}

    def sdk_method(payload, *, persist, open_positions, external_execution):
        calls.update(payload=payload, persist=persist, open_positions=open_positions, request=external_execution)
        return SimpleNamespace(
            external_execution_ack={
                "contract_version": 2, "applied_through": 0, "receipts": [], "active_position_ids": [],
                "last_observed_at": external_execution["observed_at"],
            },
            model_dump=lambda: {
                "success": True,
                "execution_state": {"bar_index": 1, "external_execution": {"applied_through": 0}},
                "open_positions": [], "position_templates": [],
                "external_execution_disposition": "evaluated", "new_orders": [],
            },
        )

    monkeypatch.setattr(execution, "_require_external_execution_sdk", lambda: sdk_method)

    class QuoteVenue:
        async def quote(self, *_args):
            return {"ask_price": "101", "bid_price": "99"}

    asyncio.run(execution._tick(QuoteVenue(), "s1"))
    assert calls["persist"] is False
    assert calls["payload"]["execution_state"]["total_trades"] == 0
    assert calls["request"]["contract_version"] == 2
    assert calls["request"]["events"] == []
    assert calls["request"]["observed_at"].endswith("Z")
    with store.connection() as conn:
        saved = json.loads(conn.execute(
            "SELECT execution_state_json FROM strategies WHERE id='s1'"
        ).fetchone()[0])
        count = conn.execute("SELECT count(*) FROM evaluations WHERE strategy_id='s1'").fetchone()[0]
    assert saved["external_execution"]["applied_through"] == 0
    assert saved["bar_index"] == 1
    assert count == 2


def test_v2_confirmed_positions_preserve_hosted_entry_index_and_actual_size():
    confirmed = [{
        "id": "p1", "asset": "BTC-USD", "position_size": 0.25,
        "entry_price": 101.0, "cost": 25.25, "entry_timestamp": "2026-10-16T10:00:00Z", "open": True,
    }]
    hosted = [{**confirmed[0], "position_size": 0.5, "entry_bar_index": 42, "orders": [{"price": 90}]}]
    result = execution._confirmed_position_view(hosted, confirmed)
    assert result[0]["position_size"] == 0.25
    assert result[0]["entry_bar_index"] == 42
    assert result[0]["orders"] == [{"price": 90}]
    with pytest.raises(SdkError, match="do not match broker-confirmed"):
        execution._confirmed_position_view([], confirmed)


def test_v2_execution_rejects_sdk_without_opt_in_parameter(monkeypatch):
    from src.shared.clients import mangrove

    def old_evaluate_by_object(payload, *, persist=False, open_positions=None):
        return None

    monkeypatch.setattr(
        mangrove, "mangrove_ai_client",
        lambda: SimpleNamespace(execution=SimpleNamespace(evaluate_by_object=old_evaluate_by_object)),
    )
    with pytest.raises(SdkError, match="does not support confirmed execution feedback v2"):
        execution._require_external_execution_sdk()


def test_confirmed_close_outbox_is_atomic_durable_and_acknowledged_with_state(ledger):
    assert reserve(side="buy", qty=1, oid="entry", key="entry")
    store.apply_snapshot("entry", snapshot(qty="1", price="100", state="filled", net="101", oid="entry"), asset_class="crypto")
    assert reserve(side="sell", qty=1, oid="exit", key="exit")

    partial = snapshot(qty="0.4", price="110", side="sell", oid="exit", net="43.56")
    store.apply_snapshot("exit", partial, asset_class="crypto")
    store.apply_snapshot("exit", partial, asset_class="crypto")
    assert store.pending_confirmed_close_events("s1") == []
    final = snapshot(qty="1", price="110", state="filled", side="sell", oid="exit", net="109")
    store.apply_snapshot("exit", final, asset_class="crypto")
    store.apply_snapshot("exit", final, asset_class="crypto")
    events = store.pending_confirmed_close_events("s1")
    assert len(events) == 1
    first = events[0]
    assert first["sequence"] == 1
    assert first["event_id"] == "close:p1"
    assert first["position_id"] == "p1"
    assert first["asset"] == "BTC-USD"
    assert Decimal(first["realized_pnl"]) == Decimal("8.00")
    assert Decimal(first["fees"]) == Decimal("2")
    assert first["occurred_at"] is None

    # A process restart reopens the same durable outbox with the fill and risk
    # acknowledgement still pending; no model or broker call is needed.
    db.reset_connection()
    db.init_db()
    assert store.pending_confirmed_close_events("s1") == events

    prior_positions = [{"id": "p1", "open": True}]
    with store.connection() as conn:
        conn.execute(
            "UPDATE strategies SET open_positions_json=? WHERE id='s1'",
            (json.dumps(prior_positions),),
        )
        prior = conn.execute(
            "SELECT execution_state_json,open_positions_json FROM strategies WHERE id='s1'"
        ).fetchone()
    prior_state = json.loads(prior["execution_state_json"] or "{}")
    prior_positions = json.loads(prior["open_positions_json"] or "[]")
    broker_revision = store.execution_inputs("s1", 110, include_revision=True)[2]
    next_state = {
        "risk_state": {"daily_trade_counter": {"2026-10-16": 1}},
        "external_execution": {"applied_through": 1},
    }
    store.acknowledge_confirmed_close_events(
        "s1", [first["event_id"]], next_state,
        open_positions=[], expected_state=prior_state,
        expected_open_positions=prior_positions, expected_applied_through=0,
        expected_broker_revision=broker_revision,
    )
    with store.connection() as conn:
        saved = json.loads(conn.execute(
            "SELECT execution_state_json FROM strategies WHERE id='s1'"
        ).fetchone()[0])
        saved_positions = json.loads(conn.execute(
            "SELECT open_positions_json FROM strategies WHERE id='s1'"
        ).fetchone()[0])
        acked = conn.execute(
            "SELECT acknowledged_at FROM broker_close_events WHERE event_id=?",
            (first["event_id"],),
        ).fetchone()[0]
    assert saved == next_state
    assert saved_positions == []
    assert acked
    assert store.pending_confirmed_close_events("s1") == []
    # Retrying the same acknowledgement after restart is idempotent.
    store.acknowledge_confirmed_close_events(
        "s1", [first["event_id"]], next_state,
        open_positions=[], expected_state=prior_state,
        expected_open_positions=prior_positions, expected_applied_through=0,
        expected_broker_revision=broker_revision,
    )


def test_partial_sell_fills_emit_one_net_position_close_outcome(ledger):
    assert reserve(side="buy", qty=1, oid="entry", key="entry")
    store.apply_snapshot("entry", snapshot(qty="1", price="100", state="filled", net="101", oid="entry"), asset_class="crypto")
    assert reserve(side="sell", qty=1, oid="exit", key="exit")

    # The first partial sell is profitable, but the remaining amount later
    # closes at a loss. No risk event is emitted until the whole position is flat.
    store.apply_snapshot(
        "exit", snapshot(qty="0.4", price="110", side="sell", oid="exit", net="43.56"),
        asset_class="crypto",
    )
    assert store.pending_confirmed_close_events("s1") == []
    # A database restart between sell fills does not require a separate
    # accumulator; earlier deltas remain represented by cumulative order data.
    db.reset_connection()
    db.init_db()
    store.apply_snapshot(
        "exit", snapshot(qty="1", price="98", state="filled", side="sell", oid="exit", net="97"),
        asset_class="crypto",
    )
    store.apply_snapshot(
        "exit", snapshot(qty="1", price="98", state="filled", side="sell", oid="exit", net="97"),
        asset_class="crypto",
    )

    events = store.pending_confirmed_close_events("s1")
    assert len(events) == 1
    assert events[0]["event_id"] == "close:p1"
    assert events[0]["sequence"] == 1
    assert Decimal(events[0]["realized_pnl"]) == Decimal("-4.00")
    assert Decimal(events[0]["fees"]) == Decimal("2.00")
    with store.connection() as conn:
        assert conn.execute("SELECT count(*) FROM broker_close_events").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM trades WHERE status='confirmed'").fetchone()[0] == 3


def test_missing_preexisting_fill_provenance_fails_closed_atomically(ledger):
    assert reserve(side="buy", qty=1, oid="entry", key="entry")
    store.apply_snapshot("entry", snapshot(qty="1", price="100", state="filled", net="101", oid="entry"), asset_class="crypto")
    assert reserve(side="sell", qty=1, oid="exit", key="exit")
    with store.connection() as conn:
        intent_data = json.loads(conn.execute(
            "SELECT intent_json FROM broker_orders WHERE id='entry'"
        ).fetchone()[0])
        intent_data["engine_position_id"] = "missing-position"
        conn.execute(
            "UPDATE broker_orders SET intent_json=? WHERE id='entry'",
            (json.dumps(intent_data),),
        )

    with pytest.raises(SdkError, match="complete broker fill provenance"):
        store.apply_snapshot(
            "exit",
            snapshot(qty="1", price="110", state="filled", side="sell", oid="exit", net="109"),
            asset_class="crypto",
        )
    assert row("positions")["status"] == "open"
    with store.connection() as conn:
        assert conn.execute("SELECT status FROM broker_orders WHERE id='exit'").fetchone()[0] == "submitting"
        assert conn.execute("SELECT count(*) FROM broker_events WHERE order_id='exit'").fetchone()[0] == 0
    assert store.pending_confirmed_close_events("s1") == []


def test_close_event_ack_rejects_unknown_ids_without_partial_state_write(ledger):
    assert reserve(side="buy", qty=1, oid="entry", key="entry")
    store.apply_snapshot("entry", snapshot(qty="1", price="100", state="filled", net="101", oid="entry"), asset_class="crypto")
    assert reserve(side="sell", qty=1, oid="exit", key="exit")
    store.apply_snapshot(
        "exit", snapshot(qty="1", price="110", state="filled", side="sell", oid="exit", net="109"),
        asset_class="crypto",
    )
    with store.connection() as conn:
        before = conn.execute(
            "SELECT execution_state_json,open_positions_json FROM strategies WHERE id='s1'"
        ).fetchone()
    before_state = json.loads(before["execution_state_json"] or "{}")
    before_positions = json.loads(before["open_positions_json"] or "[]")
    broker_revision = store.execution_inputs("s1", 110, include_revision=True)[2]
    with pytest.raises(SdkError, match="unknown event"):
        store.acknowledge_confirmed_close_events(
            "s1", ["close:p1", "missing"], {"risk_state": {"bad": True}, "external_execution": {"applied_through": 1}},
            open_positions=[], expected_state=before_state, expected_open_positions=before_positions,
            expected_applied_through=0,
            expected_broker_revision=broker_revision,
        )
    with store.connection() as conn:
        state = json.loads(conn.execute(
            "SELECT execution_state_json FROM strategies WHERE id='s1'"
        ).fetchone()[0])
        acked = conn.execute(
            "SELECT acknowledged_at FROM broker_close_events WHERE event_id='close:p1'"
        ).fetchone()[0]
    assert state == before_state
    assert acked is None
    assert [event["event_id"] for event in store.pending_confirmed_close_events("s1")] == ["close:p1"]


def test_close_ack_requires_contiguous_cursor_and_preserves_latched_halt(ledger):
    for position_id in ("p1", "p2"):
        entry_id = f"entry-{position_id}"
        assert reserve(side="buy", qty=1, oid=entry_id, key=entry_id, position_id=position_id)
        store.apply_snapshot(
            entry_id,
            snapshot(qty="1", price="100", state="filled", net="101", oid=entry_id),
            asset_class="crypto",
        )
    for position_id in ("p1", "p2"):
        exit_id = f"exit-{position_id}"
        assert reserve(side="sell", qty=1, oid=exit_id, key=exit_id, position_id=position_id)
        store.apply_snapshot(
            exit_id,
            snapshot(qty="1", price="110", state="filled", side="sell", oid=exit_id, net="109"),
            asset_class="crypto",
        )
    events = store.pending_confirmed_close_events("s1")
    assert [item["sequence"] for item in events] == [1, 2]

    with store.connection() as conn:
        conn.execute(
            "UPDATE strategies SET execution_state_json=? WHERE id='s1'",
            (json.dumps({"risk_state": {"halt": {"reason": "prior_halt"}, "hard_floor_tripped": True}}),),
        )
    prior_state = {"risk_state": {"halt": {"reason": "prior_halt"}, "hard_floor_tripped": True}}
    broker_revision = store.execution_inputs("s1", 110, include_revision=True)[2]
    with pytest.raises(SdkError, match="sequence gap"):
        store.acknowledge_confirmed_close_events(
            "s1", [events[1]["event_id"]],
            {"risk_state": {}, "external_execution": {"applied_through": 2}},
            open_positions=[], expected_state=prior_state, expected_open_positions=[],
            expected_applied_through=0,
            expected_broker_revision=broker_revision,
        )

    stale_state = prior_state | {"bar_index": 9}
    with store.connection() as conn:
        conn.execute(
            "UPDATE strategies SET execution_state_json=? WHERE id='s1'",
            (json.dumps(stale_state),),
        )
    with pytest.raises(SdkError, match="Stale confirmed close acknowledgement"):
        store.acknowledge_confirmed_close_events(
            "s1", [events[0]["event_id"], events[1]["event_id"]],
            {"external_execution": {"applied_through": 2}},
            open_positions=[], expected_state=prior_state, expected_open_positions=[],
            expected_applied_through=0,
            expected_broker_revision=broker_revision,
        )

    next_state = {
        "bar_index": 9,
        "risk_state": {},
        "external_execution": {"applied_through": 2},
    }
    store.acknowledge_confirmed_close_events(
        "s1", [events[0]["event_id"], events[1]["event_id"]], next_state,
        open_positions=[], expected_state=stale_state, expected_open_positions=[],
        expected_applied_through=0,
        expected_broker_revision=broker_revision,
    )
    with store.connection() as conn:
        saved = json.loads(conn.execute(
            "SELECT execution_state_json FROM strategies WHERE id='s1'"
        ).fetchone()[0])
    assert saved["risk_state"]["halt"] == {"reason": "prior_halt"}
    assert saved["risk_state"]["hard_floor_tripped"] is True
    assert saved["external_execution"]["applied_through"] == 2


def test_ack_rejects_late_partial_fill_even_when_engine_state_is_unchanged(ledger):
    assert reserve(side="buy", qty=1, oid="entry-p1", key="entry-p1", position_id="p1")
    store.apply_snapshot(
        "entry-p1", snapshot(qty="1", price="100", state="filled", net="101", oid="entry-p1"),
        asset_class="crypto",
    )
    assert reserve(side="sell", qty=1, oid="exit-p1", key="exit-p1", position_id="p1")
    store.apply_snapshot(
        "exit-p1", snapshot(qty="1", price="110", state="filled", side="sell", oid="exit-p1", net="109"),
        asset_class="crypto",
    )
    event = store.pending_confirmed_close_events("s1")[0]
    with store.connection() as conn:
        row_before = conn.execute(
            "SELECT execution_state_json,open_positions_json FROM strategies WHERE id='s1'"
        ).fetchone()
    expected_state = json.loads(row_before["execution_state_json"] or "{}")
    expected_positions = json.loads(row_before["open_positions_json"] or "[]")
    expected_revision = store.execution_inputs("s1", 110, include_revision=True)[2]

    # A second entry fill reuses an evaluation carrying the identical engine
    # state, but changes confirmed broker holdings after the response was made.
    assert reserve(side="buy", qty=1, oid="entry-p2", key="entry-p2", position_id="p2")
    store.apply_snapshot(
        "entry-p2", snapshot(qty="0.5", price="100", state="partially_filled", net="50", oid="entry-p2"),
        asset_class="crypto",
    )
    with store.connection() as conn:
        row_after = conn.execute(
            "SELECT execution_state_json,open_positions_json FROM strategies WHERE id='s1'"
        ).fetchone()
    assert json.loads(row_after["execution_state_json"] or "{}") == expected_state
    assert json.loads(row_after["open_positions_json"] or "[]") == expected_positions

    with pytest.raises(SdkError, match="broker or strategy state changed"):
        store.acknowledge_confirmed_close_events(
            "s1", [event["event_id"]],
            expected_state | {"external_execution": {"applied_through": 1}},
            open_positions=expected_positions,
            expected_state=expected_state,
            expected_open_positions=expected_positions,
            expected_applied_through=0,
            expected_broker_revision=expected_revision,
        )
    assert [item["event_id"] for item in store.pending_confirmed_close_events("s1")] == [event["event_id"]]


def test_close_event_sequences_are_contiguous_per_strategy(ledger):
    def close_position(position_id, *, sid="s1", equity=False):
        entry_id = f"entry-{sid}-{position_id}"
        exit_id = f"exit-{sid}-{position_id}"
        assert reserve(
            side="buy", qty=1, oid=entry_id, key=entry_id,
            sid=sid, position_id=position_id,
        )
        store.apply_snapshot(
            entry_id,
            snapshot(qty="1", price="100", state="filled", net="100", oid=entry_id),
            asset_class="equity" if equity else "crypto",
        )
        assert reserve(
            side="sell", qty=1, oid=exit_id, key=exit_id,
            sid=sid, position_id=position_id,
        )
        store.apply_snapshot(
            exit_id,
            snapshot(qty="1", price="110", state="filled", side="sell", oid=exit_id, net="110"),
            asset_class="equity" if equity else "crypto",
        )

    close_position("p1")
    close_position("p3", sid="s2", equity=True)
    close_position("p2")

    assert [event["sequence"] for event in store.pending_confirmed_close_events("s1")] == [1, 2]
    assert [event["sequence"] for event in store.pending_confirmed_close_events("s2")] == [1]


def test_snapshot_journal_preserves_fill_times_and_idempotently_records_receipt(ledger):
    reserve()
    executions = [
        {
            "id": "execution-1",
            "price": "100",
            "effective_price": "101",
            "quantity": "0.4",
            "notional": "40.40",
            "timestamp": "2026-10-15T23:58:33.123456Z",
        },
        {
            "id": "execution-2",
            "price": "100",
            "effective_price": "101",
            "quantity": "0.6",
            "notional": "60.60",
            "timestamp": "2026-10-16T00:00:11.654321+00:00",
        },
    ]
    data = snapshot(net="101", executions=executions)
    store.apply_snapshot("local", data, asset_class="crypto")
    store.apply_snapshot("local", data, asset_class="crypto")

    with store.connection() as conn:
        events = conn.execute("SELECT * FROM broker_events").fetchall()
        assert len(events) == 1
        receipt = dict(events[0])
        assert conn.execute("SELECT count(*) FROM trades").fetchone()[0] == 1
    saved = json.loads(receipt["snapshot_json"])
    assert [item["timestamp"] for item in saved["executions"]] == [
        "2026-10-15T23:58:33.123456Z",
        "2026-10-16T00:00:11.654321+00:00",
    ]
    assert receipt["observed_at"] == receipt["created_at"]
    assert receipt["observed_at"] not in {item["timestamp"] for item in saved["executions"]}
    assert receipt["snapshot_key"]


def test_unfilled_crypto_null_fees(ledger):
    reserve()
    data = snapshot(qty="0", price=None, state="queued")
    data["fees"] = None
    store.apply_snapshot("local", data, asset_class="crypto")
    assert row("broker_orders")["status"] == "open"
    assert store.execution_inputs("s1", 100)[0]["cash_balance"] == 1000


def test_duplicate_intent_and_single_pending(ledger):
    reserve()
    assert not reserve(oid="duplicate")
    import sqlite3

    with pytest.raises(sqlite3.IntegrityError):
        reserve(oid="other", key="other")
    db.reset_connection()  # Process restart must preserve reservations.
    assert len(store.orders(pending=True)) == 1


@pytest.mark.parametrize(
    "change",
    [
        {"side": "sell"},
        {"currency_code": "ETH", "symbol": "ETH"},
        {"ref_id": "other"},
        {"cumulative_quantity": "3"},
        {"state": "novel_state"},
        {"average_price": "NaN"},
    ],
)
def test_invalid_snapshot_is_atomic(ledger, change):
    reserve()
    with pytest.raises(SdkError):
        store.apply_snapshot("local", snapshot() | change, asset_class="crypto")
    with store.connection() as conn:
        assert conn.execute("SELECT count(*) FROM trades").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM positions").fetchone()[0] == 0
    assert row("broker_orders")["status"] == "submitting"


@pytest.mark.parametrize("executions", [
    [None],
    [{"price": "100"}],
    [{"id": "execution-1", "price": "100"}],
])
def test_optional_execution_metadata_is_preserved_without_rejection(ledger, executions):
    reserve()
    data = snapshot(executions=executions)
    store.apply_snapshot("local", data, asset_class="crypto")
    with store.connection() as conn:
        receipt = conn.execute("SELECT snapshot_json FROM broker_events").fetchone()
    assert json.loads(receipt[0])["executions"] == executions
    assert row("broker_orders")["status"] == "partial"


def test_non_json_snapshot_is_rejected_atomically(ledger):
    reserve()
    data = snapshot()
    data["optional"] = float("nan")
    with pytest.raises(SdkError, match="non-JSON"):
        store.apply_snapshot("local", data, asset_class="crypto")
    with store.connection() as conn:
        assert conn.execute("SELECT count(*) FROM trades").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM positions").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM broker_events").fetchone()[0] == 0
    assert row("broker_orders")["status"] == "submitting"


def test_status_transition_receipts_are_distinct_and_same_status_deduplicates(ledger):
    reserve()
    data = snapshot(net="101")
    store.apply_snapshot("local", data, asset_class="crypto")
    cancel_snapshot = data | {"state": "pending_cancelled"}
    store.apply_snapshot("local", cancel_snapshot, asset_class="crypto")
    store.apply_snapshot("local", cancel_snapshot, asset_class="crypto")
    with store.connection() as conn:
        receipts = conn.execute(
            "SELECT status,snapshot_key FROM broker_events ORDER BY id"
        ).fetchall()
        assert len(receipts) == 2
        assert [r["status"] for r in receipts] == ["partial", "cancel_pending"]
        assert receipts[0]["snapshot_key"] == receipts[1]["snapshot_key"]


def test_regression_and_cross_strategy_position_rejected(ledger):
    reserve()
    store.apply_snapshot("local", snapshot(qty="2", state="filled"), asset_class="crypto")
    with pytest.raises(SdkError):
        store.apply_snapshot("local", snapshot(), asset_class="crypto")
    reserve(sid="s2", oid="other")
    with pytest.raises(SdkError, match="another strategy"):
        store.apply_snapshot("other", snapshot(oid="other"), asset_class="equity")
    assert row("positions")["entry_amount"] == 2


async def test_uncertain_submission_is_never_retried(ledger):
    reserve()
    venue = SimpleNamespace(account=AsyncMock(), find_order=AsyncMock(return_value=None))
    await execution.reconcile(venue, "s1")
    assert row("broker_orders")["status"] == "uncertain"
    await execution.reconcile(venue, "s1")
    assert row("broker_orders")["status"] == "uncertain"
    venue.find_order.return_value = snapshot(qty="2", state="filled")
    await execution.reconcile(venue, "s1")
    assert row("broker_orders")["status"] == "filled"
    assert row("positions")["entry_amount"] == 2


async def test_declined_approval_releases_pending(ledger):
    reserve()
    store.mark("local", "awaiting_approval", approval_id="approval")
    venue = SimpleNamespace(account=AsyncMock(), approval=AsyncMock(return_value={"state": "APPROVAL_STATE_EXPIRED"}))
    await execution.reconcile(venue, "s1")
    assert row("broker_orders")["status"] == "rejected"
    assert not store.orders(pending=True)


def test_paused_strategy_cannot_reserve(ledger):
    with store.connection() as conn:
        conn.execute("UPDATE strategies SET status='inactive' WHERE id='s1'")
    assert not reserve()


async def test_tick_uses_mangrove_stateless_then_broker_not_dex(ledger, monkeypatch):
    from src.services import order_executor, portfolio_risk_service
    from src.shared.clients import mangrove
    sdk = MagicMock()

    modeled = {
        "success": True,
        "execution_state": {"cash_balance": 800, "bar_index": 1},
        "open_positions": [{"id": "p1", "position_size": 2}],
        "new_orders": [
            {
                "order_id": "engine",
                "asset": "BTC-USD",
                "side": "enter_long",
                "status": "filled",
                "position_size": 2,
                "price": 100,
                "position_id": "p1",
            }
        ],
    }
    sdk.execution.evaluate_by_object.return_value = SimpleNamespace(
        new_orders=modeled["new_orders"], model_dump=lambda: modeled
    )
    monkeypatch.setattr(mangrove, "mangrove_ai_client", lambda: sdk)
    monkeypatch.setattr(portfolio_risk_service, "check_before_live_execution", lambda: {"allowed": True})
    dex = MagicMock()
    monkeypatch.setattr(order_executor, "execute_many", dex)
    venue = SimpleNamespace(
        quote=AsyncMock(return_value={"ask_price": "100", "bid_price": "100"}),
        prepare=AsyncMock(return_value=({"quantity": "2"}, Decimal(200))),
        balance=AsyncMock(return_value=Decimal(1000)),
        submit=AsyncMock(side_effect=TimeoutError("ambiguous network outcome")),
        account=AsyncMock(),
        find_order=AsyncMock(return_value=None),
    )
    with pytest.raises(TimeoutError):
        await execution._tick(venue, "s1")
    call = sdk.execution.evaluate_by_object.call_args
    assert call.kwargs == {"persist": False, "open_positions": []}
    assert call.args[0]["execution_state"]["cash_balance"] == 1000
    assert store.orders()[0]["status"] == "uncertain"
    assert store.execution_inputs("s1", 100)[1] == []
    await execution._tick(venue, "s1")
    assert venue.submit.await_count == 1
    assert sdk.execution.evaluate_by_object.call_count == 2
    dex.assert_not_called()


def test_broker_route_bypasses_dex_tick(ledger, monkeypatch):
    mocked = MagicMock()
    monkeypatch.setattr(execution, "tick", mocked)
    strategy_service.tick("s1")
    mocked.assert_called_once_with("s1")


async def test_configuration_requires_inactive_and_no_execution_history(ledger):
    allocation = {"asset_class": "crypto", "capital": "200", "max_order_usd": "100", "slippage_pct": "0.0025"}
    venue = SimpleNamespace(
        account=AsyncMock(
            return_value={"account_number": "eligible", "rhs_account_number": "numeric", "rhc_account_number": "crypto"}
        )
    )
    with pytest.raises(SdkError, match="draft or inactive"):
        await execution.configure(venue, "s1", allocation)
    reserve()
    with store.connection() as conn:
        conn.execute("UPDATE strategies SET status='inactive' WHERE id='s1'")
    with pytest.raises(SdkError, match="execution history"):
        await execution.configure(venue, "s1", allocation)


async def test_confirmed_v2_configuration_requires_capable_sdk_and_preserves_state(ledger, monkeypatch):
    from src.shared.clients import mangrove

    with store.connection() as conn:
        conn.execute(
            """INSERT INTO strategies(id,mangrove_id,name,asset,timeframe,status,entry_json,
            exit_json,execution_config_json,created_at,updated_at,evaluation_lane,execution_state_json,open_positions_json)
            VALUES ('s3','mg-s3','s3','BTC-USD','1h','inactive','[]','[]','{}',?,?, 'server','{}','[]')""",
            (store.now(), store.now()),
        )
    def evaluate_by_object(payload, *, persist, open_positions, external_execution=None):
        return None

    monkeypatch.setattr(
        mangrove, "mangrove_ai_client",
        lambda: SimpleNamespace(execution=SimpleNamespace(evaluate_by_object=evaluate_by_object)),
    )
    venue = SimpleNamespace(account=AsyncMock(return_value={
        "account_number": "eligible", "rhs_account_number": "numeric", "rhc_account_number": "crypto",
    }))
    await execution.configure(venue, "s3", {
        "asset_class": "crypto", "capital": "200", "max_order_usd": "100",
        "slippage_pct": "0.0025", "external_execution_mode": "confirmed_v2",
    })
    with store.connection() as conn:
        row = conn.execute(
            "SELECT evaluation_lane,execution_state_json,open_positions_json FROM strategies WHERE id='s3'"
        ).fetchone()
        route = conn.execute(
            "SELECT external_execution_mode FROM broker_routes WHERE strategy_id='s3'"
        ).fetchone()
    assert row["evaluation_lane"] == "stateless"
    assert row["execution_state_json"] == "{}"
    assert row["open_positions_json"] == "[]"
    assert route["external_execution_mode"] == "confirmed_v2"


def test_strategy_input_preserves_updated_brackets(ledger):
    reserve()
    store.apply_snapshot("local", snapshot(qty="2", state="filled"), asset_class="crypto")
    with store.connection() as conn:
        conn.execute(
            "UPDATE strategies SET open_positions_json=? WHERE id=?",
            (json.dumps([{"id": "p1", "orders": [{"price": 95}]}]), "s1"),
        )
    assert store.execution_inputs("s1", 105)[1][0]["orders"][0]["price"] == 95


def test_fractional_fills_keep_exact_holdings_and_cash(ledger):
    reserve(qty=0.3)
    store.apply_snapshot("local", snapshot(qty="0.1"), asset_class="crypto")
    store.apply_snapshot("local", snapshot(qty="0.3", state="filled"), asset_class="crypto")
    reserve(side="sell", qty=0.2, oid="exit1", key="exit1")
    store.apply_snapshot("exit1", snapshot(qty="0.2", state="filled", side="sell", oid="exit1"), asset_class="crypto")
    assert row("broker_holdings")["quantity"] == "0.1"
    reserve(side="sell", qty=0.1, oid="exit2", key="exit2")
    store.apply_snapshot("exit2", snapshot(qty="0.1", state="filled", side="sell", oid="exit2"), asset_class="crypto")
    assert row("broker_holdings")["quantity"] == "0.0"
    assert store.execution_inputs("s1", 100)[0]["cash_balance"] == 1000


def test_actual_loss_does_not_silently_preserve_modeled_win(ledger):
    reserve(qty=1)
    store.apply_snapshot("local", snapshot(qty="1", state="filled"), asset_class="crypto")
    reserve(side="sell", qty=1, oid="exit", key="exit")
    store.apply_snapshot(
        "exit", snapshot(qty="1", price="99.9", state="filled", side="sell", oid="exit"), asset_class="crypto"
    )
    state, _ = store.execution_inputs("s1", 100)
    assert state["risk_state"]["hard_floor_tripped"] is True
    assert state["halt"]["reason"] == "broker_loss_diverged_from_model"


@pytest.mark.parametrize("halt_shape", ["nested", "top_level", "flag_only"])
def test_delayed_first_fill_preserves_newer_risk_halt_across_restart(ledger, halt_shape):
    reserve()
    # An unfilled order remains open while a newer evaluation trips the floor.
    store.apply_snapshot("local", snapshot(qty="0", price=None, state="queued"), asset_class="crypto")
    halt = {"reason": "capital_floor", "halted_at": "2026-10-16T12:00:00Z", "initial_capital": 1000}
    current = {"risk_state": {"hard_floor_tripped": True}}
    if halt_shape == "nested":
        current["risk_state"]["halt"] = halt
    elif halt_shape == "top_level":
        current["halt"] = halt
    with store.connection() as conn:
        conn.execute("UPDATE strategies SET execution_state_json=? WHERE id='s1'", (json.dumps(current),))
    db.reset_connection()
    # The first fill uses the old order's pre-halt evaluation, then another
    # partial fill arrives after a second restart. Neither may clear the latch.
    for qty in ("1", "2"):
        data = snapshot(qty=qty, state="filled" if qty == "2" else "partially_filled")
        store.apply_snapshot("local", data, asset_class="crypto")
        store.apply_snapshot("local", data, asset_class="crypto")
        db.reset_connection()
        state, positions = store.execution_inputs("s1", 100)
        assert state["risk_state"]["hard_floor_tripped"] is True
        if halt_shape != "flag_only":
            assert state["halt"] == halt
            assert state["risk_state"]["halt"] == halt
        assert positions[0]["position_size"] == float(qty)
        assert state["cash_balance"] == 1000 - 100 * float(qty)
    with store.connection() as conn:
        assert conn.execute("SELECT count(*) FROM trades").fetchone()[0] == 2


async def test_no_intent_evaluation_cannot_clear_persisted_halt(ledger, monkeypatch):
    from src.services import portfolio_risk_service
    from src.shared.clients import mangrove

    halt = {"reason": "broker_loss_diverged_from_model"}
    current = {"risk_state": {"hard_floor_tripped": True, "halt": halt}, "halt": halt}
    with store.connection() as conn:
        conn.execute("UPDATE strategies SET execution_state_json=? WHERE id='s1'", (json.dumps(current),))
    modeled = {"success": True, "execution_state": {"bar_index": 2, "risk_state": {
        "hard_floor_tripped": False, "halt": None}}, "open_positions": [], "new_orders": []}
    sdk = MagicMock()
    sdk.execution.evaluate_by_object.return_value = SimpleNamespace(new_orders=[], model_dump=lambda: modeled)
    monkeypatch.setattr(mangrove, "mangrove_ai_client", lambda: sdk)
    monkeypatch.setattr(portfolio_risk_service, "check_before_live_execution", lambda: {"allowed": True})
    venue = SimpleNamespace(account=AsyncMock(), quote=AsyncMock(return_value={"ask_price": "100", "bid_price": "100"}),
                            prepare=AsyncMock(), submit=AsyncMock())
    await execution._tick(venue, "s1")
    db.reset_connection()
    state, _ = store.execution_inputs("s1", 100)
    assert state["bar_index"] == 2
    assert state["risk_state"]["hard_floor_tripped"] is True
    assert state["risk_state"]["halt"] == halt
    assert state["halt"] == halt
    venue.prepare.assert_not_awaited()
    venue.submit.assert_not_awaited()


def test_later_loss_fill_keeps_original_halt_and_records_exit(ledger):
    reserve(qty=2)
    store.apply_snapshot("local", snapshot(qty="2", state="filled"), asset_class="crypto")
    halt = {"reason": "capital_floor", "initial_capital": 1000, "closed_equity": 800}
    store.save_evaluation_state("s1", {"risk_state": {"hard_floor_tripped": True, "halt": halt}}, [])
    assert reserve(side="sell", qty=2, oid="exit", key="exit")
    store.apply_snapshot("exit", snapshot(qty="1", price="101", side="sell", oid="exit"), asset_class="crypto")
    db.reset_connection()
    # The remaining unit fills at 97 (cumulative average 99), creating a loss.
    store.apply_snapshot(
        "exit", snapshot(qty="2", price="99", state="filled", side="sell", oid="exit"), asset_class="crypto"
    )
    state, positions = store.execution_inputs("s1", 99)
    assert state["halt"] == halt
    assert state["risk_state"]["halt"] == halt
    assert state["risk_state"]["hard_floor_tripped"] is True
    assert positions == []
    assert state["cash_balance"] == 998


def test_new_halt_is_persisted_without_freezing_unlatched_cooldowns(ledger):
    store.save_evaluation_state("s1", {"bar_index": 4, "risk_state": {"cooldown_remaining": {"BTC-USD": 3}}}, [])
    store.save_evaluation_state("s1", {"bar_index": 5, "risk_state": {"cooldown_remaining": {"BTC-USD": 2}}}, [])
    state, _ = store.execution_inputs("s1", 100)
    assert state["bar_index"] == 5
    assert state["risk_state"]["cooldown_remaining"] == {"BTC-USD": 2}
    assert not state["risk_state"].get("hard_floor_tripped")
    halt = {"reason": "capital_floor"}
    store.save_evaluation_state("s1", {"risk_state": {"halt": halt}}, [])
    state, _ = store.execution_inputs("s1", 100)
    assert state["halt"] == halt
    assert state["risk_state"]["hard_floor_tripped"] is True


async def test_mcp_status_dispatches_without_nested_event_loop(ledger, monkeypatch):
    from src.mcp.server import create_mcp_server

    async def probe(venue):
        return {"connected": True}

    async def fake_session_call(operation, *args):
        return await operation(None, *args)

    monkeypatch.setattr(execution, "inspect", probe)
    monkeypatch.setattr(execution, "_with_venue", fake_session_call)
    server = create_mcp_server()
    tool = server._tool_manager._tools["agent_robinhood_status"]
    denied = json.loads(await tool.run({}))
    assert denied["code"] == "AUTH_INVALID_API_KEY"
    result = json.loads(await tool.run({"api_key": "test-key-1"}))
    assert result == {"connected": True}


async def test_mcp_activation_runs_on_worker_thread(ledger, monkeypatch):
    import asyncio

    from src.mcp.server import create_mcp_server

    def update(*args):
        with pytest.raises(RuntimeError):
            asyncio.get_running_loop()
        return SimpleNamespace(model_dump=lambda **kw: {"status": "live"})

    monkeypatch.setattr(strategy_service, "update_status", update)
    tool = create_mcp_server()._tool_manager._tools["agent_update_strategy_status"]
    result = json.loads(
        await tool.run({"strategy_id": "s1", "status": "live", "confirm": True, "api_key": "test-key-1"})
    )
    assert result == {"status": "live"}


async def test_mcp_evaluation_runs_broker_tick_on_worker_thread(ledger, monkeypatch):
    import asyncio

    from src.mcp.server import create_mcp_server

    def tick(strategy_id):
        with pytest.raises(RuntimeError):
            asyncio.get_running_loop()
        assert strategy_id == "s1"

    monkeypatch.setattr(strategy_service, "get_strategy", lambda _: None)
    monkeypatch.setattr(strategy_service, "tick", tick)
    monkeypatch.setattr(trade_log, "list_evaluations", lambda *args, **kwargs: [])
    tool = create_mcp_server()._tool_manager._tools["agent_evaluate_strategy"]
    result = json.loads(await tool.run({"strategy_id": "s1", "api_key": "test-key-1"}))
    assert result["status"] == "no_evaluation_recorded"


async def test_strategy_reads_expose_broker_mandate_without_account_ids(ledger):
    from src.mcp.server import create_mcp_server

    expected = {
        "venue": "robinhood", "asset_class": "crypto", "capital": "1000",
            "max_order_usd": "500", "slippage_pct": "0.0025",
            "external_execution_mode": "legacy", "native_stop_loss_enabled": False,
    }
    assert strategy_service.get_strategy("s1").execution_venue.model_dump() == expected
    assert next(s for s in strategy_service.list_strategies() if s.id == "s1").execution_venue.model_dump() == expected
    server = create_mcp_server()
    for name, args in [("agent_get_strategy", {"strategy_id": "s1"}), ("agent_list_strategies", {})]:
        response = json.loads(await server._tool_manager._tools[name].run({**args, "api_key": "test-key-1"}))
        result = response if name == "agent_get_strategy" else next(s for s in response if s["id"] == "s1")
        assert result["execution_venue"] == expected
        assert "eligible" not in json.dumps(result)
        assert "numeric" not in json.dumps(result)


@pytest.mark.parametrize("cancel_state,cancel_qty", [("canceled", "1"), ("filled", "2"), ("partially_filled", "1")])
async def test_pending_entry_evaluates_exits_cancels_then_rechecks_holdings(ledger, monkeypatch, cancel_state, cancel_qty):
    from src.services import portfolio_risk_service
    from src.shared.clients import mangrove

    reserve()
    initial = snapshot(qty="1", state="partially_filled")
    store.apply_snapshot("local", initial, asset_class="crypto")
    sdk = MagicMock()

    def evaluate(payload, *, persist, open_positions):
        assert persist is False
        qty = open_positions[0]["position_size"]
        modeled = {"success": True, "execution_state": {"cash_balance": 99999}, "open_positions": [],
                   "new_orders": [dict(order_id="exit", asset="BTC-USD", side="exit_long", status="filled",
                                       position_size=qty, price=100, position_id="p1")]}
        return SimpleNamespace(new_orders=modeled["new_orders"], model_dump=lambda: modeled)

    sdk.execution.evaluate_by_object.side_effect = evaluate
    monkeypatch.setattr(mangrove, "mangrove_ai_client", lambda: sdk)
    monkeypatch.setattr(portfolio_risk_service, "check_before_live_execution", lambda: {"allowed": True})
    after_cancel = snapshot(qty=cancel_qty, state=cancel_state)
    venue = SimpleNamespace(
        account=AsyncMock(), quote=AsyncMock(return_value={"ask_price": "100", "bid_price": "100"}),
        find_order=AsyncMock(side_effect=[initial, after_cancel]), cancel=AsyncMock(),
        prepare=AsyncMock(), submit=AsyncMock(), sellable=AsyncMock(return_value=Decimal(cancel_qty)),
    )
    await execution._tick(venue, "s1")
    venue.cancel.assert_awaited_once_with(store.route("s1"), "broker-local")
    venue.submit.assert_not_awaited()
    venue.prepare.assert_not_awaited()
    assert sdk.execution.evaluate_by_object.call_count == 1
    # No modeled cash/closed position is committed before actual execution.
    state, positions = store.execution_inputs("s1", 100)
    assert state["cash_balance"] == 1000 - 100 * float(cancel_qty)
    assert positions[0]["position_size"] == float(cancel_qty)
    latest = trade_log.list_evaluations("s1", limit=1)[0]
    assert latest.status == "skipped"
    assert latest.order_intents[0].side == "sell"
    if cancel_state == "partially_filled":
        venue.find_order.side_effect = None
        venue.find_order.return_value = after_cancel
        await execution._tick(venue, "s1")
        venue.submit.assert_not_awaited()
        assert venue.cancel.await_count == 1
        assert store.orders("s1", pending=True)[0]["status"] == "cancel_pending"
        return
    venue.find_order.side_effect = None
    venue.prepare.return_value = ({"quantity": cancel_qty}, Decimal(cancel_qty) * 100)
    venue.submit.return_value = {"order": snapshot(qty=cancel_qty, state="filled", side="sell", oid="exit")}
    # Have the fake broker echo the newly reserved client ID.
    async def submit(route, args, ref_id):
        return {"order": snapshot(qty=args["quantity"], state="filled", side="sell", oid=ref_id)}
    venue.submit.side_effect = submit
    await execution._tick(venue, "s1")
    assert sdk.execution.evaluate_by_object.call_count == 2
    assert sdk.execution.evaluate_by_object.call_args.kwargs["open_positions"][0]["position_size"] == float(cancel_qty)
    venue.submit.assert_awaited_once()
    assert store.execution_inputs("s1", 100)[1] == []


@pytest.mark.parametrize("pending_side", ["buy", "sell"])
async def test_exit_evaluation_never_duplicates_uncertain_order_or_pending_sell(ledger, monkeypatch, pending_side):
    from src.services import portfolio_risk_service
    from src.shared.clients import mangrove

    reserve(qty=2)
    store.apply_snapshot("local", snapshot(qty="2", state="filled"), asset_class="crypto")
    reserve(side=pending_side, qty=1, oid="pending", key="pending")
    pending_snapshot = None
    if pending_side == "sell":
        pending_snapshot = snapshot(qty="0", price=None, side="sell", state="queued", oid="pending")
        store.apply_snapshot("pending", pending_snapshot, asset_class="crypto")
    modeled = {"success": True, "execution_state": {"cash_balance": 99999}, "open_positions": [],
               "new_orders": [dict(order_id="exit", asset="BTC-USD", side="exit_long", status="filled",
                                   position_size=2, price=100, position_id="p1")]}
    sdk = MagicMock()
    sdk.execution.evaluate_by_object.return_value = SimpleNamespace(
        new_orders=modeled["new_orders"], model_dump=lambda: modeled)
    monkeypatch.setattr(mangrove, "mangrove_ai_client", lambda: sdk)
    monkeypatch.setattr(portfolio_risk_service, "check_before_live_execution", lambda: {"allowed": True})
    venue = SimpleNamespace(account=AsyncMock(), find_order=AsyncMock(return_value=pending_snapshot),
                            quote=AsyncMock(return_value={"ask_price": "100", "bid_price": "100"}),
                            cancel=AsyncMock(), prepare=AsyncMock(), submit=AsyncMock())
    await execution._tick(venue, "s1")
    sdk.execution.evaluate_by_object.assert_called_once()
    venue.cancel.assert_not_awaited()
    venue.prepare.assert_not_awaited()
    venue.submit.assert_not_awaited()
    state, positions = store.execution_inputs("s1", 100)
    assert state["cash_balance"] == 800
    assert positions[0]["position_size"] == 2
    assert store.orders("s1", pending=True)[0]["status"] == ("uncertain" if pending_side == "buy" else "open")


async def test_scheduled_reconciliation_protects_delayed_confirmed_entry_without_evaluation(ledger, monkeypatch):
    from src.services import portfolio_risk_service

    template = {
        "id": "p1", "asset": "BTC-USD", "stop_loss_price": 90,
        "orders": [{"id": "stop1", "asset": "BTC-USD", "position_id": "p1",
                    "order_type": "stop_loss", "side": "exit_long", "status": "pending",
                    "price": 90, "position_size": 2}],
    }
    assert store.reserve("s1", "eval", intent(qty=2), {"quantity": "2"}, template, "entry-late")
    with store.connection() as conn:
        conn.execute(
            "UPDATE broker_routes SET external_execution_mode='confirmed_v2',native_stop_loss_enabled=1 WHERE strategy_id='s1'"
        )
    entry_snapshot = snapshot(qty="2", state="filled", oid="entry-late")
    stop_snapshot = {
        "id": "broker-protect", "currency_code": "BTC",
        "symbol": "BTC", "side": "sell", "type": "stop_loss", "stop_price": "90",
        "quantity": "2", "state": "confirmed", "cumulative_quantity": "0",
        "average_price": None, "fees": "0", "net_rounded_executed_notional": "0",
    }
    venue = SimpleNamespace(
        account=AsyncMock(), find_order=AsyncMock(return_value=entry_snapshot),
        quote=AsyncMock(return_value={"bid_price": "100", "ask_price": "101"}),
        prepare_protective_stop=AsyncMock(return_value=({"quantity": "2", "side": "sell"}, Decimal(90))),
        submit=AsyncMock(),
    )
    async def submit(_route, _args, ref_id):
        return {"order": {**stop_snapshot, "ref_id": ref_id}}
    venue.submit.side_effect = submit
    monkeypatch.setattr(portfolio_risk_service, "check_before_live_execution", lambda: {"allowed": True})

    await execution._scheduled_reconcile_and_maintain(venue)

    protective = next(item for item in store.orders("s1") if item["order_kind"] == "protective_stop")
    assert protective["status"] == "open"
    assert protective["evaluation_id"] == "eval"
    assert store.protective_snapshot_quantity_matches(protective["id"])
    status = execution.protection_status("s1")["positions"][0]
    assert status["status"] == "standing_coverage_last_observed"
    assert status["broker_acknowledged_standing"] is True
    venue.submit.assert_awaited_once()
    venue.find_order.assert_awaited_once()


async def test_duplicate_bar_still_syncs_native_protection(ledger, monkeypatch):
    from src.services import portfolio_risk_service
    from src.shared.clients import mangrove

    with store.connection() as conn:
        conn.execute("UPDATE broker_routes SET external_execution_mode='confirmed_v2',native_stop_loss_enabled=1 WHERE strategy_id='s1'")
    sdk = MagicMock()
    modeled = {
        "success": True, "execution_state": {"external_execution": {"applied_through": 0}},
        "open_positions": [], "new_orders": [], "external_execution_disposition": "duplicate_bar",
    }
    result = SimpleNamespace(
        new_orders=[], model_dump=lambda: modeled,
        external_execution_ack={"contract_version": 2, "applied_through": 0, "receipts": [], "active_position_ids": []},
    )
    def evaluate(payload, *, persist, open_positions, external_execution=None):
        return result

    sdk = SimpleNamespace(execution=SimpleNamespace(evaluate_by_object=evaluate))
    monkeypatch.setattr(mangrove, "mangrove_ai_client", lambda: sdk)
    monkeypatch.setattr(portfolio_risk_service, "check_before_live_execution", lambda: {"allowed": True})
    sync = AsyncMock()
    monkeypatch.setattr(execution, "_sync_native_protection", sync)
    venue = SimpleNamespace(
        account=AsyncMock(), find_order=AsyncMock(return_value=None),
        quote=AsyncMock(return_value={"ask_price": "100", "bid_price": "100"}),
    )

    await execution._tick(venue, "s1")

    sync.assert_awaited_once()
    assert sync.await_args.args[1] == "s1"


async def test_deferred_hosted_exit_survives_cancel_retry_and_subtracts_stop_fill(ledger, monkeypatch):
    from src.services import portfolio_risk_service

    with store.connection() as conn:
        conn.execute("UPDATE broker_routes SET external_execution_mode='confirmed_v2',native_stop_loss_enabled=1 WHERE strategy_id='s1'")
    assert store.reserve("s1", "eval", intent(qty=2), {"quantity": "2"}, {"id": "p1", "asset": "BTC-USD"}, "entry-deferred")
    store.apply_snapshot("entry-deferred", snapshot(qty="2", state="filled", oid="entry-deferred"), asset_class="crypto")
    template = {
        "id": "p1", "asset": "BTC-USD", "stop_loss_price": 90,
        "orders": [{"id": "stop1", "asset": "BTC-USD", "position_id": "p1",
                    "order_type": "stop_loss", "side": "exit_long", "status": "pending",
                    "price": 90, "position_size": 2}],
    }
    assert store.reserve_protective_stop(
        "s1", "eval", "p1", "BTC-USD", 2, 90, 90,
        {"quantity": "2", "side": "sell"}, template, "stop-ref",
    )
    stop_open = {
        "id": "broker-stop", "ref_id": "stop-ref", "currency_code": "BTC", "symbol": "BTC",
        "side": "sell", "type": "stop_loss", "stop_price": "90", "quantity": "2",
        "state": "confirmed", "cumulative_quantity": "0", "average_price": None,
        "fees": "0", "net_rounded_executed_notional": "0",
    }
    store.apply_snapshot("stop-ref", stop_open, asset_class="crypto")
    original = intent(side="sell", qty=1, order="hosted-exit", position_id="p1")
    deferred = {
        "id": "deferred-1", "position_id": "p1", "evaluation_id": "eval",
        "engine_order_id": "hosted-exit", "intent_json": original.model_dump_json(),
        "position_template": template, "decision_quantity": "2", "blocker_order_id": "stop-ref",
        "blocker_filled_at_decision": "0", "route_fingerprint": store.route_fingerprint("s1"),
    }
    with store.connection() as conn:
        conn.execute(
            """INSERT INTO broker_deferred_exits
            (id,strategy_id,position_id,evaluation_id,engine_order_id,intent_json,position_template_json,
             decision_quantity,blocker_order_id,blocker_filled_at_decision,route_fingerprint,status,created_at,updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,'waiting_stop_terminal',?,?)""",
            (deferred["id"], "s1", "p1", "eval", deferred["engine_order_id"], deferred["intent_json"],
             json.dumps(template), "2", "stop-ref", "0", deferred["route_fingerprint"], store.now(), store.now()),
        )
    monkeypatch.setattr(portfolio_risk_service, "check_before_live_execution", lambda: {"allowed": True})
    venue = SimpleNamespace(
        account=AsyncMock(), cancel=AsyncMock(), find_order=AsyncMock(return_value=stop_open),
        prepare=AsyncMock(return_value=({"quantity": "0.5", "side": "sell"}, Decimal(40))),
        sellable=AsyncMock(return_value=Decimal("0.5")),
        submit=AsyncMock(),
    )

    await execution._process_deferred_exit(venue, "s1", store.route("s1"))
    await execution._process_deferred_exit(venue, "s1", store.route("s1"))
    assert venue.cancel.await_count == 2  # restart/retry reissues cancel only after active snapshots
    assert venue.submit.await_count == 0

    stop_partial_cancelled = {
        **stop_open, "state": "partially_filled_rest_cancelled", "cumulative_quantity": "0.5",
        "average_price": "80", "net_rounded_executed_notional": "40",
    }
    store.apply_snapshot("stop-ref", stop_partial_cancelled, asset_class="crypto")
    async def submit(_route, args, ref_id):
        return {"order": {
            "id": "broker-exit", "ref_id": ref_id, "currency_code": "BTC", "symbol": "BTC",
            "side": "sell", "state": "confirmed", "cumulative_quantity": "0",
            "average_price": None, "fees": "0", "net_rounded_executed_notional": "0",
        }}
    venue.submit.side_effect = submit
    await execution._process_deferred_exit(venue, "s1", store.route("s1"))

    assert venue.prepare.await_args.args[1].amount == 0.5
    assert venue.submit.await_count == 1
    saved_exit = next(item for item in store.orders("s1") if item["id"] == store.deferred_exits("s1")[0]["reserved_order_id"])
    assert saved_exit["status"] == "open"
    assert store.deferred_exits("s1")[0]["status"] == "submitted"


async def test_deferred_exit_subtracts_same_position_partial_sell_after_decision(ledger, monkeypatch):
    from src.services import portfolio_risk_service

    with store.connection() as conn:
        conn.execute("UPDATE broker_routes SET external_execution_mode='confirmed_v2',native_stop_loss_enabled=1 WHERE strategy_id='s1'")
    assert reserve("buy", 2, "entry", "entry", position_id="p1")
    store.apply_snapshot("entry", snapshot(qty="2", state="filled", oid="entry"), asset_class="crypto")
    assert reserve("sell", 1, "old-sell", "old-sell", position_id="p1")
    store.apply_snapshot(
        "old-sell", snapshot(qty="0.25", state="partially_filled", side="sell", oid="old-sell"),
        asset_class="crypto",
    )
    blocker = store.orders("s1", pending=True)[0]
    original = intent("sell", 1, "hosted-exit", "p1")
    template = {"id": "p1", "asset": "BTC-USD", "orders": []}
    deferred = {
        "id": "deferred-sell", "position_id": "p1", "evaluation_id": "eval",
        "engine_order_id": "hosted-exit", "intent_json": original.model_dump_json(),
        "position_template": template, "decision_quantity": "1.75",
        "blocker_order_id": blocker["id"], "blocker_filled_at_decision": blocker["filled_quantity"],
        "route_fingerprint": store.route_fingerprint("s1"),
    }
    with store.connection() as conn:
        conn.execute(
            """INSERT INTO broker_deferred_exits
            (id,strategy_id,position_id,evaluation_id,engine_order_id,intent_json,position_template_json,
             decision_quantity,blocker_order_id,blocker_filled_at_decision,route_fingerprint,status,created_at,updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,'waiting_stop_terminal',?,?)""",
            (deferred["id"], "s1", "p1", "eval", deferred["engine_order_id"], deferred["intent_json"],
             json.dumps(template), "1.75", blocker["id"], blocker["filled_quantity"],
             deferred["route_fingerprint"], store.now(), store.now()),
        )
    store.apply_snapshot(
        "old-sell", snapshot(qty="0.5", state="partially_filled_rest_cancelled", side="sell", oid="old-sell"),
        asset_class="crypto",
    )
    monkeypatch.setattr(portfolio_risk_service, "check_before_live_execution", lambda: {"allowed": True})
    async def submit(_route, _args, ref_id):
        return {"order": {
            "id": "broker-new-exit", "ref_id": ref_id, "currency_code": "BTC", "symbol": "BTC",
            "side": "sell", "state": "confirmed", "cumulative_quantity": "0",
            "average_price": None, "fees": "0", "net_rounded_executed_notional": "0",
        }}
    venue = SimpleNamespace(
        prepare=AsyncMock(return_value=({"quantity": "0.75", "side": "sell"}, Decimal(75))),
        sellable=AsyncMock(return_value=Decimal("0.75")), submit=AsyncMock(side_effect=submit),
    )

    await execution._process_deferred_exit(venue, "s1", store.route("s1"))

    assert venue.prepare.await_args.args[1].amount == 0.75
    assert venue.submit.await_count == 1
    assert store.deferred_exits("s1")[0]["status"] == "submitted"


async def test_other_positions_stop_fill_does_not_reduce_hosted_exit_authorization(ledger, monkeypatch):
    from src.services import portfolio_risk_service

    with store.connection() as conn:
        conn.execute("UPDATE broker_routes SET external_execution_mode='confirmed_v2',native_stop_loss_enabled=1 WHERE strategy_id='s1'")
    assert reserve("buy", 2, "entry-p1", "entry-p1", position_id="p1")
    store.apply_snapshot("entry-p1", snapshot(qty="2", state="filled", oid="entry-p1"), asset_class="crypto")
    assert reserve("buy", 1, "entry-p2", "entry-p2", position_id="p2")
    store.apply_snapshot("entry-p2", snapshot(qty="1", state="filled", oid="entry-p2"), asset_class="crypto")
    template2 = {
        "id": "p2", "asset": "BTC-USD", "stop_loss_price": 90,
        "orders": [{"id": "stop2", "asset": "BTC-USD", "position_id": "p2",
                    "order_type": "stop_loss", "side": "exit_long", "status": "pending",
                    "price": 90, "position_size": 1}],
    }
    assert store.reserve_protective_stop(
        "s1", "eval", "p2", "BTC-USD", 1, 90, 90, {"quantity": "1", "side": "sell"}, template2, "stop-p2",
    )
    stop_snapshot = {
        "id": "broker-stop-p2", "ref_id": "stop-p2", "currency_code": "BTC", "symbol": "BTC",
        "side": "sell", "type": "stop_loss", "stop_price": "90", "quantity": "1",
        "state": "confirmed", "cumulative_quantity": "0", "average_price": None,
        "fees": "0", "net_rounded_executed_notional": "0",
    }
    store.apply_snapshot("stop-p2", stop_snapshot, asset_class="crypto")
    original = intent("sell", 1, "hosted-exit-p1", "p1")
    deferred = {
        "id": "deferred-p1", "position_id": "p1", "evaluation_id": "eval",
        "engine_order_id": "hosted-exit-p1", "intent_json": original.model_dump_json(),
        "position_template": {"id": "p1", "asset": "BTC-USD", "orders": []},
        "decision_quantity": "2", "blocker_order_id": "stop-p2", "blocker_filled_at_decision": "0",
        "route_fingerprint": store.route_fingerprint("s1"),
    }
    with store.connection() as conn:
        conn.execute(
            """INSERT INTO broker_deferred_exits
            (id,strategy_id,position_id,evaluation_id,engine_order_id,intent_json,position_template_json,
             decision_quantity,blocker_order_id,blocker_filled_at_decision,route_fingerprint,status,created_at,updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,'waiting_stop_terminal',?,?)""",
            (deferred["id"], "s1", "p1", "eval", deferred["engine_order_id"], deferred["intent_json"],
             json.dumps(deferred["position_template"]), "2", "stop-p2", "0",
             deferred["route_fingerprint"], store.now(), store.now()),
        )
    store.apply_snapshot(
        "stop-p2",
        {**stop_snapshot, "state": "partially_filled_rest_cancelled", "cumulative_quantity": "0.5",
         "average_price": "80", "net_rounded_executed_notional": "40"},
        asset_class="crypto",
    )
    monkeypatch.setattr(portfolio_risk_service, "check_before_live_execution", lambda: {"allowed": True})
    async def submit(_route, _args, ref_id):
        return {"order": {
            "id": "broker-p1-exit", "ref_id": ref_id, "currency_code": "BTC", "symbol": "BTC",
            "side": "sell", "state": "confirmed", "cumulative_quantity": "0",
            "average_price": None, "fees": "0", "net_rounded_executed_notional": "0",
        }}
    venue = SimpleNamespace(
        prepare=AsyncMock(return_value=({"quantity": "1", "side": "sell"}, Decimal(100))),
        sellable=AsyncMock(return_value=Decimal("1")), submit=AsyncMock(side_effect=submit),
    )

    await execution._process_deferred_exit(venue, "s1", store.route("s1"))

    assert venue.prepare.await_args.args[1].amount == 1
    assert venue.submit.await_count == 1


async def test_immediate_confirmed_entry_fill_triggers_protection_maintenance(ledger, monkeypatch):
    from src.services import portfolio_risk_service
    from src.shared.clients import mangrove

    with store.connection() as conn:
        conn.execute("UPDATE broker_routes SET external_execution_mode='confirmed_v2',native_stop_loss_enabled=1 WHERE strategy_id='s1'")
    template = {
        "id": "p1", "asset": "BTC-USD", "stop_loss_price": 90,
        "orders": [{"id": "stop1", "asset": "BTC-USD", "position_id": "p1",
                    "order_type": "stop_loss", "side": "exit_long", "status": "pending",
                    "price": 90, "position_size": 2}],
    }
    modeled_order = {
        "order_id": "engine-entry", "asset": "BTC-USD", "side": "enter_long",
        "status": "filled", "position_size": 2, "price": 100, "position_id": "p1",
    }
    modeled = {
        "success": True, "execution_state": {"external_execution": {"applied_through": 0}},
        "open_positions": [], "position_templates": [template], "new_orders": [modeled_order],
        "external_execution_disposition": "evaluated",
    }
    result = SimpleNamespace(
        new_orders=[modeled_order], model_dump=lambda: modeled,
        external_execution_ack={"contract_version": 2, "applied_through": 0, "receipts": [], "active_position_ids": []},
    )
    def evaluate(payload, *, persist, open_positions, external_execution=None):
        return result
    sdk = SimpleNamespace(execution=SimpleNamespace(evaluate_by_object=evaluate))
    monkeypatch.setattr(mangrove, "mangrove_ai_client", lambda: sdk)
    monkeypatch.setattr(portfolio_risk_service, "check_before_live_execution", lambda: {"allowed": True})
    sync = AsyncMock()
    monkeypatch.setattr(execution, "_sync_native_protection", sync)
    venue = SimpleNamespace(
        account=AsyncMock(), find_order=AsyncMock(return_value=None),
        quote=AsyncMock(return_value={"ask_price": "100", "bid_price": "100"}),
        prepare=AsyncMock(return_value=({"quantity": "2", "side": "buy"}, Decimal(200))),
        balance=AsyncMock(return_value=Decimal(1000)), submit=AsyncMock(),
    )
    async def submit(_route, args, ref_id):
        return {"order": snapshot(qty=args["quantity"], state="filled", oid=ref_id)}
    venue.submit.side_effect = submit

    await execution._tick(venue, "s1")

    assert store.execution_inputs("s1", 100)[1][0]["position_size"] == 2
    sync.assert_awaited_once()
    assert sync.await_args.args[1] == "s1"
    assert sync.await_args.args[5][0]["id"] == "p1"


async def test_native_protection_maintenance_never_places_while_paused_or_halted(ledger, monkeypatch):
    from src.services import portfolio_risk_service

    with store.connection() as conn:
        conn.execute("UPDATE broker_routes SET external_execution_mode='confirmed_v2',native_stop_loss_enabled=1 WHERE strategy_id='s1'")
        conn.execute("UPDATE strategies SET status='inactive' WHERE id='s1'")
    venue = SimpleNamespace(quote=AsyncMock(), submit=AsyncMock())
    await execution._native_protection_maintenance(venue)
    venue.quote.assert_not_awaited()
    venue.submit.assert_not_awaited()
    with store.connection() as conn:
        conn.execute("UPDATE strategies SET status='live' WHERE id='s1'")
    monkeypatch.setattr(portfolio_risk_service, "check_before_live_execution", lambda: {"allowed": False})
    await execution._native_protection_maintenance(venue)
    venue.quote.assert_not_awaited()
    venue.submit.assert_not_awaited()


async def test_missing_stop_quantity_evidence_stays_unverified_without_churn(ledger):
    with store.connection() as conn:
        conn.execute("UPDATE broker_routes SET external_execution_mode='confirmed_v2',native_stop_loss_enabled=1 WHERE strategy_id='s1'")
    template = {
        "id": "p1", "asset": "BTC-USD", "stop_loss_price": 90,
        "orders": [{"id": "stop1", "asset": "BTC-USD", "position_id": "p1",
                    "order_type": "stop_loss", "side": "exit_long", "status": "pending",
                    "price": 90, "position_size": 2}],
    }
    assert reserve(qty=2, oid="entry", key="entry", position_id="p1")
    store.apply_snapshot("entry", snapshot(qty="2", state="filled", oid="entry"), asset_class="crypto")
    assert store.reserve_protective_stop(
        "s1", "eval", "p1", "BTC-USD", 2, 90, 90, {"quantity": "2", "side": "sell"}, template, "stop-ref",
    )
    observed = {
        "id": "broker-stop", "ref_id": "stop-ref", "currency_code": "BTC", "symbol": "BTC",
        "side": "sell", "type": "stop_loss", "stop_price": "90", "state": "confirmed",
        "cumulative_quantity": "0", "average_price": None, "fees": "0",
        "net_rounded_executed_notional": "0",
    }
    store.apply_snapshot("stop-ref", observed, asset_class="crypto")
    _, positions = store.execution_inputs("s1", 100)
    venue = SimpleNamespace(cancel=AsyncMock(), submit=AsyncMock(), prepare_protective_stop=AsyncMock())

    await execution._sync_native_protection(venue, "s1", store.route("s1"), "eval", [template], positions)
    await execution._sync_native_protection(venue, "s1", store.route("s1"), "eval", [template], positions)

    status = execution.protection_status("s1")["positions"][0]
    assert status["status"] == "coverage_unverified"
    assert status["broker_acknowledged_standing"] is False
    venue.cancel.assert_not_awaited()
    venue.submit.assert_not_awaited()
    venue.prepare_protective_stop.assert_not_awaited()


async def test_blocked_hosted_exit_ack_persists_before_cancel(ledger, monkeypatch):
    from src.services import portfolio_risk_service
    from src.shared.clients import mangrove

    with store.connection() as conn:
        conn.execute("UPDATE broker_routes SET external_execution_mode='confirmed_v2',native_stop_loss_enabled=1 WHERE strategy_id='s1'")
    assert reserve(qty=2, oid="entry", key="entry", position_id="p1")
    store.apply_snapshot("entry", snapshot(qty="2", state="filled", oid="entry"), asset_class="crypto")
    template = {
        "id": "p1", "asset": "BTC-USD", "stop_loss_price": 90,
        "orders": [{"id": "stop1", "asset": "BTC-USD", "position_id": "p1",
                    "order_type": "stop_loss", "side": "exit_long", "status": "pending",
                    "price": 90, "position_size": 2}],
    }
    assert store.reserve_protective_stop(
        "s1", "eval", "p1", "BTC-USD", 2, 90, 90, {"quantity": "2", "side": "sell"}, template, "stop-ref",
    )
    blocker = {
        "id": "broker-stop", "ref_id": "stop-ref", "currency_code": "BTC", "symbol": "BTC",
        "side": "sell", "type": "stop_loss", "stop_price": "90", "quantity": "2",
        "state": "confirmed", "cumulative_quantity": "0", "average_price": None,
        "fees": "0", "net_rounded_executed_notional": "0",
    }
    store.apply_snapshot("stop-ref", blocker, asset_class="crypto")
    _, positions, _, events, _, _ = store.execution_inputs(
        "s1", 100, include_revision=True, include_external_events=True
    )
    event = events[0]
    receipt = {
        "sequence": event["sequence"], "event_id": event["event_id"],
        "position_id": event["position_id"], "kind": event["kind"],
        "sha256": store._external_event_hash(event), "occurred_at": event.get("occurred_at"),
        "accounting_at": "2026-10-17T07:00:00Z",
    }
    hosted_exit = {
        "order_id": "hosted-exit", "asset": "BTC-USD", "side": "exit_long",
        "status": "filled", "position_size": 1, "price": 100, "position_id": "p1",
    }
    modeled = {
        "success": True,
        "execution_state": {"external_execution": {"applied_through": 1, "receipts": [receipt]}},
        "open_positions": [positions[0]], "position_templates": [template],
        "new_orders": [hosted_exit], "external_execution_disposition": "evaluated",
    }
    result = SimpleNamespace(
        new_orders=[hosted_exit], model_dump=lambda: modeled,
        external_execution_ack={
            "contract_version": 2, "applied_through": 1, "receipts": [receipt],
            "active_position_ids": ["p1"],
        },
    )
    def evaluate(payload, *, persist, open_positions, external_execution=None):
        assert external_execution["events"] == events
        return result
    sdk = SimpleNamespace(execution=SimpleNamespace(evaluate_by_object=evaluate))
    monkeypatch.setattr(mangrove, "mangrove_ai_client", lambda: sdk)
    monkeypatch.setattr(portfolio_risk_service, "check_before_live_execution", lambda: {"allowed": True})
    async def verify_saved_then_cancel(_route, _broker_id):
        saved = store.deferred_exits("s1", pending_only=True)
        assert len(saved) == 1
        assert saved[0]["engine_order_id"] == "hosted-exit"
        state, _ = store.execution_inputs("s1", 100)
        assert state["external_execution"]["applied_through"] == 1
    venue = SimpleNamespace(
        account=AsyncMock(), find_order=AsyncMock(return_value=blocker),
        quote=AsyncMock(return_value={"ask_price": "100", "bid_price": "100"}),
        cancel=AsyncMock(side_effect=verify_saved_then_cancel),
    )

    await execution._tick(venue, "s1")

    saved = store.deferred_exits("s1", pending_only=True)
    assert len(saved) == 1
    assert saved[0]["status"] == "waiting_stop_terminal"
    assert venue.cancel.await_count == 1
    assert store.pending_external_execution_events("s1") == []


def test_deferred_exit_ack_conflict_rolls_back_cursor_and_intent(ledger):
    with store.connection() as conn:
        conn.execute("UPDATE broker_routes SET external_execution_mode='confirmed_v2',native_stop_loss_enabled=1 WHERE strategy_id='s1'")
    _, _, revision, events, raw_state, raw_positions = store.execution_inputs(
        "s1", 100, include_revision=True, include_external_events=True
    )
    assert events == []
    deferred = {
        "id": "bad-deferred", "position_id": "p1", "evaluation_id": "eval",
        "engine_order_id": "exit1", "intent_json": intent(side="sell", qty=1, order="exit1").model_dump_json(),
        "position_template": {"id": "p1", "asset": "BTC-USD"}, "decision_quantity": "1",
        "blocker_order_id": None, "blocker_filled_at_decision": "0", "route_fingerprint": "wrong",
    }
    with pytest.raises(SdkError, match="route changed"):
        store.acknowledge_confirmed_close_events(
            "s1", [], {"bar_index": 99, "external_execution": {"applied_through": 0}},
            open_positions=[], expected_state=raw_state, expected_open_positions=raw_positions,
            expected_applied_through=0, expected_broker_revision=revision,
            receipts=[], submitted_events=[], deferred_exit=deferred,
        )
    with store.connection() as conn:
        saved = conn.execute("SELECT execution_state_json,open_positions_json FROM strategies WHERE id='s1'").fetchone()
        count = conn.execute("SELECT count(*) FROM broker_deferred_exits WHERE strategy_id='s1'").fetchone()[0]
    assert saved["execution_state_json"] is None
    assert saved["open_positions_json"] is None
    assert count == 0
