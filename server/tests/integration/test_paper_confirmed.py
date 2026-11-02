"""Stateless paper uses its actual simulated journal, separately from live."""

import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.models.domain import OrderIntent
from src.services import broker_store as store
from src.services import confirmed_execution as confirmed
from src.services import paper_confirmed as paper
from src.services import strategy_service as service
from src.shared.db import sqlite as db
from src.shared.errors import SdkError
from tests.integration.test_confirmed_execution import seed_database


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    seed_database(tmp_path / "paper.db")
    with store.connection() as conn:
        conn.execute("UPDATE strategies SET status='paper',evaluation_lane='stateless',execution_config_json=? WHERE id='s'",
                     (json.dumps(dict(initial_balance=1000, position_size_calc="v2")),))
    monkeypatch.setattr(paper.order_executor, "_fetch_mark_price", lambda _: 100)
    paper.inputs("s", service._get_row("s"), 100)
    yield
    db.reset_connection()


def intent(action="enter", key="entry", pid="p", qty=2):
    return OrderIntent(action=action, side="buy" if action == "enter" else "sell", symbol="BTC",
                       amount=qty, engine_position_id=pid, engine_order_id=key, stop_loss=90, take_profit=120)


def template(pid="p"):
    return dict(id=pid, execution_confirmed=False, stop_loss_price=90, take_profit_price=120,
                orders=[dict(side="exit_long", order_type="stop_loss", price=90, status="pending")])


def fill(action="enter", key="entry", pid="p", qty=2):
    return paper.fill("s", "eval", intent(action, key, pid, qty), template(pid), confirmed.snapshot_revision("s"))


def test_entry_activation_actual_cash_close_pnl_and_duplicate_fill(ledger, monkeypatch):
    assert fill()
    assert not fill()
    state, positions, closes, complete = paper.inputs("s", service._get_row("s"), 110)
    assert complete and state["cash_balance"] == 800 and state["account_value"] == 1020
    assert len(positions) == 1 and positions[0]["entry_price"] == 100 and positions[0]["position_size"] == 2
    assert positions[0]["orders"][0]["status"] == "pending" and closes == []
    monkeypatch.setattr(paper.order_executor, "_fetch_mark_price", lambda _: 90)
    assert fill("exit", "exit")
    assert not fill("exit", "exit")
    state, positions, closes, complete = paper.inputs("s", service._get_row("s"), 110)
    assert complete and positions == [] and state["cash_balance"] == 980
    assert len(closes) == 1 and closes[0]["realized_pnl"] == -20
    with store.connection() as conn:
        assert conn.execute("SELECT count(*) FROM trades WHERE mode='paper'").fetchone()[0] == 2


def test_book_revision_rejects_stale_fill_before_any_journal_mutation(ledger):
    revision = confirmed.snapshot_revision("s")
    with store.connection() as conn:
        conn.execute("UPDATE strategies SET book_revision=book_revision+1 WHERE id='s'")
    with pytest.raises(SdkError, match="book changed"):
        paper.fill("s", "eval", intent(), template(), revision)
    with store.connection() as conn:
        assert conn.execute("SELECT count(*) FROM paper_fills").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM trades").fetchone()[0] == 0


def test_close_delivery_is_confirmed_and_acknowledged_once(ledger):
    fill()
    fill("exit", "exit")
    before, _, closes, complete = paper.inputs("s", service._get_row("s"), 100)
    sent, bootstrap = confirmed.close_delivery(before, closes, complete=complete)
    assert bootstrap and len(sent) == 1
    returned = dict(execution_state=dict(risk_state=dict(close_history_bootstrapped=True,
                    recent_close_ids=["p"], closes_booked_through=sent[0]["booked_at"])))
    assert confirmed.save_risk("s", before, returned, sent, confirmed.snapshot_revision("s"), complete=True,
                               state_field="paper_execution_state_json")
    after = paper.inputs("s", service._get_row("s"), 100)[0]
    assert confirmed.close_delivery(after, closes, complete=True) == ([], False)


def test_live_facts_are_never_paper_positions_or_cash(ledger):
    with store.connection() as conn:
        conn.execute("INSERT INTO trades(id,strategy_id,evaluation_id,order_intent_json,mode,input_token,input_amount,output_token,output_amount,fill_price,fees_json,status,executed_at) VALUES ('live','s','eval','{}','live','USDC',200,'BTC',2,100,'{}','confirmed',?)", (store.now(),))
        conn.execute("INSERT INTO positions(id,strategy_id,asset,entry_trade_id,entry_price,entry_amount,entry_time,status) VALUES ('live','s','BTC','live',100,2,?,'open')", (store.now(),))
    state, positions, closes, complete = paper.inputs("s", service._get_row("s"), 100)
    assert not complete and positions == [] and closes == [] and state["cash_balance"] == 1000
    with pytest.raises(SdkError, match="exact paper-owned"):
        fill("exit", "exit", "live")
    with store.connection() as conn:
        conn.execute("UPDATE positions SET status='closed' WHERE id='live'")
    state, positions, closes, complete = paper.inputs("s", service._get_row("s"), 100)
    assert complete and positions == [] and closes == [] and state["cash_balance"] == 1000
    assert fill()


