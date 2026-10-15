"""Offline broker tests: real journal/SQLite, injected broker and Mangrove I/O."""

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
                """INSERT INTO broker_routes VALUES (?,'robinhood','eligible','numeric','crypto','1000','500','0.0025',?)""",
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


def intent(side="buy", qty=2, order="entry"):
    return OrderIntent(
        action="enter" if side == "buy" else "exit",
        side=side,
        symbol="BTC",
        amount=qty,
        ref_price=100,
        engine_position_id="p1",
        engine_order_id=order,
    )


def reserve(side="buy", qty=2, oid="local", key="entry", sid="s1"):
    return store.reserve(
        sid,
        "eval" if sid == "s1" else "eval2",
        intent(side, qty, key),
        {"quantity": str(qty)},
        {"id": "p1", "asset": "BTC-USD", "position_size": 2, "orders": [{"position_size": 2, "price": 90}]},
        oid,
    )


def snapshot(qty="1", price="100", state="partially_filled", side="buy", oid="local", net=None):
    return dict(
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
    assert sdk.execution.evaluate_by_object.call_count == 1
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
