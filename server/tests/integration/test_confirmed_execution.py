"""Offline confirmed-consumer controls using real migrations and durable journals."""

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.config import app_config
from src.models.domain import Evaluation, OrderIntent
from src.services import broker_store as store
from src.services import confirmed_execution as confirmed
from src.services import dex_confirmed as dex
from src.services import strategy_service, trade_log
from src.shared.db import sqlite as db
from src.shared.errors import SdkError

WALLET = "0x" + "11" * 20
QUOTE = "0x" + "22" * 20
ASSET = "0x" + "33" * 20
ROUTER = "0x" + "44" * 20


def seed_database(path):
    """Dedicated harness entry point: new local file only; never a customer DB."""
    db.reset_connection()
    app_config.DB_PATH = str(path)
    db.init_db()
    with store.connection() as conn:
        conn.execute(
            "INSERT INTO strategies(id,mangrove_id,name,asset,timeframe,status,entry_json,exit_json,execution_config_json,created_at,updated_at) VALUES ('s','remote','confirmed','BTC-USD','1h','live','[]','[]','{}',?,?)",
            (store.now(), store.now()),
        )
        conn.execute(
            "INSERT INTO wallets(id,address,chain,network,chain_id,encrypted_secret,encryption_method,created_at,backup_confirmed_at) VALUES ('w',?,'evm','testnet',84532,X'00','test',?,?)",
            (WALLET, store.now(), store.now()),
        )
        conn.execute(
            "INSERT INTO allocations(id,strategy_id,wallet_address,token_address,token_symbol,amount,active,created_at,slippage_pct) VALUES ('a','s',?,?,'USDC',1000,1,?,0.002)",
            (WALLET, QUOTE, store.now()),
        )
        conn.execute("INSERT INTO execution_capital VALUES ('s','a',?,'USDC','1000',?)", (QUOTE, store.now()))
    trade_log.log_evaluation(
        Evaluation(id="eval", strategy_id="s", timestamp=trade_log.now_utc(), duration_ms=0, status="ok")
    )


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    monkeypatch.setattr(app_config, "DB_PATH", str(tmp_path / "confirmed.db"))
    seed_database(tmp_path / "confirmed.db")
    yield
    db.reset_connection()


def intent(action="enter", qty=2, pid="p", key="entry"):
    return OrderIntent(
        action=action,
        side="buy" if action == "enter" else "sell",
        symbol="BTC",
        amount=qty,
        ref_price=100,
        engine_position_id=pid,
        engine_order_id=key,
    )


def reserve_fill(action="enter", qty="2", cash="200", fee="0", pid="p", key="entry"):
    i = intent(action, float(qty), pid, key)
    oid = dex.reserve(
        "s",
        "eval",
        i,
        dict(
            id=pid,
            asset="BTC-USD",
            execution_confirmed=False,
            orders=[dict(side="exit_long", price=90, status="pending")],
        ),
        confirmed.snapshot_revision("s"),
    )
    assert oid
    dex.update(
        oid,
        tx_hash="0x" + "aa" * 32,
        context=dict(
            signed_native_value="0",
            input_token_address=QUOTE if action == "enter" else ASSET,
            output_token_address=ASSET if action == "enter" else QUOTE,
        ),
    )
    order = next(o for o in dex.orders("s") if o["id"] == oid)
    fill = dict(
        input_amount=cash if action == "enter" else qty,
        output_amount=qty if action == "enter" else cash,
        native_fee_complete=True,
        native_fee_amount=fee,
    )
    dex.apply_fill(order, fill, None, "2026-10-29T00:00:00+00:00", store.now())
    return order, fill


def test_quote_proposal_and_pending_do_not_create_holdings(ledger):
    oid = dex.reserve("s", "eval", intent(), dict(id="p", position_size=999), confirmed.snapshot_revision("s"))
    dex.update(oid, status="pending", context=dict(quoted_output=999))
    state, positions, closes, complete = confirmed.dex_inputs("s", 100)
    assert {"cash_balance", "account_value", "total_trades", "num_open_positions"} <= state.keys()
    assert positions == [] and closes == [] and complete is False
    assert state["cash_balance"] == 1000
    assert not dex.reserve("s", "eval", intent(pid="other", key="other"), {}, confirmed.snapshot_revision("s"))
    db.reset_connection()
    assert len(dex.orders("s", pending=True)) == 1


