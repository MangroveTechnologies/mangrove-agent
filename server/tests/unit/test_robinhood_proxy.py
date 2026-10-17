"""Offline tests for the authenticated, schema-preserving Robinhood MCP proxy."""

import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path
from threading import Lock
from types import SimpleNamespace
from unittest.mock import AsyncMock

import anyio
import pytest
from mcp.types import CallToolResult, TextContent, Tool, ToolAnnotations

from src.mcp import robinhood_proxy
from src.services import broker_execution, robinhood_client

ORDER_INPUTS = json.loads((Path(__file__).parents[1] / "fixtures/robinhood_order_inputs.json").read_text())


def remote_tool(*, name="get_portfolio", output_schema=None, annotations=None):
    return Tool(
        name=name,
        description="Live Robinhood description.",
        inputSchema={
            "type": "object",
            "properties": {"account_number": {"type": "string"}},
            "required": ["account_number"],
            "additionalProperties": False,
        },
        outputSchema=output_schema,
        annotations=annotations,
    )


@pytest.fixture
def robinhood_session(monkeypatch):
    tool = remote_tool(
        output_schema={
            "type": "object",
            "properties": {"data": {"type": "object"}},
            "required": ["data"],
        },
        annotations=ToolAnnotations(readOnlyHint=True),
    )
    client = AsyncMock()
    client.call_tool = AsyncMock()
    catalog_calls = []

    @asynccontextmanager
    async def session():
        yield client

    async def catalog(_client):
        catalog_calls.append(True)
        return {tool.name: tool}

    monkeypatch.setattr(robinhood_client, "session", session)
    monkeypatch.setattr(robinhood_client, "catalog", catalog)
    monkeypatch.setattr(robinhood_proxy, "_authenticated", lambda: True)
    monkeypatch.setattr(robinhood_proxy, "_audit", lambda _name, _outcome: None)
    return tool, client, catalog_calls


async def test_tools_list_preserves_live_schema_and_annotations(robinhood_session):
    tool, _, catalog_calls = robinhood_session

    result = await robinhood_proxy.list_tools()

    assert len(result) == 1
    assert result[0].name == robinhood_proxy.PREFIX + tool.name
    assert result[0].inputSchema == tool.inputSchema
    assert result[0].outputSchema == tool.outputSchema
    assert result[0].description == tool.description
    assert result[0].annotations == tool.annotations
    assert len(catalog_calls) == 1


async def test_priced_mcp_lists_live_robinhood_tools(monkeypatch, robinhood_session):
    from src.mcp import mangrove_proxy, marketplace_proxy
    from src.mcp.server import PricedFastMCP

    tool, _, _ = robinhood_session
    monkeypatch.setattr("src.services.tool_pricing.enrich_tools", AsyncMock(return_value=[]))
    monkeypatch.setattr(marketplace_proxy, "list_tools", AsyncMock(return_value=[]))
    monkeypatch.setattr(mangrove_proxy, "catalog", AsyncMock(return_value=[]))

    server = PricedFastMCP("test", streamable_http_path="/", stateless_http=True)
    listed = await server.list_tools()

    proxy_tool = next(item for item in listed if item.name == robinhood_proxy.PREFIX + tool.name)
    assert proxy_tool.inputSchema == tool.inputSchema
    assert proxy_tool.annotations == tool.annotations


async def test_priced_mcp_dispatches_namespaced_robinhood_tool(monkeypatch):
    from src.mcp import robinhood_proxy
    from src.mcp.server import PricedFastMCP

    expected = CallToolResult(content=[TextContent(type="text", text="forwarded")])
    handler = AsyncMock(return_value=expected)
    monkeypatch.setattr(robinhood_proxy, "call_tool", handler)
    server = PricedFastMCP("test", streamable_http_path="/", stateless_http=True)

    result = await server.call_tool(robinhood_proxy.PREFIX + "get_portfolio", [])

    assert result.model_dump(by_alias=True) == expected.model_dump(by_alias=True)
    handler.assert_awaited_once_with(robinhood_proxy.PREFIX + "get_portfolio", [])


async def test_unauthenticated_listing_does_not_open_robinhood_session(monkeypatch):
    opened = []

    @asynccontextmanager
    async def session():
        opened.append(True)
        yield AsyncMock()

    monkeypatch.setattr(robinhood_proxy, "_authenticated", lambda: False)
    monkeypatch.setattr(robinhood_client, "session", session)

    assert await robinhood_proxy.list_tools() == []
    assert opened == []


