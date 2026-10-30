"""Discover real local tools without starting the agent or executing an action."""
from __future__ import annotations

import socket
import sqlite3
from types import SimpleNamespace

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