def test_actual_partial_exit_close_aggregation_and_idempotency(ledger):
    entry, fill = reserve_fill()
    dex.apply_fill(entry, fill, None, "2026-10-29T00:00:00+00:00", store.now())
    reserve_fill("exit", "0.5", "45", key="exit1")
    state, positions, closes, complete = confirmed.dex_inputs("s", 100)
    assert complete and positions[0]["position_size"] == 1.5 and closes == []
    assert state["cash_balance"] == 845
    reserve_fill("exit", "1.5", "135", key="exit2")
    state, positions, closes, complete = confirmed.dex_inputs("s", 100)
    assert complete and positions == [] and state["cash_balance"] == 980
    assert len(closes) == 1 and closes[0]["realized_pnl"] == -20
    delivered, bootstrap = confirmed.close_delivery(state, closes, complete=complete)
    returned = dict(
        execution_state=dict(
            risk_state=dict(
                close_history_bootstrapped=True, recent_close_ids=["p"], closes_booked_through=closes[0]["booked_at"]
            )
        )
    )
    assert confirmed.save_risk("s", state, returned, delivered, confirmed.snapshot_revision("s"), complete=True)
    state = confirmed.dex_inputs("s", 100)[0]
    assert confirmed.close_delivery(state, closes, complete=True) == ([], False)
    with store.connection() as conn:
        assert conn.execute("SELECT count(*) FROM trades").fetchone()[0] == 3


def test_nonzero_native_fee_holds_entries_keeps_physical_exit(ledger, monkeypatch):
    reserve_fill(fee="0.00001")
    state, positions, closes, complete = confirmed.dex_inputs("s", 100)
    assert not complete and positions[0]["position_size"] == 2 and closes == []
    sdk = MagicMock()

    def evaluate(payload, *, persist, open_positions):
        assert persist is False and payload["execution_state"]["allow_entries"] is False
        assert open_positions[0]["position_size"] == 2
        assert payload["closed_positions"] == [] and payload["closed_positions_bootstrap_complete"] is False
        data = dict(
            success=True,
            execution_state=dict(risk_state=dict(close_history_bootstrapped=True, recent_close_ids=["fiction"])),
            open_positions=[],
        )
        data["new_orders"] = [
            dict(
                order_id="exit",
                asset="BTC-USD",
                side="exit_long",
                status="filled",
                position_size=2,
                price=90,
                position_id="p",
            )
        ]
        return SimpleNamespace(new_orders=data["new_orders"], model_dump=lambda: data)

    sdk.execution.evaluate_by_object.side_effect = evaluate
    monkeypatch.setattr(strategy_service, "mangrove_ai_client", lambda: sdk)
    monkeypatch.setattr(strategy_service.order_executor, "_fetch_mark_price", lambda _: 100)
    monkeypatch.setattr(dex, "reconcile", lambda _: None)
    execute = MagicMock()
    monkeypatch.setattr(dex, "execute", execute)
    strategy_service.tick("s")
    assert execute.call_count == 1 and execute.call_args.args[1].action == "exit"
    assert json.loads(strategy_service._get_row("s")["execution_state_json"] or "{}").get("risk_state") is None


def test_stale_response_and_fictional_close_cannot_authorize_entries(ledger):
    revision = confirmed.snapshot_revision("s")
    response = dict(execution_state=dict(risk_state=dict(close_history_bootstrapped=True, recent_close_ids=["fake"])))
    assert not confirmed.save_risk("s", {}, response, [], revision, complete=True)
    response["execution_state"]["risk_state"]["recent_close_ids"] = []
    reserve_fill()
    assert not confirmed.save_risk("s", {}, response, [], revision, complete=True)
    assert not dex.reserve("s", "eval", intent(pid="next", key="next"), {}, revision)


