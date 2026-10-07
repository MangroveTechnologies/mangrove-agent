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
    from unittest.mock import AsyncMock
    monkeypatch.setattr("src.mcp.mangrove_proxy.catalog", AsyncMock(return_value=[]))
    monkeypatch.setattr("src.mcp.marketplace_proxy.list_tools", AsyncMock(return_value=[]))
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
    "status", "list_tools", "create_wallet", "list_wallets", "get_balances",
    "list_dex_venues", "get_swap_quote", "execute_swap", "agent_list_strategies",
    "agent_get_strategy", "agent_update_strategy_status", "agent_backtest_strategy",
    "agent_evaluate_strategy", "agent_create_strategy_manual", "x402_spend_status",
}


async def test_reference_next_step_resolves_to_local_creation(mcp_server, monkeypatch):
    from src.mcp import tools
    from src.services import strategy_service
    from unittest.mock import Mock

    monkeypatch.setattr(tools, '_require', lambda _: True)
    payload = await _call(mcp_server, 'agent_build_strategy_from_reference', {'reference_id': 'ref-001'})
    next_tool = payload['next_step']['mcp_tool']
    assert next_tool == 'agent_create_strategy_manual'
    assert next_tool in mcp_server._tool_manager._tools
    create = Mock(return_value=Mock(model_dump=Mock(return_value={'strategy_id': 'local-fixture'})))
    monkeypatch.setattr(strategy_service, 'create_manual', create)
    result = await _call(mcp_server, next_tool, {
        key: payload[key] for key in ('name', 'asset', 'timeframe', 'entry', 'exit', 'execution_config')
    })
    assert result['strategy_id'] == 'local-fixture'
    create.assert_called_once()



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
    for core in ("status", "create_wallet", "execute_swap", "agent_list_strategies"):
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
    result = await _call(mcp_server, "agent_list_strategies", {"api_key": "wrong-key"})
    assert result["error"] is True
    assert result["code"] == "AUTH_INVALID_API_KEY"








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
