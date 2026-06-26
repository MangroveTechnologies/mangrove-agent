"""Integration tests for DEX routes."""
from __future__ import annotations

import os
from unittest.mock import MagicMock

os.environ.setdefault("ENVIRONMENT", "test")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

_API_KEY = "test-key-1"


@pytest.fixture
def client(tmp_path, monkeypatch):
    db_file = tmp_path / "dex.db"
    from src.config import app_config
    from src.services import scheduler_service as ss
    from src.shared.db import sqlite as db_mod

    monkeypatch.setattr(app_config, "DB_PATH", str(db_file))
    db_mod.reset_connection()
    ss.reset_scheduler_cache()

    from src.shared.db.sqlite import init_db
    init_db()

    # 'user-initiated' placeholder seeded by migration 002.

    # Stub the markets SDK (used by routes AND order_executor).
    sdk = MagicMock()

    venue = MagicMock()
    venue.model_dump.return_value = {"id": "uniswap-v2", "name": "Uniswap V2", "chain": "base"}
    sdk.dex.supported_venues.return_value = [venue]

    pair = MagicMock()
    pair.model_dump.return_value = {"from_token": "USDC", "to_token": "ETH"}
    sdk.dex.supported_pairs.return_value = [pair]

    quote = MagicMock()
    quote.model_dump.return_value = {
        "quote_id": "q-1", "input_amount": 100.0, "output_amount": 0.04,
        "exchange_rate": 2500.0,
    }
    quote.quote_id = "q-1"
    quote.output_amount = 0.04
    quote.exchange_rate = 2500.0
    quote.venue_fee = 0.0
    quote.mangrove_fee = 0.0
    quote.price_impact_percent = 0.0
    sdk.dex.get_quote.return_value = quote

    sdk.dex.approve_token.return_value = None  # already approved

    prepare = MagicMock()
    prepare.payload = {"chainId": 84532, "to": "0x" + "a" * 40, "data": "0x"}
    sdk.dex.prepare_swap.return_value = prepare

    bcast = MagicMock()
    bcast.tx_hash = "0xdeadbeef"
    sdk.dex.broadcast.return_value = bcast

    tx_status = MagicMock()
    tx_status.model_dump.return_value = {
        "status": "confirmed", "block_number": 42, "error_message": None,
    }
    tx_status.status = "confirmed"
    tx_status.block_number = 42
    tx_status.error_message = None
    sdk.dex.tx_status.return_value = tx_status

    # token_info is now consulted by dex_service.get_quote to convert the
    # human amount -> base units, so return realistic per-address decimals.
    _DECIMALS = {
        ("0x" + "a" * 40): ("USDC", 6),
        "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913": ("USDC", 6),   # USDC on Base
        "0x4200000000000000000000000000000000000006": ("WETH", 18),  # WETH on Base
    }

    def _token_info(chain_id, address):
        symbol, decimals = _DECIMALS.get(address.lower(), ("TKN", 18))
        ti = MagicMock()
        ti.decimals = decimals
        ti.model_dump.return_value = {
            "address": address, "symbol": symbol, "name": symbol,
            "decimals": decimals, "chain_id": chain_id,
        }
        return ti

    sdk.dex.token_info.side_effect = _token_info

    spot_price = MagicMock()
    spot_price.model_dump.return_value = {
        "chain_id": 8453,
        "prices": {"USDC": 1.0, "ETH": 2500.0},
    }
    sdk.dex.spot_price.return_value = spot_price

    gas_price = MagicMock()
    gas_price.model_dump.return_value = {
        "chain_id": 8453, "slow_gwei": 0.03, "standard_gwei": 0.05, "fast_gwei": 0.08,
    }
    sdk.dex.gas_price.return_value = gas_price

    monkeypatch.setattr("src.api.routes.dex.mangrove_markets_client", lambda: sdk)
    monkeypatch.setattr("src.services.dex_service.mangrove_markets_client", lambda: sdk)
    monkeypatch.setattr("src.services.order_executor.mangrove_markets_client", lambda: sdk)
    monkeypatch.setattr(
        "src.services.order_executor.wallet_sign",
        lambda payload, wallet_address, chain_id=None: "0xSIGNED",
    )
    # Backup gate stub — this test doesn't seed a wallet row, and
    # execute_swap's gate calls require_backup_confirmed(address) which
    # does a DB lookup. Bypass for this test.
    monkeypatch.setattr(
        "src.services.wallet_manager.require_backup_confirmed",
        lambda address: None,
    )

    from src.app import create_app
    app = create_app()
    with TestClient(app) as c:
        c.app_sdk = sdk  # expose the mock so tests can assert/reconfigure calls
        yield c
    ss.reset_scheduler_cache()
    db_mod.reset_connection()