async def test_call_preserves_complete_mcp_result(robinhood_session):
    _, client, _ = robinhood_session
    result = CallToolResult(
        content=[TextContent(type="text", text='{"data":{"value":"12.34"}}')],
        structuredContent={"data": {"value": "12.34"}},
        meta={"robinhood/trace": "trace-1"},
    )
    client.call_tool.return_value = result

    actual = await robinhood_proxy.call_tool(
        robinhood_proxy.PREFIX + "get_portfolio", {"account_number": "acct"}
    )

    assert actual.model_dump(by_alias=True) == result.model_dump(by_alias=True)
    client.call_tool.assert_awaited_once_with("get_portfolio", {"account_number": "acct"})


async def test_call_forwards_unclassified_tool_annotation_without_inventing_policy(robinhood_session):
    tool, client, _ = robinhood_session
    tool.annotations = None
    result = CallToolResult(
        content=[TextContent(type="text", text='{"data":{}}')],
        structuredContent={"data": {}},
    )
    client.call_tool.return_value = result

    actual = await robinhood_proxy.call_tool(
        robinhood_proxy.PREFIX + "get_portfolio", {"account_number": "acct"}
    )

    assert actual.model_dump(by_alias=True) == result.model_dump(by_alias=True)
    client.call_tool.assert_awaited_once()


@pytest.mark.parametrize(
    ("tool_name", "accepted_types"),
    [
        ("place_crypto_order", ["market", "limit", "stop_loss", "stop_limit"]),
        ("place_equity_order", ["market", "limit", "stop_market", "stop_limit"]),
        ("place_option_order", ["market", "limit", "stop_market", "stop_limit"]),
    ],
)
async def test_proxy_preserves_actual_order_schemas_and_type_values(monkeypatch, tool_name, accepted_types):
    client = AsyncMock()
    client.call_tool = AsyncMock(return_value=CallToolResult(content=[TextContent(type="text", text="ok")]))
    tool = Tool(name=tool_name, inputSchema=ORDER_INPUTS[tool_name]["inputSchema"])
    tools = {tool_name: tool}
    type_description = tool.inputSchema["properties"]["type"]["description"]
    assert all(order_type in type_description for order_type in accepted_types)

    @asynccontextmanager
    async def session():
        yield client

    async def catalog(_client):
        return tools

    monkeypatch.setattr(robinhood_client, "session", session)
    monkeypatch.setattr(robinhood_client, "catalog", catalog)
    monkeypatch.setattr(robinhood_proxy, "_authenticated", lambda: True)
    monkeypatch.setattr(robinhood_proxy, "_audit", lambda _name, _outcome: None)

    for order_type in accepted_types:
        if tool_name == "place_crypto_order":
            args = {
                "rhs_account_number": "123",
                "symbol": "BTC-USD",
                "side": "buy",
                "quantity": "0.01",
                "type": order_type,
            }
        elif tool_name == "place_equity_order":
            args = {
                "account_number": "acct",
                "symbol": "AAPL",
                "side": "buy",
                "quantity": "1",
                "type": order_type,
            }
        else:
            args = {
                "account_number": "acct",
                "legs": [{"option_id": "contract", "side": "buy", "position_effect": "open"}],
                "quantity": "1",
                "type": order_type,
            }
        if order_type in {"limit", "stop_limit"}:
            args["limit_price" if tool_name != "place_option_order" else "price"] = "10"
        if order_type in {"stop_loss", "stop_market", "stop_limit"}:
            args["stop_price"] = "9"
        if order_type == "market" and tool_name == "place_option_order":
            args["time_in_force"] = "gfd"
        result = await robinhood_proxy.call_tool(
            robinhood_proxy.PREFIX + tool_name, args
        )
        assert not result.isError
    assert client.call_tool.await_count == len(accepted_types)


async def test_catalog_exposes_actual_advanced_order_input_schemas(monkeypatch):
    names = ["place_advanced_order", "review_advanced_order", "cancel_advanced_order", "get_advanced_orders"]
    tools = {name: Tool(name=name, inputSchema=ORDER_INPUTS[name]["inputSchema"]) for name in names}
    client = AsyncMock()

    @asynccontextmanager
    async def session():
        yield client

    async def catalog(_client):
        return tools

    monkeypatch.setattr(robinhood_client, "session", session)
    monkeypatch.setattr(robinhood_client, "catalog", catalog)
    monkeypatch.setattr(robinhood_proxy, "_authenticated", lambda: True)

    result = await robinhood_proxy.list_tools()

    observed = {tool.name.removeprefix(robinhood_proxy.PREFIX): tool for tool in result}
    for name in names:
        assert observed[name].inputSchema == ORDER_INPUTS[name]["inputSchema"]