def test_oversell_and_independent_exit_reservations(ledger):
    reserve_fill(pid="p1", key="e1")
    reserve_fill(pid="p2", key="e2")
    with pytest.raises(SdkError, match="exceeds"):
        dex.reserve("s", "eval", intent("exit", 3, "p1", "bad"), {}, None)
    assert dex.reserve("s", "eval", intent("exit", 2, "p1", "x1"), {}, None)
    assert dex.reserve("s", "eval", intent("exit", 2, "p2", "x2"), {}, None)
    assert not dex.reserve("s", "eval", intent("exit", 1, "p1", "again"), {}, None)


def test_human_pause_retains_monitor_and_allocation_until_confirmed_flat(ledger, monkeypatch):
    reserve_fill()
    scheduler = MagicMock()
    monkeypatch.setattr(strategy_service, "scheduler_service", scheduler)
    row = strategy_service._get_row("s")
    strategy_service._apply_scheduler_effects("s", row, "inactive", "live")
    strategy_service._set_status("s", "inactive")
    assert strategy_service._get_row("s")["exit_only_mode"] == "live"
    scheduler.cancel_job.assert_not_called()
    assert strategy_service.allocation_service.get_active_allocation("s")
    reserve_fill("exit", "2", "180", key="exit")
    strategy_service._finish_exit_monitor_if_flat("s")
    scheduler.cancel_job.assert_called_once_with("s")
    assert strategy_service.allocation_service.get_active_allocation("s") is None


@pytest.mark.parametrize(
    "mutate", ["clean", "wrong_wallet", "wrong_chain", "pending", "nonzero_fee", "missing_approval"]
)
def test_finalized_receipt_adapter_clean_and_known_bad(ledger, monkeypatch, mutate):
    from tests.unit.test_receipt_settlement import BLOCK, TX, receipt, transfer

    raw = receipt()
    raw["from"] = WALLET
    raw["to"] = ROUTER
    raw["gasUsed"] = "0x0"
    raw["effectiveGasPrice"] = "0x0"
    raw.update(l1Fee="0x0", operatorFee="0x0")
    raw["logs"] = [
        transfer(QUOTE, WALLET, ROUTER, 80_000_000, 5),
        transfer(ASSET, ROUTER, WALLET, 250_000_000_000_000_000, 7),
    ]
    if mutate == "wrong_wallet":
        raw["from"] = "0x" + "99" * 20
    if mutate == "nonzero_fee":
        raw["effectiveGasPrice"] = "0x1"
        raw["gasUsed"] = "0x5208"

    class RPC:
        def __call__(self, method, args):
            if method == "eth_chainId":
                return "0x1" if mutate == "wrong_chain" else hex(84532)
            if method == "eth_getTransactionReceipt":
                return None if mutate == "pending" or args[0] != TX else raw
            if method == "eth_getBlockByNumber":
                return dict(number="0x124" if args[0] == "finalized" else "0x123", hash=BLOCK, timestamp="0x68f00000")
            raise AssertionError(method)

        def close(self):
            pass

    monkeypatch.setattr(dex, "receipt_rpc", lambda _: RPC())
    oid = dex.reserve("s", "eval", intent(), dict(id="p", orders=[]), confirmed.snapshot_revision("s"))
    context = dict(
        wallet_address=WALLET,
        signed_native_value="0",
        chain_id=84532,
        router_address=ROUTER,
        input_token_address=QUOTE,
        output_token_address=ASSET,
        input_decimals=6,
        output_decimals=18,
        approval_required=mutate == "missing_approval",
        approval_tx_hash="0x" + "cd" * 32,
        approval_recipient=QUOTE,
    )
    dex.update(oid, status="pending", tx_hash=TX, context=context)
    dex.reconcile("s")
    state, positions, closes, complete = confirmed.dex_inputs("s", 100)
    if mutate in {"wrong_wallet", "wrong_chain", "pending"}:
        assert positions == [] and state["cash_balance"] == 1000 and complete is False
        assert dex.orders("s", pending=True)
    else:
        assert positions[0]["position_size"] == 0.25 and state["cash_balance"] == 920
        assert complete is (mutate == "clean")
        dex.reconcile("s")
        with store.connection() as conn:
            assert conn.execute("SELECT count(*) FROM trades").fetchone()[0] == 1


