"""The MCP call boundary applies the secret guard for every client.

Calls go through PricedFastMCP.call_tool, the same entry point the HTTP
transport uses for Claude Code, scripts/local_mcp_bridge.py and remote clients.
"""
from __future__ import annotations

import json
import os
from unittest.mock import AsyncMock, Mock

os.environ.setdefault("ENVIRONMENT", "test")

import httpx  # noqa: E402
import pytest  # noqa: E402
from mcp.types import CallToolResult, TextContent  # noqa: E402

FAKE_KEY = "0x" + "1f" * 32
FAKE_TX = "0x" + "ab" * 32


@pytest.fixture
def mcp_server(tmp_path, monkeypatch):
    monkeypatch.setattr("src.mcp.mangrove_proxy.catalog", AsyncMock(return_value=[]))
    monkeypatch.setattr("src.mcp.marketplace_proxy.list_tools", AsyncMock(return_value=[]))
    monkeypatch.setattr("src.services.tool_pricing._fetch_prices",
                        Mock(side_effect=httpx.ConnectError("offline pricing fixture")))
    from src.config import app_config
    from src.shared.db import sqlite as db_mod

    monkeypatch.setattr(app_config, "DB_PATH", str(tmp_path / "guard.db"))
    db_mod.reset_connection()
    from src.shared.db.sqlite import init_db
    init_db()
    from src.mcp.server import create_mcp_server
    yield create_mcp_server()
    db_mod.reset_connection()


def _body(result) -> dict:
    assert isinstance(result, CallToolResult)
    return result.structuredContent


@pytest.mark.asyncio
async def test_key_pasted_as_vault_token_is_refused_before_the_tool_runs(mcp_server, monkeypatch):
    service = Mock()
    monkeypatch.setattr("src.services.wallet_manager.import_wallet", service)
    result = await mcp_server.call_tool("import_wallet", {"vault_token": FAKE_KEY})
    assert result.isError
    assert _body(result)["code"] == "SECRET_IN_ARGUMENTS"
    assert FAKE_KEY not in json.dumps(result.model_dump())
    service.assert_not_called()


@pytest.mark.asyncio
async def test_secret_is_never_forwarded_upstream(mcp_server, monkeypatch):
    upstream = AsyncMock()
    monkeypatch.setattr("src.mcp.mangrove_proxy.call_tool", upstream)
    phrase = " ".join(sorted(__import__("src.shared.secret_guard", fromlist=["x"]).bip39_words())[:12])
    result = await mcp_server.call_tool("kb_search", {"query": phrase})
    assert _body(result)["code"] == "SECRET_IN_ARGUMENTS"
    upstream.assert_not_called()


@pytest.mark.asyncio
async def test_result_with_key_field_is_withheld(mcp_server, monkeypatch):
    leaked = CallToolResult(content=[TextContent(type="text", text=json.dumps({"private_key": FAKE_KEY}))])
    monkeypatch.setattr("src.mcp.mangrove_proxy.call_tool", AsyncMock(return_value=leaked))
    result = await mcp_server.call_tool("some_upstream_tool", {})
    assert result.isError
    assert _body(result)["code"] == "SECRET_IN_RESULT"
    assert FAKE_KEY not in json.dumps(result.model_dump())


@pytest.mark.asyncio
async def test_transaction_hashes_pass_through(mcp_server, monkeypatch):
    ok = CallToolResult(content=[TextContent(type="text", text=json.dumps({"tx_hash": FAKE_TX}))])
    upstream = AsyncMock(return_value=ok)
    monkeypatch.setattr("src.mcp.mangrove_proxy.call_tool", upstream)
    result = await mcp_server.call_tool("explain_transaction", {"tx_hash": FAKE_TX})
    assert result is ok
    upstream.assert_awaited_once()


@pytest.mark.asyncio
async def test_local_tools_still_work(mcp_server):
    result = await mcp_server.call_tool("status", {})
    assert "SECRET_IN" not in json.dumps(result, default=str)


def test_http_transport_used_by_every_client_applies_the_guard(monkeypatch):
    """The /mcp/ endpoint (Claude Code, scripts/local_mcp_bridge.py, remote clients)."""
    from fastapi.testclient import TestClient

    from src.app import create_app
    from src.mcp import server

    server.reset_mcp_server()
    monkeypatch.setattr("src.services.scheduler_service.start", lambda: None)
    monkeypatch.setattr("src.services.scheduler_service.shutdown", lambda: None)
    monkeypatch.setattr("src.shared.x402.server._ensure_initialized", Mock(side_effect=ConnectionError("offline")))
    service = Mock()
    monkeypatch.setattr("src.services.wallet_manager.import_wallet", service)
    try:
        with TestClient(create_app(), base_url="http://localhost:9080") as client:
            response = client.post("/mcp/", headers={
                "X-API-Key": "test-key-1", "Accept": "application/json, text/event-stream",
                "MCP-Protocol-Version": "2025-03-26",
            }, json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                     "params": {"name": "import_wallet", "arguments": {"vault_token": FAKE_KEY}}})
        assert response.status_code == 200
        result = response.json()["result"]
        assert result["isError"] is True
        assert result["structuredContent"]["code"] == "SECRET_IN_ARGUMENTS"
        assert FAKE_KEY not in response.text
        service.assert_not_called()
    finally:
        server.reset_mcp_server()
