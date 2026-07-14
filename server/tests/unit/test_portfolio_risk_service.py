"""Unit tests for portfolio_risk_service — the agent-side kill switch (#146).

Covers the realized-P&L book value, high-water-mark tracking + re-baseline,
the 30% latched trip that pauses all live strategies, and human reset.
"""
from __future__ import annotations

import os

os.environ.setdefault("ENVIRONMENT", "test")

import pytest  # noqa: E402

_T0 = "2026-04-18T00:00:00+00:00"


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    db_file = tmp_path / "test_portfolio.db"
    from src.config import app_config
    from src.shared.db import sqlite as db_mod

    monkeypatch.setattr(app_config, "DB_PATH", str(db_file))
    # Deterministic 30% limit regardless of local config drift.
    monkeypatch.setattr(app_config, "PORTFOLIO_MAX_DRAWDOWN_PCT", 0.30, raising=False)
    db_mod.reset_connection()
    from src.shared.db.sqlite import get_connection, init_db
    init_db()

    conn = get_connection()
    # A wallet for allocation FKs.
    conn.execute(
        """INSERT INTO wallets (id, address, chain, network, chain_id,
             encrypted_secret, encryption_method, label, created_at, metadata_json)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        ("w1", "0xabc", "evm", "testnet", 84532, b"x", "fernet-v1", "t", _T0, None),
    )
    conn.commit()
    yield conn
    db_mod.reset_connection()


def _seed_strategy(conn, sid, status="live"):
    conn.execute(
        """INSERT INTO strategies (id, mangrove_id, name, asset, timeframe, status,
             entry_json, exit_json, execution_config_json, generation_report_json,
             created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (sid, f"mg-{sid}", sid, "ETH", "1h", status, "[]", "[]", "{}", None, _T0, _T0),
    )
    conn.commit()


def _seed_allocation(conn, sid, amount):
    conn.execute(
        """INSERT INTO allocations (id, strategy_id, wallet_address, token_address,
             token_symbol, amount, active, created_at, released_at)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (f"a-{sid}", sid, "0xabc", "0xusdc", "USDC", amount, 1, _T0, None),
    )
    conn.commit()


def _seed_trade(conn, sid, pnl, tid=None):
    conn.execute(
        """INSERT INTO trades (id, strategy_id, evaluation_id, order_intent_json, mode,
             tx_hash, input_token, input_amount, output_token, output_amount,
             fill_price, fees_json, status, executed_at, confirmed_at, p_and_l)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (tid or f"t-{sid}-{pnl}", sid, None, "{}", "live", None, "USDC", 100.0,
         "ETH", 0.05, 2000.0, "{}", "confirmed", _T0, _T0, pnl),
    )
    conn.commit()


# --------------------------------------------------------------------------
# book value
# --------------------------------------------------------------------------

def test_book_value_is_live_allocations_plus_realized_pnl(temp_db):
    from src.services import portfolio_risk_service as prs
    _seed_strategy(temp_db, "s1", "live")
    _seed_strategy(temp_db, "s2", "live")
    _seed_strategy(temp_db, "s3", "paper")       # excluded (not live)
    _seed_allocation(temp_db, "s1", 5000)
    _seed_allocation(temp_db, "s2", 5000)
    _seed_allocation(temp_db, "s3", 9999)        # excluded (paper)
    _seed_trade(temp_db, "s1", -1000)
    _seed_trade(temp_db, "s2", +250)
    _seed_trade(temp_db, "s3", -8000, tid="tx")  # excluded (paper)
    # 10000 committed + (-1000 + 250) realized = 9250
    assert prs.compute_book_value() == pytest.approx(9250.0)


# --------------------------------------------------------------------------
# high-water mark + healthy pass
# --------------------------------------------------------------------------

def test_healthy_tick_advances_hwm_and_allows(temp_db):
    from src.services import portfolio_risk_service as prs
    _seed_strategy(temp_db, "s1", "live")
    _seed_allocation(temp_db, "s1", 10000)
    r = prs.check_before_live_execution()
    assert r["allowed"] is True
    assert r["high_water_mark"] == pytest.approx(10000.0)
    # A small gain lifts the peak; still allowed.
    _seed_trade(temp_db, "s1", +500)
    r = prs.check_before_live_execution()
    assert r["allowed"] is True
    assert r["high_water_mark"] == pytest.approx(10500.0)


def test_rebaseline_resets_hwm_to_current_book(temp_db):
    from src.services import portfolio_risk_service as prs
    _seed_strategy(temp_db, "s1", "live")
    _seed_allocation(temp_db, "s1", 10000)
    prs.check_before_live_execution()             # hwm -> 10000
    _seed_trade(temp_db, "s1", -2000)             # book -> 8000
    prs.rebaseline(reason="test")
    assert prs.get_status()["high_water_mark"] == pytest.approx(8000.0)
    # From the new baseline, an 8% dip does not trip.
    _seed_trade(temp_db, "s1", -640)              # book 7360, dd 8% of 8000
    assert prs.check_before_live_execution()["allowed"] is True


# --------------------------------------------------------------------------
# the trip: latched, pauses all live
# --------------------------------------------------------------------------

def test_trips_at_limit_pauses_all_live_and_latches(temp_db, monkeypatch):
    from src.services import portfolio_risk_service as prs
    from src.services import strategy_service

    # Spy that pauses locally without the SDK/scheduler round-trip.
    paused = []
    def fake_update_status(sid, update):
        paused.append((sid, update.status))
        temp_db.execute("UPDATE strategies SET status = ? WHERE id = ?", (update.status, sid))
        temp_db.commit()
    monkeypatch.setattr(strategy_service, "update_status", fake_update_status)

    _seed_strategy(temp_db, "s1", "live")
    _seed_strategy(temp_db, "s2", "live")
    _seed_allocation(temp_db, "s1", 5000)
    _seed_allocation(temp_db, "s2", 5000)
    prs.check_before_live_execution()             # hwm -> 10000
    # Realized -3500 across the book -> book 6500 -> 35% drawdown (>= 30%).
    _seed_trade(temp_db, "s1", -2000)
    _seed_trade(temp_db, "s2", -1500)

    r = prs.check_before_live_execution()
    assert r["allowed"] is False
    assert r["tripped"] is True
    assert r["drawdown"] == pytest.approx(0.35, abs=1e-3)
    # Every live strategy was paused.
    assert sorted(paused) == [("s1", "inactive"), ("s2", "inactive")]
    # Latch persisted.
    st = prs.get_status()
    assert st["tripped"] is True
    assert "35" in (st["tripped_reason"] or "") or "0.35" in (st["tripped_reason"] or "")


def test_latched_switch_blocks_subsequent_ticks_until_reset(temp_db, monkeypatch):
    from src.services import portfolio_risk_service as prs
    from src.services import strategy_service
    monkeypatch.setattr(strategy_service, "update_status", lambda *a, **k: None)

    _seed_strategy(temp_db, "s1", "live")
    _seed_allocation(temp_db, "s1", 10000)
    prs.check_before_live_execution()             # hwm 10000
    _seed_trade(temp_db, "s1", -4000)             # 40% dd -> trip
    assert prs.check_before_live_execution()["allowed"] is False

    # Even if the book fully recovers, the latch keeps it blocked.
    _seed_trade(temp_db, "s1", +4000)             # book back to 10000
    r = prs.check_before_live_execution()
    assert r["allowed"] is False and r["tripped"] is True

    # Human reset clears the latch and re-baselines to current book.
    out = prs.reset()
    assert out["tripped"] is False
    assert out["high_water_mark"] == pytest.approx(10000.0)
    assert prs.check_before_live_execution()["allowed"] is True


def test_no_trip_when_book_is_zero(temp_db):
    """No live allocations -> book 0, hwm 0 -> no division, no spurious trip."""
    from src.services import portfolio_risk_service as prs
    r = prs.check_before_live_execution()
    assert r["allowed"] is True
    assert r["drawdown"] == 0.0