@pytest.mark.parametrize(
    "outcome",
    [
        "clean",
        "missing_rpc",
        "wrong_chain",
        "native_value",
        "lost_ack",
        "wrong_hash",
        "pause_during_quote",
        "stale_during_quote",
        "pause_after_sign",
    ],
)
def test_execute_preflight_and_locally_hashed_broadcast_controls(ledger, monkeypatch, outcome):
    from eth_utils import keccak

    from src.services import order_executor, wallet_manager
    from src.shared.clients import mangrove

    localhash = "0x" + keccak(hexstr="0x0102").hex()
    router = next(iter(wallet_manager._ONEINCH_ROUTERS))

    class RPC:
        def __call__(self, method, args):
            if method == "eth_chainId":
                return "0x1" if outcome == "wrong_chain" else hex(84532)
            if method == "eth_getBlockByNumber":
                return dict(number="0x123", hash="0x" + "bb" * 32)
            if method == "eth_getTransactionReceipt":
                return None
            raise AssertionError(method)

        def close(self):
            pass

    def provider(_):
        if outcome == "missing_rpc":
            raise SdkError("No configured RPC")
        return RPC()

    monkeypatch.setattr(dex, "receipt_rpc", provider)
    sdk = MagicMock()
    sdk.dex.token_search.return_value = [SimpleNamespace(symbol="BTC", address=ASSET, decimals=18)]
    sdk.dex.token_info.return_value = SimpleNamespace(decimals=6)
    payload = dict(
        to=router,
        value=1 if outcome == "native_value" else 0,
        data="0x1234",
        gas=100000,
        gasPrice=1,
        nonce=0,
        chainId=84532,
    )
    sdk.dex.prepare_swap.return_value = SimpleNamespace(payload=payload)
    sdk.dex.approve_token.return_value = None
    monkeypatch.setattr(mangrove, "mangrove_markets_client", lambda: sdk)

    def quote(**kwargs):
        with store.connection() as conn:
            if outcome == "pause_during_quote":
                conn.execute("UPDATE strategies SET status='inactive' WHERE id='s'")
            if outcome == "stale_during_quote":
                conn.execute("UPDATE strategies SET book_revision=book_revision+1 WHERE id='s'")
        return dict(quote_id="quote", output_amount=99999)

    monkeypatch.setattr(dex.dex_service, "get_quote", quote)

    def sign_payload(*args, **kwargs):
        if outcome == "pause_after_sign":
            strategy_service._set_status("s", "inactive")
        return "0x0102"

    sign = MagicMock(side_effect=sign_payload)
    monkeypatch.setattr(order_executor, "wallet_sign", sign)

    def broadcast(**kwargs):
        # The hash is durable before the first possibly irreversible call.
        assert dex.orders("s")[0]["tx_hash"] == localhash
        if outcome == "lost_ack":
            raise TimeoutError("response lost")
        return SimpleNamespace(tx_hash="0x" + "ff" * 32 if outcome == "wrong_hash" else localhash)

    sdk.dex.broadcast.side_effect = broadcast
    oid = dex.reserve("s", "eval", intent(), dict(id="p"), confirmed.snapshot_revision("s"))
    if outcome == "clean":
        dex.execute(oid, intent(), WALLET, 84532, 0.002, 1000)
    else:
        with pytest.raises(Exception):
            dex.execute(oid, intent(), WALLET, 84532, 0.002, 1000)
    row = dex.orders("s")[0]
    state, positions, _, complete = confirmed.dex_inputs("s", 100)
    assert positions == [] and state["cash_balance"] == 1000
    if outcome in {"missing_rpc", "wrong_chain", "native_value", "pause_during_quote", "stale_during_quote"}:
        sign.assert_not_called()
        sdk.dex.broadcast.assert_not_called()
        assert row["status"] == "failed" and complete is True
    elif outcome == "pause_after_sign":
        sign.assert_called_once()
        sdk.dex.broadcast.assert_not_called()
        assert row["tx_hash"] == localhash and row["status"] == "failed" and complete is True
    else:
        sign.assert_called_once()
        assert row["tx_hash"] == localhash
        assert row["status"] == ("pending" if outcome == "clean" else "uncertain")
        assert complete is False


