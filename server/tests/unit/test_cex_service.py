"""CEX (Kraken) BYOK service + encrypted-creds tests. No real key/network."""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone

os.environ.setdefault("ENVIRONMENT", "test")

import pytest

from src.config import app_config
from src.services import cex_credentials, cex_service
from src.services.secret_vault import vault
from src.shared.crypto import fernet


@pytest.fixture
def tmp_env(tmp_path, monkeypatch):
    # Temp master key (no real keychain) + temp agent-data dir.
    monkeypatch.setattr("keyring.get_password", lambda s, u: None)
    monkeypatch.setattr("keyring.set_password", lambda s, u, p: None)
    monkeypatch.setattr(app_config, "MASTER_KEY_PATH", str(tmp_path / "master.key"), raising=False)
    monkeypatch.setattr(app_config, "DB_PATH", str(tmp_path / "agent.db"), raising=False)
    fernet.reset_master_key_cache()
    vault.clear()
    yield tmp_path
    fernet.reset_master_key_cache()
    vault.clear()


def test_credentials_roundtrip_encrypted_at_rest(tmp_env):
    cex_credentials.save("kraken", "K-API-KEY", "S-SECRET-VALUE")
    assert cex_credentials.is_connected("kraken")
    enc = (tmp_env / "cex-kraken.enc").read_bytes()
    assert b"K-API-KEY" not in enc and b"S-SECRET-VALUE" not in enc   # ciphertext
    assert cex_credentials.load("kraken") == ("K-API-KEY", "S-SECRET-VALUE")
    assert cex_credentials.disconnect("kraken") is True
    assert cex_credentials.load("kraken") is None


def test_connect_from_vault_consumes_token(tmp_env):
    token = vault.stash(json.dumps({"api_key": "K", "api_secret": "S"}))
    assert cex_service.connect_from_vault(token)["connected"] is True
    assert cex_service.status() == {"venue": "kraken", "connected": True}
    with pytest.raises(KeyError):       # single-read: token now consumed
        vault.reveal(token)


class _FakeKraken:
    def __init__(self, key, secret):
        self.key, self.secret = key, secret

    def balance(self):
        return {"ZUSD": "100.0"}

    def add_order(self, **kw):
        assert kw["validate"] is True   # service must dry-run
        return {"descr": {"order": "buy 0.01 XBTUSD @ market"}}

    def trades_as_records(self, *, mode="live"):
        from mangrove_markets import TradeRecord
        return [TradeRecord(
            id="kraken:T1", mode=mode, status="confirmed", venue="kraken",
            venue_trade_ref="T1", side="buy", base="XXBTZUSD", qty=0.01,
            fill_price=64000.0, executed_at=datetime(2026, 6, 27, tzinfo=timezone.utc),
        )]


class _FakeTelemetry:
    def __init__(self):
        self.sent = []

    def report_trades(self, records):
        self.sent.extend(records)
        return [{"stored": True, "id": r.id} for r in records]


def test_balances_and_validate_use_byok_client(tmp_env):
    cex_credentials.save("kraken", "K", "S")
    factory = lambda k, s: _FakeKraken(k, s)  # noqa: E731
    assert cex_service.get_balances(client_factory=factory)["ZUSD"] == "100.0"
    out = cex_service.validate_order(pair="XBTUSD", side="buy", volume=0.01, client_factory=factory)
    assert "descr" in out


def test_sync_fills_emits_to_telemetry(tmp_env):
    cex_credentials.save("kraken", "K", "S")
    tel = _FakeTelemetry()
    out = cex_service.sync_fills(client_factory=lambda k, s: _FakeKraken(k, s), telemetry=tel)
    assert out == {"emitted": 1, "trade_ids": ["kraken:T1"]}
    assert tel.sent[0].venue == "kraken"
    assert tel.sent[0].tx_hash is None      # CEX spot — no chain hash


def test_operations_without_creds_raise(tmp_env):
    with pytest.raises(RuntimeError):
        cex_service.get_balances(client_factory=lambda k, s: _FakeKraken(k, s))
