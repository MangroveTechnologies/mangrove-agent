"""MCP tool auth and x402 payment enforcement tests.

Verifies the x402-gated hello_mangrove tool returns x402-native payment
requirements when called without payment in the MCP _meta. Garbage-payment
rejection is no longer in our code path — it's handled inside the x402
library's create_payment_wrapper, which has its own tests upstream.

Phase 4 will add auth tests for the mangrove-agent tools once they exist.
"""
import json
import os

os.environ.setdefault("ENVIRONMENT", "test")

import pytest  # noqa: E402

from src.mcp.server import create_mcp_server  # noqa: E402


async def _call_tool(server, tool_name: str, args: dict | None = None):
    """Call a named tool on the MCP server and return the CallToolResult.

    Tools wrapped with x402.mcp.create_payment_wrapper return a CallToolResult
    (with content + isError) rather than a raw JSON string, so callers unpack
    .content[0].text themselves when they need the body.
    """
    tools = server._tool_manager._tools
    tool = tools[tool_name]
    return await tool.run(args or {})


@pytest.mark.asyncio
async def test_hello_mangrove_returns_payment_requirements_without_credentials():
    server = create_mcp_server()
    result = await _call_tool(server, "hello_mangrove")
    assert result.isError is True
    assert result.content, "payment-required result should carry content"
    body = json.loads(result.content[0].text)
    assert body.get("x402Version") == 2
    accepts = body.get("accepts", [])
    assert len(accepts) > 0
    first = accepts[0]
    assert first["scheme"] == "exact"
    assert first["payTo"]
    assert first["network"]
