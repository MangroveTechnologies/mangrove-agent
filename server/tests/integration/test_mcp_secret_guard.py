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
    from eth_account.hdaccount.mnemonic import Mnemonic
    from eth_account.types import Language

    phrase = Mnemonic(Language.ENGLISH).generate(12)  # generated for the test
    while len(set(phrase.split())) < 12:  # distinct words: independent of the repeated-word limits
        phrase = Mnemonic(Language.ENGLISH).generate(12)
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


BARE_KEY = "1f" * 32  # MetaMask "Show private key" format: no 0x prefix


def _upstream_ok():
    return AsyncMock(return_value=CallToolResult(content=[TextContent(type="text", text="{}")]))


@pytest.mark.asyncio
async def test_bare_hex_key_as_vault_token_is_refused_before_the_tool_runs(mcp_server, monkeypatch):
    service = Mock()
    monkeypatch.setattr("src.services.wallet_manager.import_wallet", service)
    result = await mcp_server.call_tool("import_wallet", {"vault_token": BARE_KEY, "api_key": "test-key-1"})
    assert _body(result)["code"] == "SECRET_IN_ARGUMENTS"
    assert BARE_KEY not in json.dumps(result.model_dump())
    service.assert_not_called()


@pytest.mark.parametrize("arguments", [
    {"query": f"import my wallet {FAKE_KEY}"},
    {"query": f"import my wallet {BARE_KEY}"},
    {"arguments": {"privateKey": FAKE_KEY}},
    {"secret_key": BARE_KEY},
])
@pytest.mark.asyncio
async def test_key_in_free_text_or_unlisted_field_is_not_forwarded(mcp_server, monkeypatch, arguments):
    upstream = _upstream_ok()
    monkeypatch.setattr("src.mcp.mangrove_proxy.call_tool", upstream)
    result = await mcp_server.call_tool("search_signals", arguments)
    assert _body(result)["code"] == "SECRET_IN_ARGUMENTS"
    upstream.assert_not_called()


@pytest.mark.asyncio
async def test_strategy_prose_is_forwarded(mcp_server, monkeypatch):
    upstream = _upstream_ok()
    monkeypatch.setattr("src.mcp.mangrove_proxy.call_tool", upstream)
    arguments = {"description": "price above average, volume above average, trend above zero, "
                                "exit when price below average"}
    result = await mcp_server.call_tool("match_signals", arguments)
    upstream.assert_awaited_once()
    assert result is upstream.return_value


@pytest.mark.parametrize("body", [{"privateKey": FAKE_KEY}, {"private_key": BARE_KEY}])
@pytest.mark.asyncio
async def test_camelcase_or_bare_hex_result_is_withheld(mcp_server, monkeypatch, body):
    leaked = CallToolResult(content=[TextContent(type="text", text=json.dumps(body))])
    monkeypatch.setattr("src.mcp.mangrove_proxy.call_tool", AsyncMock(return_value=leaked))
    result = await mcp_server.call_tool("some_upstream_tool", {})
    assert _body(result)["code"] == "SECRET_IN_RESULT"
    assert "1f1f1f" not in json.dumps(result.model_dump())


@pytest.mark.asyncio
async def test_swap_result_with_transaction_hashes_passes(mcp_server, monkeypatch):
    body = {"tx_hash": FAKE_TX, "approval_tx_hash": "0x" + "cd" * 32,
            "explorer_url": f"https://basescan.org/tx/{FAKE_TX}", "status": "confirmed"}
    ok = CallToolResult(content=[TextContent(type="text", text=json.dumps(body))])
    monkeypatch.setattr("src.mcp.mangrove_proxy.call_tool", AsyncMock(return_value=ok))
    result = await mcp_server.call_tool("some_upstream_tool", {"definition_hash": "ef" * 32})
    assert result is ok


async def _call_text(mcp_server, name, arguments) -> tuple[str, object]:
    """The text a client receives: the result, or the message of the raised error."""
    try:
        result = await mcp_server.call_tool(name, arguments)
    except Exception as exc:  # noqa: BLE001 - the transport sends str(exc) to the client
        return str(exc), exc
    return json.dumps(result.model_dump()), result


@pytest.mark.asyncio
async def test_error_message_holding_a_key_is_withheld(mcp_server, monkeypatch):
    upstream = AsyncMock(side_effect=RuntimeError(f"wallet decrypt failed for key {FAKE_KEY}"))
    monkeypatch.setattr("src.mcp.mangrove_proxy.call_tool", upstream)
    text, outcome = await _call_text(mcp_server, "some_upstream_tool", {})
    assert FAKE_KEY not in text
    assert _body(outcome)["code"] == "SECRET_IN_ERROR"


@pytest.mark.asyncio
async def test_validation_error_does_not_echo_argument_values(mcp_server):
    value = f"tx {BARE_KEY}"  # passes the argument scan as a labelled hash
    text, outcome = await _call_text(mcp_server, "x402_spend_status", {"limit": value})
    assert isinstance(outcome, Exception)
    assert "1f1f1f" not in text
    assert "limit" in text


@pytest.mark.asyncio
async def test_error_message_repeating_an_argument_is_redacted(mcp_server, monkeypatch):
    digest = "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08"
    upstream = AsyncMock(side_effect=ValueError(f"unknown dataset {digest}"))
    monkeypatch.setattr("src.mcp.mangrove_proxy.call_tool", upstream)
    text, outcome = await _call_text(mcp_server, "some_upstream_tool", {"dataset": digest})
    assert isinstance(outcome, Exception)
    assert digest not in text
    assert text == "unknown dataset [argument value]"


@pytest.mark.asyncio
async def test_ordinary_errors_are_raised_unchanged(mcp_server, monkeypatch):
    error = ValueError("upstream timed out")
    monkeypatch.setattr("src.mcp.mangrove_proxy.call_tool", AsyncMock(side_effect=error))
    with pytest.raises(ValueError) as raised:
        await mcp_server.call_tool("some_upstream_tool", {"symbol": "ETH"})
    assert raised.value is error