def test_tied_new_close_cursor_replays_complete_history(ledger):
    closes = [
        dict(position_id="old", booked_at="2026-10-29T00:00:00+00:00", exit_timestamp="2026-10-29T00:00:00+00:00"),
        dict(position_id="new", booked_at="2026-10-29T00:00:00+00:00", exit_timestamp="2026-10-29T00:00:00+00:00"),
    ]
    state = dict(
        risk_state=dict(
            close_history_bootstrapped=True, recent_close_ids=["old"], closes_booked_through=closes[0]["booked_at"]
        )
    )
    assert confirmed.close_delivery(state, closes, complete=True) == (closes, True)
    response = dict(
        execution_state=dict(
            risk_state=dict(close_history_bootstrapped=True, closes_booked_through="2099-01-01T00:00:00+00:00")
        )
    )
    assert not confirmed.save_risk("s", state, response, closes, confirmed.snapshot_revision("s"), complete=True)


def test_archiving_exit_monitor_keeps_scheduler_and_reactivation_failure_keeps_allocation(ledger, monkeypatch):
    reserve_fill()
    scheduler = MagicMock()
    monkeypatch.setattr(strategy_service, "scheduler_service", scheduler)
    strategy_service._apply_scheduler_effects("s", strategy_service._get_row("s"), "inactive", "live")
    strategy_service._set_status("s", "inactive")
    sdk = MagicMock()
    sdk.strategies.update_status.side_effect = RuntimeError("offline")
    monkeypatch.setattr(strategy_service, "mangrove_ai_client", lambda: sdk)
    from src.shared.errors import StrategyInvalidStatusTransition

    with pytest.raises(StrategyInvalidStatusTransition, match="settle flat"):
        strategy_service._update_status("s", strategy_service.StrategyStatusUpdate(status="paper"))
    allocation = strategy_service.StrategyAllocationInput(
        wallet_address=WALLET, token="USDC", token_address=QUOTE, amount=1000, slippage_pct=0.002
    )
    with pytest.raises(SdkError, match="offline"):
        strategy_service._update_status(
            "s", strategy_service.StrategyStatusUpdate(status="live", confirm=True, allocation=allocation)
        )
    assert strategy_service.allocation_service.get_active_allocation("s") is not None
    strategy_service._apply_scheduler_effects("s", strategy_service._get_row("s"), "archived", "inactive")
    scheduler.cancel_job.assert_not_called()
    assert strategy_service._get_row("s")["exit_only_mode"] == "live"


def test_activation_baseline_ignores_paper_audit_book(ledger):
    from src.models.domain import Position, Trade
    from src.services.allocation_service import record_allocation, release_allocation

    with store.connection() as conn:
        conn.execute("DELETE FROM execution_capital")
    release_allocation("s")
    paper = Trade(
        id="paper",
        strategy_id="s",
        order_intent=intent(pid="paperposition"),
        mode="paper",
        input_token="USDC",
        input_amount=999,
        output_token="BTC",
        output_amount=99,
        fill_price=10,
        fees={},
        status="simulated",
        executed_at=trade_log.now_utc(),
    )
    trade_log.log_trade(paper)
    trade_log.update_position(
        Position(
            id="paperposition",
            strategy_id="s",
            asset="BTC",
            entry_trade_id="paper",
            entry_price=10,
            entry_amount=99,
            entry_time=trade_log.now_utc(),
            status="open",
        )
    )
    with store.connection() as conn:
        conn.execute(
            "UPDATE strategies SET execution_state_json=? WHERE id=?",
            (json.dumps(dict(risk_state=dict(halt={"reason": "paperloss"}))), "s"),
        )
    record_allocation("s", WALLET, QUOTE, "USDC", 1000, 0.002)
    state, positions, closes, complete = confirmed.dex_inputs("s", 100)
    assert complete and positions == [] and closes == []
    assert state["cash_balance"] == 1000 and state["total_trades"] == 0 and not state.get("risk_state")
    assert not confirmed.has_exposure("s")


