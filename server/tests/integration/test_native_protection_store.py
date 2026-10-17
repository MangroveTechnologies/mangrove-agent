"""Offline store and status tests for native protective-stop coverage."""

import pytest

from src.config import app_config
from src.models.domain import Evaluation
from src.services import broker_execution as execution
from src.services import broker_store as store
from src.services import trade_log
from src.shared.db import sqlite as db
from src.shared.errors import SdkError
from tests.integration.test_robinhood_execution import reserve, snapshot


@pytest.fixture
def protection_ledger(tmp_path, monkeypatch):
    db.reset_connection()
    monkeypatch.setattr(app_config, "DB_PATH", str(tmp_path / "protection.db"))
    db.init_db()
    with store.connection() as conn:
        conn.execute(
            """INSERT INTO strategies(id,mangrove_id,name,asset,timeframe,status,entry_json,
            exit_json,execution_config_json,created_at,updated_at,evaluation_lane)
            VALUES ('s1','mg-s1','s1','BTC-USD','1h','live','[]','[]','{}',?,?,'stateless')""",
            (store.now(), store.now()),
        )
        conn.execute(
            """INSERT INTO broker_routes
            (strategy_id,venue,account_number,rhs_account_number,asset_class,capital,max_order_usd,
             slippage_pct,created_at)
            VALUES ('s1','robinhood','eligible','numeric','crypto','1000','500','0.0025',?)""",
            (store.now(),),
        )
    trade_log.log_evaluation(
        Evaluation(
            id="eval", strategy_id="s1", timestamp=trade_log.now_utc(), duration_ms=0,
            status="ok", sdk_response={"execution_state": {"bar_index": 1}},
        )
    )
    yield
    db.reset_connection()


def enable_protection(*, mode="confirmed_v2", status="live"):
    with store.connection() as conn:
        conn.execute(
            "UPDATE broker_routes SET external_execution_mode=?,native_stop_loss_enabled=1 WHERE strategy_id='s1'",
            (mode,),
        )
        conn.execute("UPDATE strategies SET status=? WHERE id='s1'", (status,))


def seed_confirmed_position():
    assert reserve(side="buy", qty=2, oid="entry", key="entry", position_id="p1")
    store.apply_snapshot(
        "entry", snapshot(qty="2", price="100", state="filled", oid="entry"), asset_class="crypto"
    )


def reserve_stop(quantity="2", ref_id="stop-local"):
    request = {
        "rhs_account_number": "numeric",
        "symbol": "BTC-USD",
        "side": "sell",
        "type": "stop_loss",
        "quantity": str(quantity),
        "stop_price": "90",
        "time_in_force": "gtc",
    }
    template = {"id": "p1", "asset": "BTC-USD", "stop_loss": 90}
    return store.reserve_protective_stop(
        "s1", "eval", "p1", "BTC-USD", quantity, "90", "90", request, template, ref_id
    )


def stop_snapshot(*, state="queued", quantity="2", cumulative="0", stop_price="90", order_type="stop_loss"):
    saved = snapshot(
        qty=cumulative,
        price="90",
        state=state,
        side="sell",
        oid="stop-local",
        net=str(float(cumulative) * 90),
    )
    saved.update(type=order_type, stop_price=stop_price, quantity=quantity)
    return saved


def test_stop_reservation_is_default_off(protection_ledger):
    assert not reserve_stop()
    assert store.orders("s1", order_kind="protective_stop") == []


def test_stop_reservation_requires_confirmed_v2_and_live_strategy(protection_ledger):
    for mode, status in (("legacy", "live"), ("confirmed_v2", "inactive")):
        enable_protection(mode=mode, status=status)
        assert not reserve_stop(ref_id=f"stop-{mode}-{status}")
    assert store.orders("s1", order_kind="protective_stop") == []


def test_stop_reservation_rechecks_exact_confirmed_holding(protection_ledger):
    enable_protection()
    seed_confirmed_position()

    assert not reserve_stop(quantity="1")
    assert store.orders("s1", order_kind="protective_stop") == []


def test_stop_reservation_waits_for_existing_pending_order(protection_ledger):
    enable_protection()
    seed_confirmed_position()
    assert reserve(side="sell", qty=1, oid="pending-exit", key="exit", position_id="p1")

    assert not reserve_stop()
    assert store.orders("s1", order_kind="protective_stop") == []


def test_valid_broker_stop_snapshot_is_last_observed_exact_coverage(protection_ledger):
    enable_protection()
    seed_confirmed_position()
    assert reserve_stop()
    store.apply_snapshot("stop-local", stop_snapshot(), asset_class="crypto")
    store.set_protection_state(
        "s1", "p1", stop_price="90", routed_stop_price="90", quantity="2",
        status="standing_coverage", active_order_id="stop-local",
    )

    status = execution.protection_status("s1")

    assert status["positions"][0]["status"] == "standing_coverage_last_observed"
    assert status["positions"][0]["broker_acknowledged_standing"] is True


@pytest.mark.parametrize(
    ("snapshot_changes", "message"),
    [
        ({"type": "limit"}, "order type or stop price"),
        ({"stop_price": "89"}, "order type or stop price"),
    ],
)
def test_stop_snapshot_type_or_price_mismatch_never_claims_coverage(protection_ledger, snapshot_changes, message):
    enable_protection()
    seed_confirmed_position()
    assert reserve_stop()
    broker_snapshot = stop_snapshot()
    broker_snapshot.update(snapshot_changes)

    with pytest.raises(SdkError, match=message):
        store.apply_snapshot("stop-local", broker_snapshot, asset_class="crypto")

    status = execution.protection_status("s1")["positions"][0]
    assert status["broker_acknowledged_standing"] is False
    assert status["status"] != "standing_coverage_last_observed"


@pytest.mark.parametrize("accepted_quantity", ["1", None])
def test_stop_snapshot_quantity_mismatch_never_claims_coverage(protection_ledger, accepted_quantity):
    enable_protection()
    seed_confirmed_position()
    assert reserve_stop()
    observed = stop_snapshot(quantity=accepted_quantity)
    if accepted_quantity is None:
        observed.pop("quantity")
    store.apply_snapshot("stop-local", observed, asset_class="crypto")
    store.set_protection_state(
        "s1", "p1", stop_price="90", routed_stop_price="90", quantity="2",
        status="reconciling", active_order_id="stop-local",
    )

    status = execution.protection_status("s1")["positions"][0]

    expected = "coverage_unverified" if accepted_quantity is None else "quantity_or_level_mismatch"
    assert status["status"] == expected
    assert status["broker_acknowledged_standing"] is False
    assert not store.protective_snapshot_quantity_matches("stop-local")


def test_terminal_stop_fill_reduces_confirmed_coverage_to_flat(protection_ledger):
    enable_protection()
    seed_confirmed_position()
    assert reserve_stop()
    store.apply_snapshot("stop-local", stop_snapshot(), asset_class="crypto")
    store.set_protection_state(
        "s1", "p1", stop_price="90", routed_stop_price="90", quantity="2",
        status="standing_coverage", active_order_id="stop-local",
    )

    store.apply_snapshot(
        "stop-local",
        stop_snapshot(state="filled", quantity="2", cumulative="2"),
        asset_class="crypto",
    )

    status = execution.protection_status("s1")["positions"][0]
    assert status["status"] == "flat"
    assert status["confirmed_quantity"] == "0"
    assert status["broker_acknowledged_standing"] is False
