"""Integration tests for wallet routes — auth, CRUD, SDK pass-throughs."""
from __future__ import annotations

import os
from unittest.mock import MagicMock

os.environ.setdefault("ENVIRONMENT", "test")

import pytest  # noqa: E402
from eth_account import Account  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

_API_KEY = "test-key-1"
_TEST_PRIVKEY = "0x" + "11" * 32
_TEST_ADDRESS = Account.from_key(_TEST_PRIVKEY).address


@pytest.fixture
def client(tmp_path, monkeypatch):
    db_file = tmp_path / "wr.db"
    from src.config import app_config
    from src.services import scheduler_service as ss
    from src.shared.db import sqlite as db_mod

    monkeypatch.setattr(app_config, "DB_PATH", str(db_file))
    db_mod.reset_connection()
    ss.reset_scheduler_cache()

    # Stub keyring (no real keychain inside the container).
    store: dict = {}
    monkeypatch.setattr("keyring.get_password", lambda s, u: store.get((s, u)))
    monkeypatch.setattr("keyring.set_password",
                        lambda s, u, p: store.update({(s, u): p}))
    from src.shared.crypto import fernet as f
    f.reset_master_key_cache()

    # Stub SDK for wallet create + read endpoints.
    create_result = MagicMock()
    create_result.address = _TEST_ADDRESS
    create_result.private_key = _TEST_PRIVKEY
    create_result.seed_phrase = None
    create_result.secret = None

    sdk = MagicMock()
    sdk.wallet.create.return_value = create_result

    # dex.balances
    balances = MagicMock()
    balances.model_dump.return_value = {"balances": [{"token": "ETH", "amount": 1.5}]}
    sdk.dex.balances.return_value = balances

    # portfolio.*
    for attr, payload in [
        ("value", {"total_value_usd": 1000.0}),
        ("pnl", {"pnl_usd": 50.0}),
        ("tokens", {"tokens": []}),
        ("defi", {"positions": []}),
    ]:
        m = MagicMock()
        m.model_dump.return_value = payload
        setattr(sdk.portfolio, attr, MagicMock(return_value=m))
    sdk.portfolio.history.return_value = [MagicMock(model_dump=MagicMock(return_value={"tx": "0xabc"}))]

    # create_wallet now generates the keypair locally (eth_account) — there is
    # no markets-SDK call on the create path to patch. The route-level client
    # below still backs the keyless read endpoints (balances/portfolio/history).
    monkeypatch.setattr("src.api.routes.wallet.mangrove_markets_client", lambda: sdk)

    from src.app import create_app
    app = create_app()
    with TestClient(app) as c:
        yield c
    ss.reset_scheduler_cache()
    db_mod.reset_connection()
    f.reset_master_key_cache()


def _auth() -> dict:
    return {"X-API-Key": _API_KEY}


def test_create_wallet_happy_path(client):
    r = client.post(
        "/api/v1/agent/wallet/create",
        headers=_auth(),
        json={"chain": "evm", "network": "testnet", "chain_id": 84532, "label": "test"},
    )
    assert r.status_code == 201
    body = r.json()
    # Locally generated EVM address (no fixed SDK address to assert against).
    assert body["address"].startswith("0x") and len(body["address"]) == 42
    assert body["chain"] == "evm"
    # Phase-2 contract: the plaintext key MUST NOT appear in the response.
    assert "seed_phrase" not in body
    assert "private_key" not in body
    assert _TEST_PRIVKEY not in r.text
    # The response carries a vault_token that the CLI consumes via
    # /wallet/reveal-secret/{id}. Plaintext flows out-of-band, never via MCP.
    assert body["vault_token"]
    assert body["reveal_cmd"].startswith("./scripts/reveal-secret.sh ")
    assert body["backup_required"] is True
    assert body["secret_type"] in {"private_key", "mnemonic"}
    assert body["master_key_source"] in {"keyfile", "keychain", "generated_keyfile"}


def test_stash_then_import_flow(client):
    """CLI-style stash + MCP-style import end-to-end via HTTP."""
    # Step 1: CLI-equivalent — stash the plaintext key out-of-band.
    stash = client.post(
        "/api/v1/agent/wallet/stash-secret",
        headers=_auth(),
        json={"secret": _TEST_PRIVKEY},
    )
    assert stash.status_code == 200
    vault_token = stash.json()["vault_token"]
    assert vault_token

    # Step 2: agent-equivalent — import_wallet with the vault_token.
    imp = client.post(
        "/api/v1/agent/wallet/import",
        headers=_auth(),
        json={
            "vault_token": vault_token,
            "chain": "evm",
            "network": "testnet",
            "chain_id": 84532,
        },
    )
    assert imp.status_code == 201
    body = imp.json()
    assert body["address"] == _TEST_ADDRESS
    # Imported wallets auto-confirm backup.
    assert body["backup_required"] is False


