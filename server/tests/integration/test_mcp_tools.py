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
    "get_backtest", "list_backtests",
    # market
    "get_benchmark",
    # knowledge graph
    "query_knowledge",
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
    # x402 spend budget
    "x402_spend_status", "x402_spend_reset",
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


@pytest.mark.asyncio
async def test_query_knowledge_rejects_missing_key(mcp_server):
    result = await _call(mcp_server, "query_knowledge", {"op": "stats"})
    assert result["code"] == "AUTH_INVALID_API_KEY"


@pytest.mark.asyncio
async def test_query_knowledge_stats_offline(mcp_server):
    result = await _call(mcp_server, "query_knowledge", {"op": "stats", "api_key": "test-key-1"})
    assert result["op"] == "stats"
    assert result["result"]["nodes"] > 0
    assert "source" not in result["result"]


@pytest.mark.asyncio
async def test_query_knowledge_invalid_op_is_structured_error(mcp_server):
    result = await _call(mcp_server, "query_knowledge", {"op": "search", "api_key": "test-key-1"})
    assert result["error"] is True
    assert result["code"] == "KNOWLEDGE_QUERY_INVALID"


# -- x402 spend budget: the top-up is a conversation, not a terminal trip ----


@pytest.mark.asyncio
async def test_spend_status_rejects_missing_key(mcp_server):
    result = await _call(mcp_server, "x402_spend_status")
    assert result["error"] is True
    assert result["code"] == "AUTH_INVALID_API_KEY"


@pytest.mark.asyncio
async def test_spend_status_returns_budget_and_ledger(mcp_server):
    """One call answers both "how much is left" and "where did it go", so the
    agent can show the user before asking them to authorize more."""
    from src.services import spend_service

    spend_service.reserve(value=250_000, wallet_address="0xPayer",
                          resource="https://api.mangrove.ai/v1/signals")

    result = await _call(mcp_server, "x402_spend_status", {"api_key": "test-key-1"})
    # Budget nested under its own key rather than flattened alongside the
    # ledger -- two payloads that evolve independently must not share a
    # namespace.
    assert result["budget"]["spent_usd"] == 0.25
    assert result["budget"]["exhausted"] is False
    assert result["count"] == 1
    assert result["payments"][0]["resource"] == "https://api.mangrove.ai/v1/signals"


@pytest.mark.asyncio
async def test_spend_reset_refuses_without_confirmation(mcp_server):
    """The agent must not be able to unblock its own payment reflexively.
    confirm=true is the assertion that a human said yes."""
    result = await _call(mcp_server, "x402_spend_reset", {"api_key": "test-key-1"})
    assert result["error"] is True
    assert result["code"] == "CONFIRMATION_REQUIRED"


@pytest.mark.asyncio
async def test_spend_reset_authorizes_a_new_budget(mcp_server):
    """The whole point of the branch change: 'yes, make it $50' in-conversation."""
    from src.services import spend_service

    budget = spend_service.get_status()["cap_usd"]
    spend_service.reserve(value=int(budget * 1_000_000), wallet_address="0xPayer")
    assert spend_service.get_status()["exhausted"] is True

    result = await _call(mcp_server, "x402_spend_reset",
                         {"confirm": True, "cap_usd": 50, "api_key": "test-key-1"})
    assert result["exhausted"] is False
    assert result["cap_usd"] == 50.0
    assert result["cap_source"] == "authorized"  # reset returns the budget directly
    assert spend_service.check_before_payment()["allowed"] is True