def test_capital_is_immutable_and_proposed_balances_do_not_become_cash(ledger):
    with store.connection() as conn:
        conn.execute("UPDATE strategies SET execution_config_json=?,execution_state_json=? WHERE id='s'",
                     (json.dumps(dict(initial_balance=999999)), json.dumps(dict(cash_balance=999999))))
    state = paper.inputs("s", service._get_row("s"), 100)[0]
    assert state["cash_balance"] == state["reference_capital"] == state["cumulative_deposits"] == 1000


def test_human_paper_halt_retains_monitor_and_blocks_entries_but_allows_exit(ledger, monkeypatch):
    fill()
    scheduler = MagicMock()
    monkeypatch.setattr(service, "scheduler_service", scheduler)
    row = service._get_row("s")
    service._apply_scheduler_effects("s", row, "inactive", "paper")
    with store.connection() as conn:
        conn.execute("UPDATE strategies SET status='inactive' WHERE id='s'")
    assert service._get_row("s")["exit_only_mode"] == "paper"
    scheduler.register_job.assert_called_once()
    with pytest.raises(SdkError, match="active empty"):
        fill(pid="other", key="other")
    assert fill("exit", "exit")
    release = MagicMock()
    monkeypatch.setattr(service.allocation_service, "release_allocation", release)
    service._finish_exit_monitor_if_flat("s")
    assert service._get_row("s")["exit_only_mode"] is None
    release.assert_not_called()


def test_unknown_legacy_history_holds_entries_and_retains_recorded_exit_template(ledger):
    fill()
    with store.connection() as conn:
        conn.execute("DELETE FROM paper_fills")
        conn.execute("UPDATE strategies SET open_positions_json=? WHERE id='s'", (json.dumps([template()]),))
    _, positions, _, complete = paper.inputs("s", service._get_row("s"), 100)
    assert not complete and positions[0]["orders"][0]["price"] == 90


def test_v3_is_explicitly_refused_before_evaluation(ledger):
    with store.connection() as conn:
        conn.execute("UPDATE strategies SET execution_config_json=? WHERE id='s'", (json.dumps(dict(position_size_calc="v3")),))
    with pytest.raises(SdkError, match="v3 core allocations are unsupported"):
        paper.inputs("s", service._get_row("s"), 100)


def test_paper_risk_does_not_overwrite_live_state_and_open_paper_blocks_promotion(ledger):
    from src.shared.errors import StrategyInvalidStatusTransition

    sentinel = dict(risk_state=dict(hard_floor_tripped=True), cash_balance=4321)
    with store.connection() as conn:
        conn.execute("UPDATE strategies SET execution_state_json=? WHERE id='s'", (json.dumps(sentinel),))
    before = paper.inputs("s", service._get_row("s"), 100)[0]
    assert "risk_state" not in before
    response = dict(execution_state=dict(risk_state=dict(close_history_bootstrapped=True)))
    assert confirmed.save_risk("s", before, response, [], confirmed.snapshot_revision("s"), complete=True,
                               state_field="paper_execution_state_json")
    assert json.loads(service._get_row("s")["execution_state_json"]) == sentinel
    fill()
    with pytest.raises(StrategyInvalidStatusTransition, match="paper exposure must settle flat"):
        service.update_status("s", service.StrategyStatusUpdate(status="live", confirm=True))


def test_position_write_failure_rolls_back_fill_trade_and_revision(ledger):
    before = confirmed.snapshot_revision("s")
    with store.connection() as conn:
        conn.execute("CREATE TRIGGER reject_paper_position BEFORE INSERT ON positions BEGIN SELECT RAISE(ABORT,'position failure'); END")
    with pytest.raises(sqlite3.IntegrityError, match="position failure"):
        fill()
    assert confirmed.snapshot_revision("s") == before
    with store.connection() as conn:
        assert conn.execute("SELECT count(*) FROM paper_fills").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM trades").fetchone()[0] == 0


