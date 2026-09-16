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
    _reserve(10_000)
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
