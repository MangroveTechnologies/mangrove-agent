"""Integration tests for shared/db/sqlite.py — connection + migrations."""
from __future__ import annotations

import os

os.environ.setdefault("ENVIRONMENT", "test")

import pytest  # noqa: E402


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    """Point DB_PATH at a tmp file for this test."""
    db_file = tmp_path / "test_agent.db"

    from src.config import app_config
    from src.shared.db import sqlite as db_mod

    monkeypatch.setattr(app_config, "DB_PATH", str(db_file))
    db_mod.reset_connection()
    yield db_file
    db_mod.reset_connection()


EXPECTED_TABLES = {
    "wallets",
    "strategies",
    "allocations",
    "evaluations",
    "trades",
    "positions",
    "_migrations",
}

# (table, expected columns subset — just the critical ones we committed to in the spec)
EXPECTED_COLUMNS = {
    "wallets": {"id", "address", "chain", "network", "chain_id",
                "encrypted_secret", "encryption_method", "label",
                "created_at", "metadata_json"},
    "strategies": {"id", "mangrove_id", "name", "asset", "timeframe", "status",
                   "entry_json", "exit_json", "execution_config_json",
                   "generation_report_json", "created_at", "updated_at"},
    "allocations": {"id", "strategy_id", "wallet_address", "token_address",
                    "token_symbol", "amount", "active", "created_at", "released_at"},
    "evaluations": {"id", "strategy_id", "timestamp", "market_snapshot_json",
                    "sdk_response_json", "order_intents_json",
                    "duration_ms", "status", "error_msg"},
    "trades": {"id", "strategy_id", "evaluation_id", "order_intent_json",
               "mode", "tx_hash", "input_token", "input_amount",
               "output_token", "output_amount", "fill_price", "fees_json",
               "status", "executed_at", "confirmed_at", "p_and_l"},
    "positions": {"id", "strategy_id", "asset", "entry_trade_id",
                  "exit_trade_id", "entry_price", "entry_amount", "entry_time",
                  "exit_price", "exit_amount", "exit_time", "status",
                  "stop_loss", "take_profit"},
}


def test_init_db_creates_all_tables(temp_db):
    from src.shared.db.sqlite import get_connection, init_db

    applied = init_db()
    assert "001_initial.sql" in applied

    conn = get_connection()
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()
    tables = {r["name"] for r in rows}
    assert EXPECTED_TABLES.issubset(tables), f"missing: {EXPECTED_TABLES - tables}"


@pytest.mark.parametrize("table,cols", EXPECTED_COLUMNS.items())
def test_table_has_expected_columns(temp_db, table, cols):
    from src.shared.db.sqlite import get_connection, init_db

    init_db()
    conn = get_connection()
    info = conn.execute(f"PRAGMA table_info({table})").fetchall()
    actual = {r["name"] for r in info}
    assert cols.issubset(actual), f"{table}: missing {cols - actual}"


def test_init_db_is_idempotent(temp_db):
    from src.shared.db.sqlite import init_db

    first = init_db()
    second = init_db()
    assert len(first) >= 1
    assert second == []  # nothing new to apply


def test_foreign_keys_enabled(temp_db):
    from src.shared.db.sqlite import get_connection, init_db

    init_db()
    conn = get_connection()
    (fk_on,) = conn.execute("PRAGMA foreign_keys").fetchone()
    assert fk_on == 1


def test_insert_and_query_roundtrip(temp_db):
    from src.shared.db.sqlite import get_connection, init_db

    init_db()
    conn = get_connection()
    conn.execute(
        """INSERT INTO wallets
           (id, address, chain, network, chain_id, encrypted_secret,
            encryption_method, label, created_at, metadata_json)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        ("w1", "0xabc", "evm", "testnet", 84532, b"ciphertext",
         "fernet-v1", "test wallet", "2026-04-18T00:00:00Z", None),
    )
    conn.commit()

    row = conn.execute("SELECT * FROM wallets WHERE id = ?", ("w1",)).fetchone()
    assert row["address"] == "0xabc"
    assert row["encrypted_secret"] == b"ciphertext"
    assert row["chain"] == "evm"


def test_reset_connection_logs_warning_on_close_failure(monkeypatch):
    """If close() raises a sqlite3.Error, a warning is logged and cache_clear still runs."""
    import sqlite3
    from unittest.mock import MagicMock

    from src.shared.db import sqlite as db_mod

    mock_conn = MagicMock()
    mock_conn.close.side_effect = sqlite3.Error("forced close failure")

    mock_get_connection = MagicMock(return_value=mock_conn)
    mock_get_connection.cache_clear = MagicMock()
    monkeypatch.setattr(db_mod, "get_connection", mock_get_connection)

    mock_log = MagicMock()
    monkeypatch.setattr(db_mod, "_log", mock_log)

    db_mod.reset_connection()

    mock_log.warning.assert_called_once_with(
        "db.connection.close_failed", error="forced close failure"
    )
    mock_get_connection.cache_clear.assert_called_once()