def test_legacy_paper_migration_moves_only_proven_paper_risk():
    conn = sqlite3.connect(":memory:")
    conn.executescript("""
      CREATE TABLE strategies(id TEXT PRIMARY KEY,status TEXT,evaluation_lane TEXT,execution_state_json TEXT);
      CREATE TABLE trades(id TEXT PRIMARY KEY,strategy_id TEXT,mode TEXT);
      CREATE TABLE allocations(id TEXT PRIMARY KEY,strategy_id TEXT);
      CREATE TABLE broker_routes(strategy_id TEXT PRIMARY KEY);
    """)
    halt = json.dumps(dict(risk_state=dict(hard_floor_tripped=True, recent_close_ids=["paper-close"])))
    for sid in ("paper", "live-history", "live-allocation", "live-route"):
        conn.execute("INSERT INTO strategies VALUES (?,'paper','stateless',?)", (sid, halt))
    conn.execute("INSERT INTO trades VALUES ('live','live-history','live')")
    conn.execute("INSERT INTO allocations VALUES ('a','live-allocation')")
    conn.execute("INSERT INTO broker_routes VALUES ('live-route')")
    migration = Path(__file__).parents[2] / "src/shared/db/migrations/022_paper_confirmed_execution.sql"
    conn.executescript(migration.read_text())
    moved = conn.execute("SELECT execution_state_json,paper_execution_state_json FROM strategies WHERE id='paper'").fetchone()
    assert moved == (None, halt)
    for sid in ("live-history", "live-allocation", "live-route"):
        assert conn.execute("SELECT execution_state_json,paper_execution_state_json FROM strategies WHERE id=?", (sid,)).fetchone() == (halt, None)
    conn.close()


@pytest.mark.parametrize("prior_unlocked_control", [False, True])
def test_live_transition_serializes_interleaved_paper_tick(ledger, monkeypatch, prior_unlocked_control):
    data = dict(success=True, execution_state=dict(risk_state=dict(close_history_bootstrapped=True)),
                open_positions=[], proposed_positions=[template()],
                new_orders=[dict(order_id="entry", asset="BTC", side="enter_long", status="filled",
                                 position_size=2, price=100, position_id="p")])
    response = SimpleNamespace(new_orders=data["new_orders"], model_dump=lambda: data)
    upstream = MagicMock()
    upstream.execution.evaluate_by_object.return_value = response
    # The upstream status request yields while PAPER is still the local mode.
    # The old wrapper allowed the scheduler tick to fill inside that window.
    upstream.strategies.update_status.side_effect = lambda *_: service.tick("s")
    monkeypatch.setattr(service, "mangrove_ai_client", lambda: upstream)
    monkeypatch.setattr(service, "_record_dex_allocation", lambda *_: None)
    monkeypatch.setattr(service, "scheduler_service", MagicMock())
    from src.services import portfolio_risk_service

    monkeypatch.setattr(portfolio_risk_service, "rebaseline", MagicMock())
    update = service.StrategyStatusUpdate(status="live", confirm=True)
    transition = service._update_status if prior_unlocked_control else service.update_status
    transition("s", update)
    assert service._get_row("s")["status"] == "live"
    with store.connection() as conn:
        fills = conn.execute("SELECT count(*) FROM paper_fills").fetchone()[0]
    assert fills == int(prior_unlocked_control)
    assert paper.has_exposure("s") is prior_unlocked_control


def test_running_tick_refuses_transition_before_upstream_or_allocation(ledger, monkeypatch):
    upstream = MagicMock()
    monkeypatch.setattr(service, "mangrove_ai_client", lambda: upstream)
    with confirmed.tick_lock("s") as acquired:
        assert acquired
        with pytest.raises(SdkError, match="evaluation is in progress"):
            service.update_status("s", service.StrategyStatusUpdate(status="inactive"))
    assert service._get_row("s")["status"] == "paper"
    upstream.strategies.update_status.assert_not_called()


def test_failed_paper_exit_does_not_hide_other_protection_or_dispatch_entry(ledger, monkeypatch):
    fill(pid="p1", key="entry1")
    fill(pid="p2", key="entry2")
    orders = [dict(order_id=pid, asset="BTC", side=side, status="filled", position_size=2, price=100, position_id=pid)
              for pid, side in (("p1", "exit_long"), ("p2", "exit_long"), ("p3", "enter_long"))]
    data = dict(success=True, new_orders=orders, open_positions=[], proposed_positions=[template("p3")],
                execution_state=dict(risk_state=dict(close_history_bootstrapped=True)))
    sdk = MagicMock()
    sdk.execution.evaluate_by_object.return_value = SimpleNamespace(new_orders=orders, model_dump=lambda: data)
    monkeypatch.setattr(service, "mangrove_ai_client", lambda: sdk)
    actual_fill = paper.order_executor._paper_fill

    def fail_first(intent, *args, **kwargs):
        if intent.engine_position_id == "p1":
            raise SdkError("First paper price read unavailable")
        return actual_fill(intent, *args, **kwargs)

    monkeypatch.setattr(paper.order_executor, "_paper_fill", fail_first)
    paper.tick("s")
    with store.connection() as conn:
        assert dict(conn.execute("SELECT id,status FROM positions")) == {"p1": "open", "p2": "closed"}
        assert conn.execute("SELECT count(*) FROM paper_fills").fetchone()[0] == 3
