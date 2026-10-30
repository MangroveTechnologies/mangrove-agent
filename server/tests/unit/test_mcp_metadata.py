"""Discover real local tools without starting the agent or executing an action."""
from __future__ import annotations

import json
import socket
import sqlite3
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from mcp.types import Tool, ToolAnnotations
from starlette.testclient import TestClient

# readOnlyHint, destructiveHint, idempotentHint, openWorldHint. Mutations use
# conservative idempotency hints even where one receiver supports recovery.
EXPECTED_HINTS = {
    "status": (True, False, True, True),
    "set_agent_name": (False, True, False, False),
    "list_tools": (True, False, True, True),
    "create_wallet": (False, False, False, False),
    "import_wallet": (False, True, False, False),
    "list_wallets": (True, False, True, False),
    "cex_status": (True, False, True, False),
    "cex_connect_kraken": (False, True, False, False),
    "cex_balances": (True, False, True, True),
    "cex_validate_order": (True, False, True, True),
    "cex_sync_fills": (False, False, False, True),
    "execute_swap": (False, True, False, True),
    "agent_robinhood_status": (True, False, True, True),
    "agent_configure_execution_venue": (False, True, False, True),
    "agent_configure_strategy_market": (False, True, False, False),
    "agent_list_venue_orders": (True, False, True, False),
    "agent_reconcile_venue_orders": (False, True, False, True),
    "agent_cancel_venue_order": (False, True, False, True),
    "marketplace_prepare": (False, False, False, True),
    "marketplace_submit": (False, True, False, True),
    "agent_start_paper_schedule": (False, True, False, True),
    "agent_list_paper_schedules": (True, False, True, False),
    "agent_pause_paper_schedule": (False, True, False, True),
    "agent_get_paper_schedule": (True, False, True, True),
    "agent_reconcile_paper_schedule": (False, True, False, True),
    "x402_spend_status": (True, False, True, False),
    "x402_spend_reset": (False, True, False, False),
}
HINT_KEYS = ("readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint")


@pytest.fixture
def metadata_server(monkeypatch):
    from src.mcp import mangrove_proxy, marketplace_proxy, registry, tools
    from src.mcp.server import PricedFastMCP
    from src.services import mangrove_oauth, tool_pricing

    def forbidden(*args, **kwargs):
        raise AssertionError("Metadata tests prohibit network, DB and payment initialization")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr(sqlite3, "connect", forbidden)
    monkeypatch.setattr(registry, "_tools", [])
    monkeypatch.setattr(mangrove_oauth, "enabled", lambda: False)
    monkeypatch.setattr("src.shared.x402.server._ensure_initialized", forbidden)
    # Avoid an unrelated startup thread; exercise degraded registration directly.
    monkeypatch.setattr(tools, "_run_bounded", lambda fn, timeout_s: fn())

    async def enrich(entries):
        return [{**entry, "pricing": {"status": "unknown"}} for entry in entries]

    async def empty():
        return []

    monkeypatch.setattr(tool_pricing, "enrich_tools", enrich)
    monkeypatch.setattr(tool_pricing, "price_hint", lambda pricing: "Fixture pricing unavailable.")
    monkeypatch.setattr(mangrove_proxy, "catalog", empty)
    monkeypatch.setattr(marketplace_proxy, "list_tools", empty)
    server = PricedFastMCP("metadata-tests", stateless_http=True, json_response=True)
    tools.register(server)
    return server


@pytest.mark.asyncio
async def test_every_visible_local_tool_has_explicit_effect_hints(metadata_server):
    tools = await metadata_server.list_tools()
    assert {tool.name for tool in tools} == set(EXPECTED_HINTS)
    assert len({tool.title for tool in tools}) == len(tools)
    for tool in tools:
        assert tool.title and tool.title.strip()
        assert tool.annotations is not None, tool.name
        hints = tool.annotations.model_dump(exclude_unset=True)
        assert tuple(hints.get(key) for key in HINT_KEYS) == EXPECTED_HINTS[tool.name], tool.name


@pytest.mark.asyncio
async def test_enrichment_preserves_receiver_hints_and_interaction_metadata(metadata_server, monkeypatch):
    from src.mcp import mangrove_proxy, marketplace_proxy

    remote = Tool(name="receiver_action", title="Receiver action", inputSchema={"type": "object"},
                  annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True,
                                              idempotentHint=False, openWorldHint=True),
                  _meta={"receiver/approval": "required"})

    async def catalog():
        return [remote]

    for proxy, name in [(mangrove_proxy, "catalog"), (marketplace_proxy, "list_tools")]:
        with monkeypatch.context() as patch:
            patch.setattr(proxy, name, catalog)
            discovered = {tool.name: tool for tool in await metadata_server.list_tools()}
            assert discovered[remote.name].model_dump() == remote.model_dump()
            submit = discovered["marketplace_submit"]
            assert submit.meta["anthropic/requiresUserInteraction"] is True
            assert submit.meta["mangrove/pricing"] == {"status": "unknown"}
            assert submit.annotations.readOnlyHint is False


