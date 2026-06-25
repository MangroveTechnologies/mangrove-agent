"""MCP X-API-Key header → tool auth bridge.

Claude Code registers this server with the key as an HTTP header
(`claude mcp add --header "X-API-Key: <key>"`). FastMCP tools never receive
HTTP headers as params, so an ASGI middleware (src/app.py::_MCPApiKeyHeaderMiddleware)
captures the header into a ContextVar and the tools' `_require()` gate consults
it. Before this bridge, every auth-tier tool returned AUTH_INVALID_API_KEY after
the documented setup even though the client was authenticated.

These cover the bridge deterministically (the `_call` helper invokes the tool in
the same async context, so a ContextVar set here is visible to `_require`). The
full streamable-HTTP request path is exercised in live E2E verification.
"""
from __future__ import annotations

import json
import os

os.environ.setdefault("ENVIRONMENT", "test")

import pytest  # noqa: E402

from src.shared.auth.middleware import (  # noqa: E402
    get_request_api_key,
    reset_request_api_key,
    set_request_api_key,
)

VALID_KEY = "test-key-1"  # matches test-config.json API_KEYS


@pytest.fixture
def mcp_server(tmp_path, monkeypatch):
    db_file = tmp_path / "mcp.db"
    from src.config import app_config
    from src.services import scheduler_service as ss
    from src.shared.db import sqlite as db_mod

    monkeypatch.setattr(app_config, "DB_PATH", str(db_file))
    db_mod.reset_connection()
    ss.reset_scheduler_cache()

    from src.shared.db.sqlite import init_db
    init_db()

    from src.mcp.server import create_mcp_server
    server = create_mcp_server()
    yield server
    ss.reset_scheduler_cache()
    db_mod.reset_connection()


async def _call(server, name: str, args: dict | None = None) -> dict | list:
    tool = server._tool_manager._tools[name]
    return json.loads(await tool.run(args or {}))


@pytest.mark.asyncio
async def test_header_key_authenticates_without_param(mcp_server):
    """The header-derived key (set by the middleware) authenticates a tool
    called with NO api_key argument — the core of the fix."""
    token = set_request_api_key(VALID_KEY)
    try:
        result = await _call(mcp_server, "list_wallets")  # no api_key arg
        assert result == []  # authenticated → empty list, not an auth error
    finally:
        reset_request_api_key(token)


@pytest.mark.asyncio
async def test_rejects_when_no_header_and_no_param(mcp_server):
    result = await _call(mcp_server, "list_wallets")
    assert result["error"] is True
    assert result["code"] == "AUTH_INVALID_API_KEY"


@pytest.mark.asyncio
async def test_rejects_invalid_header_key(mcp_server):
    token = set_request_api_key("wrong-key")
    try:
        result = await _call(mcp_server, "list_wallets")
        assert result["error"] is True
        assert result["code"] == "AUTH_INVALID_API_KEY"
    finally:
        reset_request_api_key(token)


@pytest.mark.asyncio
async def test_explicit_param_still_works(mcp_server):
    """Explicit api_key param keeps working (backward compatible)."""
    result = await _call(mcp_server, "list_wallets", {"api_key": VALID_KEY})
    assert result == []


@pytest.mark.asyncio
async def test_middleware_extracts_header_into_contextvar():
    """_MCPApiKeyHeaderMiddleware pulls X-API-Key from the ASGI scope, exposes
    it via get_request_api_key() to the wrapped app, and resets it after."""
    from src.app import _MCPApiKeyHeaderMiddleware

    seen: dict[str, str] = {}

    async def stub_app(scope, receive, send):
        seen["key"] = get_request_api_key()

    async def receive():
        return {"type": "http.request"}

    async def send(_msg):
        return None

    mw = _MCPApiKeyHeaderMiddleware(stub_app)
    scope = {"type": "http", "headers": [(b"x-api-key", VALID_KEY.encode())]}
    await mw(scope, receive, send)

    assert seen["key"] == VALID_KEY
    assert get_request_api_key() == ""  # reset in finally after the request


@pytest.mark.asyncio
async def test_middleware_passes_through_non_http():
    from src.app import _MCPApiKeyHeaderMiddleware

    called: dict[str, bool] = {}

    async def stub_app(scope, receive, send):
        called["ok"] = True

    mw = _MCPApiKeyHeaderMiddleware(stub_app)
    await mw({"type": "lifespan"}, None, None)
    assert called.get("ok") is True
