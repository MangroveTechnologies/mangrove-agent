"""Integration tests for market / on-chain / signals / kb pass-through routes.

We only assert wiring: each route calls the expected SDK method and
returns the SDK's response. Behavior of the SDK itself is its own
responsibility.
"""
from __future__ import annotations

import os
from unittest.mock import MagicMock

os.environ.setdefault("ENVIRONMENT", "test")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

_API_KEY = "test-key-1"


@pytest.fixture
def client(tmp_path, monkeypatch):
    db_file = tmp_path / "pt.db"
    from src.config import app_config
    from src.services import scheduler_service as ss
    from src.shared.db import sqlite as db_mod

    monkeypatch.setattr(app_config, "DB_PATH", str(db_file))
    db_mod.reset_connection()
    ss.reset_scheduler_cache()

    sdk = MagicMock()

    # crypto_assets.*
    def _resp(payload):
        m = MagicMock()
        m.model_dump.return_value = payload
        return m

    sdk.crypto_assets.get_ohlcv.return_value = _resp({"candles": [{"t": 1, "o": 1, "h": 2, "l": 1, "c": 2, "v": 100}]})
    sdk.crypto_assets.get_market_data.return_value = _resp({"data": {"current_price": 2500.0}})
    sdk.crypto_assets.get_trending.return_value = _resp({"trending": []})
    sdk.crypto_assets.get_global_market.return_value = _resp({"btc_dominance": 0.52})

    # on_chain.*
    sdk.on_chain.get_smart_money_sentiment.return_value = _resp({"sentiment": "bullish"})
    sdk.on_chain.get_whale_activity.return_value = _resp({"whale": "calm"})
    sdk.on_chain.get_token_holders.return_value = _resp({"holders": []})

    # signals.*
    sig = MagicMock()
    sig.model_dump.return_value = {"name": "rsi_oversold", "category": "overbought_oversold"}
    page = MagicMock()
    page.items = [sig]
    page.total = 1
    sdk.signals.list.return_value = page
    sdk.signals.search.return_value = page
    sdk.signals.get.return_value = sig

    # kb.*
    sdk.kb.search.query.return_value = _resp({"hits": []})
    sdk.kb.glossary.get.return_value = _resp({"term": "rsi", "definition": "relative strength index"})

    for path in (
        "src.api.routes.market.mangrove_ai_client",
        "src.api.routes.on_chain.mangrove_ai_client",
        "src.api.routes.signals.mangrove_ai_client",
        "src.api.routes.kb.mangrove_ai_client",
    ):
        monkeypatch.setattr(path, lambda s=sdk: s)

    from src.app import create_app
    app = create_app()
    with TestClient(app) as c:
        yield c
    ss.reset_scheduler_cache()
    db_mod.reset_connection()


def _auth() -> dict:
    return {"X-API-Key": _API_KEY}


# -- market ------------------------------------------------------------------


def test_ohlcv(client):
    r = client.get("/api/v1/agent/market/ohlcv",
                   params={"symbol": "BTC", "timeframe": "1h", "lookback_days": 7},
                   headers=_auth())
    assert r.status_code == 200
    assert "candles" in r.json()


def test_market_data(client):
    r = client.get("/api/v1/agent/market/data", params={"symbol": "ETH"}, headers=_auth())
    assert r.status_code == 200
    assert r.json()["data"]["current_price"] == 2500.0


def test_trending(client):
    r = client.get("/api/v1/agent/market/trending", headers=_auth())
    assert r.status_code == 200
    assert "trending" in r.json()


def test_global_market(client):
    r = client.get("/api/v1/agent/market/global", headers=_auth())
    assert r.status_code == 200
    assert r.json()["btc_dominance"] == 0.52


# -- on-chain ---------------------------------------------------------------


def test_smart_money(client):
    r = client.get("/api/v1/agent/on-chain/smart-money",
                   params={"symbol": "ETH", "chain": "ethereum"}, headers=_auth())
    assert r.status_code == 200
    assert r.json()["sentiment"] == "bullish"


def test_whale_activity(client):
    r = client.get("/api/v1/agent/on-chain/whale-activity",
                   params={"symbol": "BTC"}, headers=_auth())
    assert r.status_code == 200


def test_token_holders(client):
    r = client.get("/api/v1/agent/on-chain/token-holders/ETH", headers=_auth())
    assert r.status_code == 200


# -- signals ----------------------------------------------------------------


def test_list_signals(client):
    r = client.get("/api/v1/agent/signals", headers=_auth())
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 1
    assert body["items"][0]["name"] == "rsi_oversold"


def test_list_signals_filters_by_category(client):
    r = client.get("/api/v1/agent/signals", params={"category": "breakout"}, headers=_auth())
    assert r.status_code == 200
    # Only item is category overbought_oversold → filtered out
    assert r.json()["items"] == []


def test_get_signal(client):
    r = client.get("/api/v1/agent/signals/rsi_oversold", headers=_auth())
    assert r.status_code == 200
    assert r.json()["name"] == "rsi_oversold"


# -- kb ---------------------------------------------------------------------


def test_kb_search(client):
    r = client.get("/api/v1/agent/kb/search", params={"q": "RSI"}, headers=_auth())
    assert r.status_code == 200


def test_kb_glossary(client):
    r = client.get("/api/v1/agent/kb/glossary/rsi", headers=_auth())
    assert r.status_code == 200
    assert r.json()["term"] == "rsi"


# -- auth enforcement -------------------------------------------------------


def test_auth_required_on_all_passthrough_routes(client):
    endpoints = [
        "/api/v1/agent/market/ohlcv?symbol=BTC",
        "/api/v1/agent/market/data?symbol=BTC",
        "/api/v1/agent/market/trending",
        "/api/v1/agent/market/global",
        "/api/v1/agent/on-chain/smart-money?symbol=ETH",
        "/api/v1/agent/on-chain/whale-activity?symbol=ETH",
        "/api/v1/agent/on-chain/token-holders/ETH",
        "/api/v1/agent/signals",
        "/api/v1/agent/signals/rsi_oversold",
        "/api/v1/agent/kb/search?q=x",
        "/api/v1/agent/kb/glossary/rsi",
    ]
    for ep in endpoints:
        assert client.get(ep).status_code == 401, f"{ep} should require auth"
