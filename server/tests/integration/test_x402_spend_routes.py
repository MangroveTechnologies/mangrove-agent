"""Integration tests for the x402 spend-cap routes.

Verification case 6's last clause -- "human reset works" -- needs a surface a
human can actually reach. These cover it, plus the /status block that answers
"why did the agent stop paying for things?" without anyone having to know the
route exists.
"""
from __future__ import annotations

import os

os.environ.setdefault("ENVIRONMENT", "test")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

_API_KEY = "test-key-1"
_AUTH = {"X-API-Key": _API_KEY}


@pytest.fixture
def client(tmp_path, monkeypatch):
    db_file = tmp_path / "spend_routes.db"
    from src.api.routes import discovery
    from src.config import app_config
    from src.services import scheduler_service as ss
    from src.shared.db import sqlite as db_mod

    monkeypatch.setattr(app_config, "DB_PATH", str(db_file))
    monkeypatch.setattr(app_config, "X402_SPEND_CAP_USD", 5, raising=False)
    db_mod.reset_connection()
    ss.reset_scheduler_cache()
    discovery.reset_catalog_cache()

    from src.app import create_app

    with TestClient(create_app()) as c:
        yield c
    ss.reset_scheduler_cache()
    db_mod.reset_connection()
    discovery.reset_catalog_cache()


def _reserve(amount_micro_usd: int) -> str:
    from src.services import spend_service

    return spend_service.reserve(value=amount_micro_usd, wallet_address="0xPayer")


def test_spend_routes_require_auth(client):
    assert client.get("/api/v1/agent/x402/spend").status_code == 401
    assert client.post("/api/v1/agent/x402/spend/reset").status_code == 401


def test_spend_status_reports_a_fresh_budget(client):
    body = client.get("/api/v1/agent/x402/spend", headers=_AUTH).json()
    assert body["exhausted"] is False
    assert body["cap_usd"] == 5.0
    assert body["spent_usd"] == 0.0
    assert body["remaining_usd"] == 5.0
    assert body["period_id"] == 1


def test_ledger_lists_payments_newest_first(client):
    from src.services import spend_service
    spend_service.settle(_reserve(10_000), transaction="0x" + "ab" * 32)
    _reserve(20_000)

    body = client.get("/api/v1/agent/x402/spend/payments", headers=_AUTH).json()
    assert body["count"] == 2
    assert [p["amount_usd"] for p in body["payments"]] == [0.02, 0.01]


def test_breach_is_visible_then_cleared_by_the_reset_endpoint(client):
    """The full human loop: it trips, they see why, they clear it."""
    _reserve(5_000_000)

    tripped = client.get("/api/v1/agent/x402/spend", headers=_AUTH).json()
    assert tripped["exhausted"] is True
    assert tripped["remaining_usd"] == 0.0
    assert "5.0" in tripped["exhausted_reason"]

    after = client.post("/api/v1/agent/x402/spend/reset", headers=_AUTH).json()
    assert after["exhausted"] is False
    assert after["spent_usd"] == 0.0
    assert after["period_id"] == 2

    # History is kept, stamped with the period it belonged to.
    ledger = client.get("/api/v1/agent/x402/spend/payments", headers=_AUTH).json()
    assert ledger["count"] == 1 and ledger["payments"][0]["period_id"] == 1


def test_status_surfaces_the_budget_beside_portfolio_risk(client):
    """/status is free and unauthenticated -- it is where a user looks first
    when the agent has quietly stopped doing something."""
    _reserve(2_500_000)

    body = client.get("/api/v1/agent/status").json()
    assert body["x402_spend"]["spent_usd"] == 2.5
    assert body["x402_spend"]["remaining_usd"] == 2.5
    assert body["x402_spend"]["exhausted"] is False
    # Both halves of "why did the agent stop?" in one place.
    assert "portfolio_risk" in body


def test_reset_accepts_an_authorized_budget(client):
    _reserve(5_000_000)
    after = client.post("/api/v1/agent/x402/spend/reset", headers=_AUTH,
                        json={"cap_usd": 40}).json()
    assert after["cap_usd"] == 40.0
    assert after["cap_source"] == "authorized"
    assert after["exhausted"] is False


def test_reset_rejects_a_negative_budget_at_the_boundary(client):
    r = client.post("/api/v1/agent/x402/spend/reset", headers=_AUTH,
                    json={"cap_usd": -5})
    assert r.status_code == 422