def _auth() -> dict:
    return {"X-API-Key": _API_KEY}


def test_venues(client):
    r = client.get("/api/v1/agent/dex/venues", headers=_auth())
    assert r.status_code == 200
    body = r.json()
    assert len(body) == 1
    assert body[0]["id"] == "uniswap-v2"


def test_pairs(client):
    r = client.get("/api/v1/agent/dex/pairs", params={"venue_id": "uniswap-v2"}, headers=_auth())
    assert r.status_code == 200
    assert r.json()[0]["from_token"] == "USDC"


_WETH_BASE = "0x4200000000000000000000000000000000000006"
_USDC_BASE = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"


def test_quote_converts_human_amount_to_base_units(client):
    """Regression for the INSUFFICIENT_LIQUIDITY blocker.

    A human amount (0.001 WETH) must reach the backend as base units
    (0.001 * 1e18 = 1e15 wei). The pre-fix code forwarded 0.001 verbatim,
    which the backend read as sub-wei dust and rejected with
    INSUFFICIENT_LIQUIDITY for every pair/chain.
    """
    # Backend echoes/returns amounts in base units (wei).
    quote = MagicMock()
    quote.model_dump.return_value = {
        "quote_id": "q-weth", "venue_id": "1inch",
        "input_amount": 1_000_000_000_000_000,   # 0.001 WETH in wei
        "output_amount": 2_500_000,               # 2.5 USDC in base units (6 dp)
        "exchange_rate": 2500.0,
    }
    client.app_sdk.dex.get_quote.return_value = quote
    client.app_sdk.dex.get_quote.side_effect = None

    r = client.post(
        "/api/v1/agent/dex/quote",
        headers=_auth(),
        json={
            "input_token": _WETH_BASE, "output_token": _USDC_BASE,
            "amount": 0.001, "chain_id": 8453,
        },
    )
    assert r.status_code == 200, r.text

    # The SDK was called with the BASE-unit amount, not the human float.
    _, kwargs = client.app_sdk.dex.get_quote.call_args
    assert kwargs["amount"] == 1_000_000_000_000_000

    body = r.json()
    assert body["quote_id"] == "q-weth"
    # Returned amounts are converted back to human units for display.
    assert body["input_amount"] == 0.001
    assert body["output_amount"] == 2.5
    assert body["input_amount_base_units"] == 1_000_000_000_000_000
    assert body["input_token_decimals"] == 18
    assert body["output_token_decimals"] == 6


def test_quote_rejects_dust_amount(client):
    """An amount that rounds to 0 base units gets a clear client error,
    not a confusing upstream INSUFFICIENT_LIQUIDITY."""
    r = client.post(
        "/api/v1/agent/dex/quote",
        headers=_auth(),
        json={
            "input_token": _USDC_BASE, "output_token": _WETH_BASE,
            "amount": 0.0000001, "chain_id": 8453,  # < 1e-6 USDC -> 0 base units
        },
    )
    assert r.status_code == 400
    assert r.json()["code"] == "VALIDATION_ERROR"


def test_swap_requires_confirm(client):
    r = client.post(
        "/api/v1/agent/dex/swap",
        headers=_auth(),
        json={
            "input_token": "USDC", "output_token": "ETH", "amount": 100.0,
            "chain_id": 84532, "wallet_address": "0xabc",
            "slippage_pct": 0.002, "confirm": False,
        },
    )
    assert r.status_code == 400
    assert r.json()["code"] == "CONFIRMATION_REQUIRED"