def test_tools_list_wire_exposes_quantity_units_and_hints(metadata_server):
    # This is the SDK's ASGI transport only: no agent lifespan, DB, scheduler,
    # provider client or tool handler is started. TestClient uses no socket.
    with TestClient(metadata_server.streamable_http_app(), base_url="http://127.0.0.1:8000") as client:
        headers = {"Accept": "application/json, text/event-stream"}
        initialized = client.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 1,
            "method": "initialize", "params": {"protocolVersion": "2025-06-18", "capabilities": {},
            "clientInfo": {"name": "metadata-fixture", "version": "1"}}})
        assert initialized.status_code == 200
        headers["MCP-Protocol-Version"] = initialized.json()["result"]["protocolVersion"]
        notification = client.post("/mcp", headers=headers,
                                   json={"jsonrpc": "2.0", "method": "notifications/initialized"})
        assert notification.status_code == 202
        response = client.post("/mcp", headers=headers,
                               json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        assert response.status_code == 200
        result = response.json()
    assert "error" not in result
    tools = {tool["name"]: tool for tool in result["result"]["tools"]}
    assert set(tools) == set(EXPECTED_HINTS)
    for name, tool in tools.items():
        assert tool["title"]
        assert tuple(tool["annotations"][key] for key in HINT_KEYS) == EXPECTED_HINTS[name]
    swap = tools["execute_swap"]
    amount = swap["inputSchema"]["properties"]["amount"]
    assert amount["type"] == "number"
    assert "amount" in swap["inputSchema"]["required"]
    assert set(amount) == {"type", "title", "description"}  # Metadata, no new constraints/defaults.
    for text in (swap["description"], amount["description"]):
        assert "buy" in text and "output" in text and "sell" in text and "input" in text
        assert "USDC" in text and "mark price" in text
        assert "REST" in text and "fixed" in text
    from src.mcp.registry import list_tools
    entry = next(tool for tool in list_tools() if tool["name"] == "execute_swap")
    parameter = next(param for param in entry["parameters"] if param["name"] == "amount")
    assert parameter["description"] == amount["description"]
    assert tools["marketplace_submit"]["_meta"]["anthropic/requiresUserInteraction"] is True


@pytest.mark.asyncio
async def test_quantity_description_is_in_sdk_schema_and_registry(metadata_server):
    from src.mcp.registry import list_tools

    swap = next(tool for tool in await metadata_server.list_tools() if tool.name == "execute_swap")
    amount = swap.inputSchema["properties"]["amount"]
    assert "buy uses output-asset units" in amount.get("description", "")
    assert "sell uses input-asset units" in amount["description"]
    assert "not a fixed USDC spend cap" in amount["description"]
    assert "REST /dex/swap uses the exact input-token amount" in amount["description"]
    registry = next(tool for tool in list_tools() if tool["name"] == "execute_swap")
    assert next(param for param in registry["parameters"] if param["name"] == "amount")["description"] == amount["description"]


@pytest.mark.parametrize("side,explicit,ref_price,expected", [
    ("buy", False, None, ("USDC", "ETH", 2500.0)),
    ("buy", False, 2000.0, ("USDC", "ETH", 2000.0)),
    ("sell", False, None, ("ETH", "USDC", 1.0)),
    ("buy", True, None, ("USDC", "ETH", 1.0)),
    ("sell", True, None, ("ETH", "USDC", 1.0)),
])
def test_existing_quantity_resolution_remains_unsigned(side, explicit, ref_price, expected, monkeypatch):
    from src.services import order_executor

    monkeypatch.setattr(order_executor, "_fetch_mark_price", lambda symbol: 2500.0)
    intent = SimpleNamespace(side=side, symbol="ETH", amount=1.0, ref_price=ref_price,
        input_token_address=("USDC" if side == "buy" else "ETH") if explicit else None,
        output_token_address=("ETH" if side == "buy" else "USDC") if explicit else None)
    # Pure resolver only. No quote, signing, execute_one, wallet or provider call.
    assert order_executor._resolve_tokens(intent) == expected


_BASE_USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
_SEPOLIA_USDC = "0x036CbD53842c5426634e7929541eC2318f3dCF7e"


@pytest.mark.asyncio
@pytest.mark.parametrize("input_token,output_token,chain_id", [
    ("DAI", "ETH", 8453), ("ETH", "DAI", 8453),
    ("ETH", "eth", 8453), ("USDC", "usdc", 8453),
    ("", "USDC", 8453), ("USDC", "", 8453),
    (_BASE_USDC, "USDC", 8453), ("USDC", _BASE_USDC, 8453),
    (_BASE_USDC, _BASE_USDC.lower(), 8453),
    (_BASE_USDC, "ETH", 84532), ("ETH", _BASE_USDC, 84532),
    (_SEPOLIA_USDC, "ETH", 8453), ("USDC", _SEPOLIA_USDC, 8453),
    ("ETH", "0x" + "1" * 40, 8453),
])
async def test_unsupported_pair_never_reaches_executor(metadata_server, monkeypatch, input_token, output_token, chain_id):
    from src.services import order_executor

    executor = Mock(side_effect=AssertionError("Unsupported pair reached action boundary"))
    monkeypatch.setattr(order_executor, "execute_one", executor)
    result = await metadata_server._tool_manager._tools["execute_swap"].run({
        "input_token": input_token, "output_token": output_token, "amount": 1,
        "chain_id": chain_id, "wallet_address": "unused-fixture", "slippage_pct": 0.001,
        "confirm": True, "api_key": "test-key-1",
    })
    error = json.loads(result)
    assert error["error"] is True
    assert error["code"] == "VALIDATION_ERROR"
    assert "USDC" in error["message"]
    executor.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("input_token,output_token,chain_id,side,symbol", [
    ("USDC", "ETH", 8453, "buy", "ETH"),
    ("ETH", "USDC", 8453, "sell", "ETH"),
    ("usdc", "eth", 1, "buy", "eth"),
    ("ETH", "uSdC", 10, "sell", "ETH"),
    (_BASE_USDC, "ETH", 8453, "buy", "ETH"),
    ("ETH", _BASE_USDC.lower(), 8453, "sell", "ETH"),
    (_SEPOLIA_USDC, "ETH", 84532, "buy", "ETH"),
    ("ETH", _SEPOLIA_USDC.lower(), 84532, "sell", "ETH"),
    ("0x" + "e" * 40, "USDC", 8453, "sell", "0x" + "e" * 40),
])
async def test_valid_pair_preserves_asset_quantity_at_unsigned_boundary(
    metadata_server, monkeypatch, input_token, output_token, chain_id, side, symbol,
):
    from src.services import order_executor

    def unsigned(intent, **kwargs):
        assert intent.side == side and intent.symbol == symbol and intent.amount == 1
        assert intent.input_token_address is None and intent.output_token_address is None
        assert kwargs == {"mode": "live", "wallet_address": "unused-fixture", "chain_id": chain_id,
                          "venue_id": None, "slippage_pct": 0.001}
        source, target, amount = order_executor._resolve_tokens(intent)
        assert (source, target, amount) == (("USDC", symbol, 2500) if side == "buy" else (symbol, "USDC", 1))
        return SimpleNamespace(tx_hash=None, status="unsigned-fixture", input_token=source, input_amount=amount,
                               output_token=target, output_amount=0, fill_price=2500, fees={}, id="not-an-order")

    executor = Mock(side_effect=unsigned)
    monkeypatch.setattr(order_executor, "execute_one", executor)
    monkeypatch.setattr(order_executor, "_fetch_mark_price", lambda symbol: 2500)
    result = await metadata_server._tool_manager._tools["execute_swap"].run({
        "input_token": input_token, "output_token": output_token, "amount": 1,
        "chain_id": chain_id, "wallet_address": "unused-fixture", "slippage_pct": 0.001,
        "confirm": True, "api_key": "test-key-1",
    })
    assert json.loads(result)["status"] == "unsigned-fixture"
    executor.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("api_key,confirm,expected", [
    ("invalid-fixture", True, "AUTH_INVALID_API_KEY"),
    ("test-key-1", False, "CONFIRMATION_REQUIRED"),
])
async def test_pair_guard_keeps_auth_and_confirmation_first(metadata_server, monkeypatch, api_key, confirm, expected):
    from src.services import order_executor

    executor = Mock(side_effect=AssertionError("Unauthenticated or unconfirmed request reached action"))
    monkeypatch.setattr(order_executor, "execute_one", executor)
    result = await metadata_server._tool_manager._tools["execute_swap"].run({
        "input_token": "DAI", "output_token": "ETH", "amount": 1, "chain_id": 8453,
        "wallet_address": "unused-fixture", "slippage_pct": 0.001, "api_key": api_key, "confirm": confirm,
    })
    assert json.loads(result)["code"] == expected
    executor.assert_not_called()