def test_reveal_secret_is_single_read(client):
    # Create a wallet to get a valid vault_token.
    r = client.post(
        "/api/v1/agent/wallet/create",
        headers=_auth(),
        json={"chain": "evm", "network": "testnet", "chain_id": 84532},
    )
    sid = r.json()["vault_token"]

    # First reveal: 200 + plaintext (a locally generated 0x + 64-hex key).
    r1 = client.get(f"/api/v1/agent/wallet/reveal-secret/{sid}", headers=_auth())
    assert r1.status_code == 200
    secret = r1.json()["secret"]
    assert secret.startswith("0x") and len(secret) == 66

    # Second reveal: rejected (vault consumed the entry).
    r2 = client.get(f"/api/v1/agent/wallet/reveal-secret/{sid}", headers=_auth())
    # The endpoint raises a SigningError which the exception handler maps to 4xx/5xx.
    assert r2.status_code >= 400


def test_confirm_backup_flips_flag(client):
    created = client.post(
        "/api/v1/agent/wallet/create",
        headers=_auth(),
        json={"chain": "evm", "network": "testnet", "chain_id": 84532},
    )
    addr = created.json()["address"]  # locally generated, capture it
    r = client.post(
        f"/api/v1/agent/wallet/{addr}/confirm-backup",
        headers=_auth(),
        json={},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["address"] == addr
    assert body["backup_confirmed_at"]
    assert "unlocked" in body["message"].lower() or "confirmed" in body["message"].lower()


def test_create_wallet_xrpl_returns_501(client):
    r = client.post(
        "/api/v1/agent/wallet/create",
        headers=_auth(),
        json={"chain": "xrpl", "network": "testnet"},
    )
    assert r.status_code == 501
    assert r.json()["code"] == "CHAIN_NOT_SUPPORTED_IN_V1"


def test_auth_required(client):
    r = client.post(
        "/api/v1/agent/wallet/create",
        json={"chain": "evm", "network": "testnet", "chain_id": 84532},
    )
    assert r.status_code == 401
    assert r.json()["code"] in {"AUTH_MISSING_API_KEY", "AUTH_INVALID_API_KEY"}


def test_auth_rejects_bad_key(client):
    r = client.post(
        "/api/v1/agent/wallet/create",
        headers={"X-API-Key": "wrong-key"},
        json={"chain": "evm", "network": "testnet", "chain_id": 84532},
    )
    assert r.status_code == 401
    assert r.json()["code"] == "AUTH_INVALID_API_KEY"


def test_list_wallets_redacts_secrets(client):
    client.post(
        "/api/v1/agent/wallet/create",
        headers=_auth(),
        json={"chain": "evm", "network": "testnet", "chain_id": 84532, "label": "a"},
    )
    r = client.get("/api/v1/agent/wallet/list", headers=_auth())
    assert r.status_code == 200
    items = r.json()
    assert len(items) == 1
    for forbidden in ("secret", "seed_phrase", "private_key", "encrypted_secret"):
        assert forbidden not in items[0]


def test_balances_passes_through_to_sdk(client):
    r = client.get(
        f"/api/v1/agent/wallet/{_TEST_ADDRESS}/balances",
        params={"chain_id": 84532},
        headers=_auth(),
    )
    assert r.status_code == 200
    assert r.json() == {"balances": [{"token": "ETH", "amount": 1.5}]}


def test_portfolio_aggregates_sdk_calls(client):
    r = client.get(
        f"/api/v1/agent/wallet/{_TEST_ADDRESS}/portfolio",
        headers=_auth(),
    )
    assert r.status_code == 200
    body = r.json()
    assert set(body.keys()) == {"value", "pnl", "tokens", "defi"}
    assert body["value"] == {"total_value_usd": 1000.0}


def test_history_passes_through_to_sdk(client):
    r = client.get(
        f"/api/v1/agent/wallet/{_TEST_ADDRESS}/history",
        params={"limit": 10},
        headers=_auth(),
    )
    assert r.status_code == 200
    assert r.json() == [{"tx": "0xabc"}]


def test_correlation_id_on_error_response(client):
    """Auth-rejected responses carry the spec-shaped error body with correlation_id."""
    r = client.post(
        "/api/v1/agent/wallet/create",
        json={"chain": "evm", "network": "testnet", "chain_id": 84532},
    )
    body = r.json()
    assert body["error"] is True
    assert "correlation_id" in body
    assert r.headers.get("x-correlation-id") == body["correlation_id"]
