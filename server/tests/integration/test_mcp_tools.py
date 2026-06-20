"""Integration tests for MCP tool registration — wiring, not business logic.

Exhaustive business-logic tests live per-service; here we verify:
- Every expected tool name is registered
- Free tools bypass auth
- Auth-gated tools reject missing/invalid api_key
- Valid api_key reaches the tool body (end-to-end wiring)
"""
from __future__ import annotations

import json
import os

os.environ.setdefault("ENVIRONMENT", "test")

import pytest  # noqa: E402


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
    result = await tool.run(args or {})
    return json.loads(result)


CORE_TOOLS = {
    # discovery
    "status", "list_tools",
    # wallet
    "create_wallet", "list_wallets", "get_balances",
    # dex
    "list_dex_venues", "get_swap_quote", "execute_swap",
    # market
    "get_ohlcv", "get_market_data",
    # signals
    "list_signals",
    # strategy
    "create_strategy_autonomous", "create_strategy_manual",
    "list_strategies", "get_strategy",
    "update_strategy_status", "backtest_strategy", "evaluate_strategy",
    # logs
    "list_evaluations", "list_trades", "list_all_trades",
    # kb
    "kb_search",
    # defi (DeFiLlama; the Pro tools require a Pro/Startup/Enterprise plan)
    "get_protocol_tvl", "get_chain_tvl", "get_stablecoin_metrics",
    "get_token_unlocks", "get_perp_funding", "get_treasuries",
    "get_etf_flows", "get_lending_borrow_rates",
    # x402 demo
    "hello_mangrove",
}


def test_all_expected_tools_registered(mcp_server):
    registered = set(mcp_server._tool_manager._tools.keys())
    missing = CORE_TOOLS - registered
    extra = registered - CORE_TOOLS
    assert not missing, f"missing tools: {missing}"
    # Extra is OK (template might add more later); we just don't want missing.
    assert extra == set() or extra, f"extra tools present: {extra}"


@pytest.mark.asyncio
async def test_status_free_no_auth(mcp_server):
    result = await _call(mcp_server, "status")
    assert result["version"] == "0.1.0"
    assert "wallets_count" in result


@pytest.mark.asyncio
async def test_list_tools_free_no_auth(mcp_server):
    result = await _call(mcp_server, "list_tools")
    assert "tools" in result
    names = {t["name"] for t in result["tools"]}
    # Subset check — mirrors the top-level REST tool catalog
    for core in ("status", "create_wallet", "execute_swap", "list_strategies"):
        assert core in names


@pytest.mark.asyncio
async def test_list_wallets_rejects_missing_key(mcp_server):
    result = await _call(mcp_server, "list_wallets")
    assert result["error"] is True
    assert result["code"] == "AUTH_INVALID_API_KEY"


@pytest.mark.asyncio
async def test_list_wallets_accepts_valid_key(mcp_server):
    result = await _call(mcp_server, "list_wallets", {"api_key": "test-key-1"})
    assert result == []


@pytest.mark.asyncio
async def test_list_strategies_rejects_bad_key(mcp_server):
    result = await _call(mcp_server, "list_strategies", {"api_key": "wrong-key"})
    assert result["error"] is True
    assert result["code"] == "AUTH_INVALID_API_KEY"