@pytest.fixture
def payer_wallets(client, monkeypatch):
    from src.config import app_config
    from src.shared.db.sqlite import get_connection
    addresses = ['0x' + digit * 40 for digit in ['1', '2', '3']]
    conn = get_connection()
    for i, address in enumerate(addresses):
        conn.execute('''INSERT INTO wallets
            (id,address,chain,network,chain_id,encrypted_secret,encryption_method,label,created_at,backup_confirmed_at)
            VALUES (?,?,?,?,?,?,?,?,?,?)''',
            (str(i), address, 'evm', 'mainnet', 8453, b'NEVER-READ-THIS', 'fernet-v1', 'x402 payments',
             f'2026-10-30T23:3{i}:00+00:00', '2026-10-30T23:40:00+00:00' if i != 2 else None))
    conn.commit()
    monkeypatch.setattr(app_config, 'X402_PAYER_WALLET', addresses[1])
    monkeypatch.setattr(app_config, 'X402_NETWORK', 'eip155:8453')
    monkeypatch.setattr(app_config, 'MANGROVE_ACCESS_MODE', 'x402')
    return addresses


def test_status_identifies_configured_payer_not_newest_wallet(client, payer_wallets):
    from src.services.x402_payer import resolve_payer_wallet
    body = client.get('/api/v1/agent/x402/spend', headers=_AUTH).json()
    assert body['payer']['address'] == resolve_payer_wallet() == payer_wallets[1]
    assert body['payer']['selection_source'] == 'configuration'
    assert body['payer']['selection_status'] == 'configured'
    assert body['payer']['backup_confirmed'] is True
    assert body['payer']['network_name'] == 'Base Mainnet'
    assert body['payer']['balance_checked'] is False
    assert body['budget_scope'] == 'agent'
    wallets = client.get('/api/v1/agent/wallet/list', headers=_AUTH).json()
    assert [w['address'] for w in wallets if w['is_configured_x402_payer']] == [payer_wallets[1]]
    assert 'NEVER-READ-THIS' not in str(body) + str(wallets)


@pytest.mark.parametrize('configured,expected', [('', 'not_configured'), ('invalid-secret-marker', 'invalid_configuration'),
                                               ('0x' + '4'*40, 'wallet_not_found')])
def test_payer_status_never_falls_back_to_a_stored_wallet(client, payer_wallets, monkeypatch, configured, expected):
    from src.config import app_config
    monkeypatch.setattr(app_config, 'X402_PAYER_WALLET', configured)
    response = client.get('/api/v1/agent/x402/spend', headers=_AUTH)
    payer = response.json()['payer']
    assert payer['selection_status'] == expected
    assert payer['address'] == (configured if expected == 'wallet_not_found' else None)
    assert payer['wallet_stored'] is False
    assert 'invalid-secret-marker' not in response.text
    wallets = client.get('/api/v1/agent/wallet/list', headers=_AUTH).json()
    assert not any(w['is_configured_x402_payer'] for w in wallets)


def test_payer_change_is_reflected_without_cached_wallet_order(client, payer_wallets, monkeypatch):
    from src.config import app_config
    monkeypatch.setattr(app_config, 'X402_PAYER_WALLET', payer_wallets[2].upper())
    payer = client.get('/api/v1/agent/x402/spend', headers=_AUTH).json()['payer']
    assert payer['address'] == payer_wallets[2]
    assert payer['backup_confirmed'] is False
    assert payer['selection_status'] == 'configured'
    wallets = client.get('/api/v1/agent/wallet/list', headers=_AUTH).json()
    assert [w['address'] for w in wallets if w['is_configured_x402_payer']] == [payer_wallets[2]]


def test_payer_is_not_added_to_unauthenticated_status(client, payer_wallets):
    assert client.get('/api/v1/agent/x402/spend').status_code == 401
    response = client.get('/api/v1/agent/status')
    assert 'payer' not in response.json()['x402_spend']
    assert payer_wallets[1] not in response.text


@pytest.mark.parametrize('mode', ['api-key', 'oauth'])
def test_retained_payer_reports_current_access_mode(client, payer_wallets, monkeypatch, mode):
    from src.config import app_config
    monkeypatch.setattr(app_config, 'MANGROVE_ACCESS_MODE', mode)
    payer = client.get('/api/v1/agent/x402/spend', headers=_AUTH).json()['payer']
    assert payer['address'] == payer_wallets[1]
    assert payer['access_mode'] == mode
    assert payer['scope'] == 'default_x402_payer'


def test_mcp_spend_status_includes_the_same_authoritative_payer(client, payer_wallets):
    import asyncio
    import json
    from mcp.server.fastmcp import FastMCP
    from src.mcp.tools import _register_x402_spend
    server = FastMCP('payer-status-test')
    _register_x402_spend(server)
    tool = server._tool_manager.get_tool('x402_spend_status')
    body = json.loads(asyncio.run(tool.fn(api_key=_API_KEY)))
    assert body['payer']['address'] == payer_wallets[1]
    assert body['payer'] == client.get('/api/v1/agent/x402/spend', headers=_AUTH).json()['payer']
    assert body['budget']['cap_usd'] == 5
    assert body['budget_scope'] == 'agent'
    assert body['payments'] == []
    denied = asyncio.run(tool.fn(api_key='wrong-key'))
    assert payer_wallets[1] not in denied
