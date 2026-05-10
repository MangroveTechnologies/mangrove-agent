"""MCP server registration tests."""
import os

os.environ.setdefault("ENVIRONMENT", "test")

from src.mcp.server import create_mcp_server


def test_mcp_server_creates_successfully():
    server = create_mcp_server()
    assert server.name == "mangrove-agent"


def test_mcp_server_is_singleton():
    server1 = create_mcp_server()
    server2 = create_mcp_server()
    assert server1 is server2