def test_swap_requires_explicit_slippage(client):
    """slippage_pct has no default — picking a tolerance is a risk
    decision the user must make explicitly for every live swap."""
    r = client.post(
        "/api/v1/agent/dex/swap",
        headers=_auth(),
        json={
            "input_token": "USDC", "output_token": "ETH", "amount": 100.0,
            "chain_id": 84532, "wallet_address": "0xabc", "confirm": True,
        },
    )
    assert r.status_code == 422  # Pydantic rejects missing required field
    body = r.json()
    errors = body.get("detail", [])
    missing_fields = [e["loc"][-1] for e in errors if e.get("type") == "missing"]
    assert "slippage_pct" in missing_fields


def test_swap_rejects_slippage_above_cap(client):
    """slippage_pct cap is 0.0025 (0.25%) — anything higher is refused
    at the API boundary to prevent rekt-on-illiquid-pair execution."""
    r = client.post(
        "/api/v1/agent/dex/swap",
        headers=_auth(),
        json={
            "input_token": "USDC", "output_token": "ETH", "amount": 100.0,
            "chain_id": 84532, "wallet_address": "0xabc",
            "slippage_pct": 0.01, "confirm": True,  # 1%, over the 0.25% cap
        },
    )
    assert r.status_code == 422
    body = r.json()
    errors = body.get("detail", [])
    assert any(
        e.get("loc", [None, None])[-1] == "slippage_pct"
        and e.get("type") in ("less_than_equal", "greater_than")
        for e in errors
    ), f"expected cap rejection on slippage_pct; got {errors}"


def test_swap_accepts_slippage_at_cap(client):
    """Boundary: slippage_pct = 0.0025 (exactly the cap) is allowed."""
    r = client.post(
        "/api/v1/agent/dex/swap",
        headers=_auth(),
        json={
            "input_token": "USDC", "output_token": "ETH", "amount": 100.0,
            "chain_id": 84532, "wallet_address": "0xabc",
            "slippage_pct": 0.0025, "confirm": True,
        },
    )
    assert r.status_code == 200


def test_swap_happy_path(client):
    r = client.post(
        "/api/v1/agent/dex/swap",
        headers=_auth(),
        json={
            "input_token": "USDC", "output_token": "ETH", "amount": 100.0,
            "chain_id": 84532, "wallet_address": "0xabc",
            "slippage_pct": 0.002, "confirm": True,
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["tx_hash"] == "0xdeadbeef"
    assert body["status"] == "confirmed"
    assert body["input_token"] == "USDC"
    assert body["output_token"] == "ETH"
    assert "trade_log_id" in body


def test_auth_required_on_dex_endpoints(client):
    assert client.get("/api/v1/agent/dex/venues").status_code == 401
    assert client.get("/api/v1/agent/dex/pairs?venue_id=x").status_code == 401
    assert client.post("/api/v1/agent/dex/quote",
                       json={"input_token": "USDC", "output_token": "ETH",
                             "amount": 1, "chain_id": 84532}).status_code == 401


def test_tx_status(client):
    r = client.get(
        "/api/v1/agent/dex/tx-status?tx_hash=0xdeadbeef&chain_id=8453",
        headers=_auth(),
    )
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "confirmed"
    assert body["block_number"] == 42


def test_token_info(client):
    r = client.get(
        "/api/v1/agent/dex/token-info?chain_id=8453&address=0x" + "a" * 40,
        headers=_auth(),
    )
    assert r.status_code == 200
    body = r.json()
    assert body["symbol"] == "USDC"
    assert body["decimals"] == 6


def test_spot_price(client):
    r = client.get(
        "/api/v1/agent/dex/spot-price?chain_id=8453&tokens=USDC,ETH",
        headers=_auth(),
    )
    assert r.status_code == 200
    body = r.json()
    assert body["prices"]["ETH"] == 2500.0


def test_gas_price(client):
    r = client.get(
        "/api/v1/agent/dex/gas-price?chain_id=8453",
        headers=_auth(),
    )
    assert r.status_code == 200
    body = r.json()
    assert body["standard_gwei"] == 0.05


def test_auth_required_on_new_dex_endpoints(client):
    assert client.get("/api/v1/agent/dex/tx-status?tx_hash=x&chain_id=1").status_code == 401
    assert client.get("/api/v1/agent/dex/token-info?chain_id=1&address=0x0").status_code == 401
    assert client.get("/api/v1/agent/dex/spot-price?chain_id=1&tokens=X").status_code == 401
    assert client.get("/api/v1/agent/dex/gas-price?chain_id=1").status_code == 401
