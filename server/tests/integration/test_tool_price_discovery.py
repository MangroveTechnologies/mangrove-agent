"""Verify prices reach actual REST and MCP consumers without payment calls."""
import json

import pytest
from fastapi.testclient import TestClient

from src.services import tool_pricing


@pytest.fixture
def client(tmp_path, monkeypatch):
    from src.app import create_app
    from src.config import app_config
    from src.services import scheduler_service
    from src.shared.db import sqlite

    monkeypatch.setattr(app_config, "DB_PATH", str(tmp_path / "pricing.db"))
    monkeypatch.setattr(app_config, "MANGROVE_API_KEY", None)
    monkeypatch.setattr(tool_pricing, "_cache", tool_pricing.PriceCache())
    calls = []

    def fetch(endpoint):
        calls.append(endpoint)
        return {"rest:signals_get": "0.007", "rest:signals_list": "0.002"}, "eip155:84532", True

    def forbidden(*args, **kwargs):
        pytest.fail("Free discovery accessed a payment-capable SDK client")

    monkeypatch.setattr(tool_pricing, "_fetch_prices", fetch)
    monkeypatch.setattr("src.shared.clients.mangrove.mangrove_ai_client", forbidden)
    sqlite.reset_connection()
    scheduler_service.reset_scheduler_cache()
    with TestClient(create_app(), base_url="http://127.0.0.1:9080") as session:
        yield session, calls
    scheduler_service.reset_scheduler_cache()
    sqlite.reset_connection()


def rpc(client, method, params=None):
    response = client.post("/mcp/", json={
        "jsonrpc": "2.0", "id": 1, "method": method, "params": params or {},
    }, headers={"Accept": "application/json, text/event-stream"})
    assert response.status_code == 200
    body = response.json()
    assert "error" not in body
    return body["result"]


def test_rest_tool_and_mcp_protocol_share_prices_and_cache(client):
    session, calls = client
    rest = session.get("/api/v1/agent/tools").json()["tools"]
    tool_result = rpc(session, "tools/call", {"name": "list_tools", "arguments": {}})
    tool_rows = json.loads(tool_result["content"][0]["text"])["tools"]
    assert rest == tool_rows
    protocol = rpc(session, "tools/list")["tools"]
    get_signal = next(t for t in protocol if t["name"] == "get_signal")
    rest_signal = next(t for t in rest if t["name"] == "get_signal")
    assert get_signal["_meta"]["mangrove/pricing"] == rest_signal["pricing"]
    assert "0.007 USDC" in get_signal["description"]
    assert rest_signal["price"] == "$0.007 USDC per upstream request"
    assert len(calls) == 1
    again = rpc(session, "tools/list")["tools"]
    assert protocol == again  # hints do not accumulate on cached tool objects
    local = next(t for t in protocol if t["name"] == "build_strategy_from_reference")
    assert "mangrove/pricing" not in (local.get("_meta") or {})


def test_status_in_payment_mode_never_spends(client):
    from src.api.routes.discovery import reset_catalog_cache

    reset_catalog_cache()
    session, calls = client
    result = session.get("/api/v1/agent/status")
    assert result.status_code == 200
    assert result.json()["catalog"]["error"] == "catalog_counts_unavailable_in_payment_mode"
    assert calls == []
    reset_catalog_cache()


def test_price_failure_keeps_both_catalogs_available(client, monkeypatch):
    def fail(endpoint):
        raise ValueError("secret upstream payload")
    monkeypatch.setattr(tool_pricing, "_fetch_prices", fail)
    session, _ = client
    response = session.get("/api/v1/agent/tools")
    assert response.status_code == 200
    assert "secret upstream" not in response.text
    get_signal = next(t for t in rpc(session, "tools/list")["tools"] if t["name"] == "get_signal")
    assert get_signal["_meta"]["mangrove/pricing"]["status"] == "unavailable"
    assert "do not assume" in get_signal["description"]