@pytest.mark.parametrize("approval_only", [False, True])
def test_fee_only_receipts_never_create_physical_fills(ledger, monkeypatch, approval_only):
    from tests.unit.test_receipt_settlement import BLOCK, TX, receipt

    raw = receipt()
    raw.update(
        status="0x0" if not approval_only else "0x1", to=ROUTER, from_address=WALLET, l1Fee="0x0", operatorFee="0x0"
    )
    raw["from"] = WALLET

    class RPC:
        def __call__(self, method, args):
            if method == "eth_chainId":
                return hex(84532)
            if method == "eth_getTransactionReceipt":
                return raw
            if method == "eth_getBlockByNumber":
                return dict(number="0x124" if args[0] == "finalized" else "0x123", hash=BLOCK, timestamp="0x68f00000")
            raise AssertionError(method)

        def close(self):
            pass

    monkeypatch.setattr(dex, "receipt_rpc", lambda _: RPC())
    oid = dex.reserve("s", "eval", intent(), dict(id="p"), confirmed.snapshot_revision("s"))
    context = dict(
        wallet_address=WALLET,
        chain_id=84532,
        router_address=ROUTER,
        input_token_address=QUOTE,
        output_token_address=ASSET,
        input_decimals=6,
        output_decimals=18,
        signed_native_value="0",
        approval_required=approval_only,
        approval_tx_hash=TX,
        approval_recipient=ROUTER,
    )
    dex.update(oid, status="uncertain", tx_hash=None if approval_only else TX, context=context)
    dex.reconcile("s")
    assert dex.orders("s")[0]["status"] == "fee_only"
    state, positions, closes, complete = confirmed.dex_inputs("s", 100)
    assert positions == [] and closes == [] and state["cash_balance"] == 1000 and complete is False
    assert not dex.orders("s", pending=True)


def test_actual_template_clears_prospective_book_values():
    template = dict(
        id="p",
        cost=100,
        entry_price=100,
        position_size=1,
        value_at_close=999,
        exit_price=999,
        exit_reason="modeled",
        exit_timestamp="future",
        orders=[dict(side="exit_long", price=90, status="filled")],
    )
    actual = dict(id="p", asset="BTC", entry_amount=2, entry_price=200, entry_time="2026-10-29T00:00:00+00:00")
    activated = confirmed.activate_template(template, actual)
    assert activated["cost"] == 400 and activated["position_size"] == 2 and activated["entry_price"] == 200
    assert all(activated[key] is None for key in ("value_at_close", "exit_price", "exit_reason", "exit_timestamp"))
    assert activated["orders"][0]["price"] == 90 and activated["orders"][0]["status"] == "pending"
    assert template["cost"] == 100  # input proposal stays immutable


def test_exit_batch_defers_entry_and_continues_distinct_position_after_uncertainty(ledger, monkeypatch):
    reserve_fill(pid="p1", key="e1")
    reserve_fill(pid="p2", key="e2")
    sdk = MagicMock()
    data = dict(
        success=True,
        execution_state=dict(risk_state=dict(close_history_bootstrapped=True)),
        proposed_positions=[dict(id="new", execution_confirmed=False)],
        open_positions=[],
        new_orders=[
            dict(
                order_id=pid,
                asset="BTC-USD",
                side="exit_long",
                status="filled",
                position_size=2,
                price=90,
                position_id=pid,
            )
            for pid in ("p1", "p2")
        ]
        + [
            dict(
                order_id="entry",
                asset="BTC-USD",
                side="enter_long",
                status="filled",
                position_size=1,
                price=100,
                position_id="new",
            )
        ],
    )
    sdk.execution.evaluate_by_object.return_value = SimpleNamespace(
        new_orders=data["new_orders"], model_dump=lambda: data
    )
    monkeypatch.setattr(strategy_service, "mangrove_ai_client", lambda: sdk)
    monkeypatch.setattr(strategy_service.order_executor, "_fetch_mark_price", lambda _: 100)
    monkeypatch.setattr(dex, "reconcile", lambda _: None)
    seen = []

    def execute(oid, intent, *args):
        seen.append(intent.engine_position_id)
        if intent.engine_position_id == "p1":
            dex.update(oid, status="uncertain")
            raise TimeoutError("ambiguous first position")

    monkeypatch.setattr(dex, "execute", execute)
    strategy_service.tick("s")
    assert seen == ["p1", "p2"]
    assert len(dex.orders("s", pending=True)) == 2
    assert not any(json.loads(o["intent_json"])["engine_position_id"] == "new" for o in dex.orders("s"))
