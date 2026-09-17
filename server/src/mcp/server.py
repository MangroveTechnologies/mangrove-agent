"""MCP server -- unified entry point for all agent tools.

Mounted at /mcp on the FastAPI app via Streamable HTTP transport.
"""
from mcp.server.fastmcp import FastMCP

_mcp_server = None


class PricedFastMCP(FastMCP):
    """Attach current price hints without mutating registered tool definitions."""

    async def list_tools(self):
        from src.services.tool_pricing import enrich_tools, price_hint

        tools = await super().list_tools()
        entries = await enrich_tools([{"name": tool.name} for tool in tools])
        result = []
        for tool, entry in zip(tools, entries, strict=True):
            pricing = entry.get("pricing")
            if pricing is not None:
                tool = tool.model_copy(update={
                    "description": (tool.description or "") + "\n\n" + price_hint(pricing),
                    "meta": {**(tool.meta or {}), "mangrove/pricing": pricing},
                })
            result.append(tool)
        return result


def reset_mcp_server() -> None:
    """Drop the cached MCP server. Used between TestClient lifespans where
    the prior session manager has been closed and can't be reused."""
    global _mcp_server
    _mcp_server = None


def create_mcp_server() -> FastMCP:
    """Create and configure the MCP server with all tools registered.

    Idempotent -- returns the same server instance on repeated calls
    to avoid duplicate tool registration warnings. Call reset_mcp_server()
    first if you need a fresh instance.
    """
    global _mcp_server
    if _mcp_server is not None:
        return _mcp_server

    # streamable_http_path="/" so when mounted at /mcp on the parent app,
    # the final endpoint is /mcp/ (instead of /mcp/mcp).
    # stateless_http=True so each request stands alone — no per-session task
    # group state means the session manager is safe to reuse across multiple
    # TestClient lifespans (and matches our single-user, request-driven model).
    _mcp_server = PricedFastMCP(
        "mangrove-agent", streamable_http_path="/", stateless_http=True, json_response=True
    )

    from src.mcp.tools import register
    register(_mcp_server)

    return _mcp_server
