"""Unit tests for the keyless CEX OAuth service.

Verifies the agent proxies to the markets server with the Mangrove key as
Bearer (never a venue key), forwards the chosen mode, and surfaces proxy
errors. No Kraken credential is ever involved.
"""
from __future__ import annotations

import os

os.environ.setdefault("ENVIRONMENT", "test")

import httpx
import pytest

from src.config import app_config
from src.services import cex_oauth_service


def _transport(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_connect_start_uses_bearer_and_forwards_mode(monkeypatch):
    monkeypatch.setattr(app_config, "MANGROVE_API_KEY", "mgv_test_key", raising=False)
    monkeypatch.setattr(app_config, "MANGROVEMARKETS_BASE_URL", "http://markets.local", raising=False)
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json
        seen["auth"] = request.headers.get("Authorization")
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"authorize_url": "https://id.kraken.com/x", "state": "s"})

    with _transport(handler) as http:
        out = cex_oauth_service.connect_start(mode="execute", http=http)

    assert seen["auth"] == "Bearer mgv_test_key"  # Mangrove key, NOT a Kraken key
    assert seen["url"].endswith("/api/v1/exchanges/kraken/connect")
    assert seen["body"]["mode"] == "execute"
    assert out["authorize_url"].startswith("https://id.kraken.com")


def test_place_order_forwards_fields(monkeypatch):
    monkeypatch.setattr(app_config, "MANGROVE_API_KEY", "mgv_test_key", raising=False)
    monkeypatch.setattr(app_config, "MANGROVEMARKETS_BASE_URL", "http://markets.local", raising=False)
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"pair": "XRPUSDC", "validate_only": True, "tx_ids": []})

    with _transport(handler) as http:
        out = cex_oauth_service.place_order(
            base="XRP", quote="USDC", side="buy", volume="5", validate_only=True, http=http,
        )
    assert seen["body"] == {
        "base": "XRP", "quote": "USDC", "side": "buy",
        "order_type": "market", "volume": "5", "validate_only": True,
    }
    assert out["pair"] == "XRPUSDC"


def test_proxy_error_raises(monkeypatch):
    monkeypatch.setattr(app_config, "MANGROVE_API_KEY", "mgv_test_key", raising=False)
    monkeypatch.setattr(app_config, "MANGROVEMARKETS_BASE_URL", "http://markets.local", raising=False)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"detail": "connection is view-only"})

    with _transport(handler) as http:
        with pytest.raises(RuntimeError, match="view-only"):
            cex_oauth_service.place_order(base="XRP", quote="USDC", side="buy", volume="5", http=http)
