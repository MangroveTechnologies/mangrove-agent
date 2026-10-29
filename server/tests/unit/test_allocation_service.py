"""Unit tests for allocation_service — record/release/get."""

from __future__ import annotations

import os

os.environ.setdefault("ENVIRONMENT", "test")

import pytest  # noqa: E402


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    db_file = tmp_path / "test_alloc.db"
    from src.config import app_config
    from src.shared.db import sqlite as db_mod

    monkeypatch.setattr(app_config, "DB_PATH", str(db_file))
    db_mod.reset_connection()
    from src.shared.db.sqlite import get_connection, init_db

    init_db()

    # Seed a strategy row.
    conn = get_connection()
    conn.execute(
        """INSERT INTO strategies
           (id, mangrove_id, name, asset, timeframe, status,
            entry_json, exit_json, execution_config_json,
            generation_report_json, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            "s1",
            "mg-s1",
            "test",
            "ETH",
            "1h",
            "paper",
            "[]",
            "[]",
            "{}",
            None,
            "2026-04-18T00:00:00+00:00",
            "2026-04-18T00:00:00+00:00",
        ),
    )
    # Seed a stored wallet (plausible address + placeholder encrypted secret).
    conn.execute(
        """INSERT INTO wallets
           (id, address, chain, network, chain_id, encrypted_secret,
            encryption_method, label, created_at, metadata_json)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        ("w1", "0xabc", "evm", "testnet", 84532, b"ciphertext", "fernet-v1", "test", "2026-04-18T00:00:00+00:00", None),
    )
    conn.commit()

    yield db_file
    db_mod.reset_connection()


@pytest.fixture(autouse=True)
def stub_wallet_exists(monkeypatch):
    """Default: the seeded wallet 0xabc exists; anything else doesn't."""

    def _fake_exists(addr: str) -> bool:
        return addr == "0xabc"

    monkeypatch.setattr("src.services.allocation_service.wallet_exists", _fake_exists)


def test_record_allocation_happy_path(temp_db):
    from src.services.allocation_service import record_allocation

    alloc = record_allocation("s1", "0xabc", "0xusdc", "USDC", 100.0)
    assert alloc.active is True
    assert alloc.released_at is None
    assert alloc.amount == 100.0
    assert alloc.token_symbol == "USDC"


def test_record_allocation_zero_amount_raises(temp_db):
    from src.services.allocation_service import record_allocation
    from src.shared.errors import AllocationInsufficient

    with pytest.raises(AllocationInsufficient):
        record_allocation("s1", "0xabc", "0xusdc", "USDC", 0)

    with pytest.raises(AllocationInsufficient):
        record_allocation("s1", "0xabc", "0xusdc", "USDC", -5.0)


def test_record_allocation_unknown_wallet_raises(temp_db):
    from src.services.allocation_service import record_allocation
    from src.shared.errors import WalletNotFound

    with pytest.raises(WalletNotFound):
        record_allocation("s1", "0xdeadbeef", "0xusdc", "USDC", 100.0)


def test_get_active_allocation(temp_db):
    from src.services.allocation_service import get_active_allocation, record_allocation

    assert get_active_allocation("s1") is None
    record_allocation("s1", "0xabc", "0xusdc", "USDC", 100.0)
    active = get_active_allocation("s1")
    assert active is not None
    assert active.amount == 100.0


def test_release_allocation(temp_db):
    from src.services.allocation_service import (
        get_active_allocation,
        record_allocation,
        release_allocation,
    )

    record_allocation("s1", "0xabc", "0xusdc", "USDC", 100.0)
    released = release_allocation("s1")
    assert released is not None
    assert released.active is False
    assert released.released_at is not None

    assert get_active_allocation("s1") is None


def test_release_allocation_no_active_returns_none(temp_db):
    from src.services.allocation_service import release_allocation

    # No prior record_allocation call
    assert release_allocation("s1") is None


def test_release_allocation_idempotent(temp_db):
    from src.services.allocation_service import record_allocation, release_allocation

    record_allocation("s1", "0xabc", "0xusdc", "USDC", 100.0)
    first = release_allocation("s1")
    second = release_allocation("s1")
    assert first is not None
    assert second is None  # already released


def test_active_execution_allocation_is_immutable_and_idempotent(temp_db):
    from src.services.allocation_service import get_active_allocation, record_allocation
    from src.shared.errors import AllocationInsufficient

    first = record_allocation("s1", "0xabc", "0xusdc", "USDC", 100.0)
    assert record_allocation("s1", "0xabc", "0xusdc", "USDC", 100.0).id == first.id
    with pytest.raises(AllocationInsufficient, match="cannot be replaced"):
        record_allocation("s1", "0xabc", "0xusdc", "USDC", 200.0)
    assert get_active_allocation("s1").amount == 100.0