async def test_invalid_arguments_are_rejected_before_remote_call(robinhood_session):
    _, client, _ = robinhood_session

    result = await robinhood_proxy.call_tool(
        robinhood_proxy.PREFIX + "get_portfolio",
        {"account_number": "acct", "unexpected": "secret"},
    )

    assert result.isError
    assert result.structuredContent["code"] == "VALIDATION_ERROR"
    client.call_tool.assert_not_awaited()


async def test_unknown_tool_is_rejected_using_live_catalog(robinhood_session):
    _, client, _ = robinhood_session

    result = await robinhood_proxy.call_tool(robinhood_proxy.PREFIX + "removed_tool", {})

    assert result.isError
    assert result.structuredContent["code"] == "UNKNOWN_TOOL"
    client.call_tool.assert_not_awaited()


async def test_upstream_error_result_is_preserved_without_retry(robinhood_session):
    _, client, _ = robinhood_session
    result = CallToolResult(
        isError=True,
        content=[TextContent(type="text", text="Robinhood rejected the request.")],
        structuredContent={"error": "rejected"},
    )
    client.call_tool.return_value = result

    actual = await robinhood_proxy.call_tool(
        robinhood_proxy.PREFIX + "get_portfolio", {"account_number": "acct"}
    )

    assert actual.model_dump(by_alias=True) == result.model_dump(by_alias=True)
    client.call_tool.assert_awaited_once()


async def test_transport_failure_is_unknown_and_never_retried(robinhood_session):
    _, client, _ = robinhood_session
    client.call_tool.side_effect = RuntimeError("SYNTHETIC_PRIVATE")

    result = await robinhood_proxy.call_tool(
        robinhood_proxy.PREFIX + "get_portfolio", {"account_number": "acct"}
    )

    assert result.isError
    assert result.structuredContent["code"] == "OUTCOME_UNKNOWN"
    assert "SYNTHETIC_PRIVATE" not in result.model_dump_json()
    client.call_tool.assert_awaited_once()


async def test_proxy_session_serializes_with_managed_broker_session(monkeypatch):
    tool = remote_tool(output_schema=None)
    client = AsyncMock()
    client.call_tool = AsyncMock(return_value=CallToolResult(content=[TextContent(type="text", text="ok")]))
    venue = SimpleNamespace(tools={tool.name: tool}, client=client)
    active = 0
    maximum_active = 0
    guard = Lock()

    async def tracked_with_venue(operation, *args):
        nonlocal active, maximum_active
        with guard:
            active += 1
            maximum_active = max(maximum_active, active)
        try:
            await asyncio.sleep(0.02)
            return await operation(venue, *args)
        finally:
            with guard:
                active -= 1

    async def managed_operation(_venue):
        return "managed"

    monkeypatch.setattr(broker_execution, "_with_venue", tracked_with_venue)
    monkeypatch.setattr(robinhood_proxy, "_authenticated", lambda: True)
    monkeypatch.setattr(robinhood_proxy, "_audit", lambda _name, _outcome: None)

    await asyncio.gather(
        robinhood_proxy.call_tool(robinhood_proxy.PREFIX + tool.name, {"account_number": "acct"}),
        anyio.to_thread.run_sync(lambda: broker_execution.run(managed_operation)),
    )

    assert maximum_active == 1


async def test_output_schema_mismatch_is_unknown_and_never_retried(robinhood_session):
    _, client, _ = robinhood_session
    client.call_tool.return_value = CallToolResult(
        content=[TextContent(type="text", text='{"wrong":true}')],
        structuredContent={"wrong": True},
    )

    result = await robinhood_proxy.call_tool(
        robinhood_proxy.PREFIX + "get_portfolio", {"account_number": "acct"}
    )

    assert result.isError
    assert result.structuredContent["code"] == "UPSTREAM_CONTRACT_MISMATCH"
    client.call_tool.assert_awaited_once()


async def test_proxy_requires_local_auth_before_opening_remote_session(monkeypatch):
    opened = []

    @asynccontextmanager
    async def session():
        opened.append(True)
        yield AsyncMock()

    monkeypatch.setattr(robinhood_proxy, "_authenticated", lambda: False)
    monkeypatch.setattr(robinhood_client, "session", session)
    monkeypatch.setattr(robinhood_proxy, "_audit", lambda _name, _outcome: None)

    result = await robinhood_proxy.call_tool(robinhood_proxy.PREFIX + "get_portfolio", {})

    assert result.isError
    assert result.structuredContent["code"] == "AUTH_REQUIRED"
    assert opened == []
