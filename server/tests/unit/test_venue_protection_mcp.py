"""Offline authorization and delegation checks for venue protection MCP tools."""

import json
from unittest.mock import Mock

from mcp.server.fastmcp import FastMCP

from src.mcp import broker_tools
from src.services import broker_execution, robinhood_client


async def test_venue_protection_status_requires_auth_before_service_call(monkeypatch):
    monkeypatch.setattr("src.mcp.tools._require", lambda _: False)
    protection_status = Mock(side_effect=AssertionError("unauthenticated call reached service"))
    monkeypatch.setattr(broker_execution, "protection_status", protection_status)
    server = FastMCP("venue-protection-auth-test")
    broker_tools.register(server)

    result = json.loads(
        await server._tool_manager._tools["agent_list_venue_protection"].run(
            {"strategy_id": "strategy-1", "api_key": "invalid"}
        )
    )

    assert result["code"] == "AUTH_INVALID_API_KEY"
    protection_status.assert_not_called()


async def test_venue_protection_status_delegates_to_local_read_only_status(monkeypatch):
    monkeypatch.setattr("src.mcp.tools._require", lambda _: True)
    payload = {"enabled": True, "positions": [], "deferred_exits": []}
    protection_status = Mock(return_value=payload)
    monkeypatch.setattr(broker_execution, "protection_status", protection_status)
    monkeypatch.setattr(
        broker_execution,
        "run",
        Mock(side_effect=AssertionError("status must not open a venue session")),
    )
    monkeypatch.setattr(
        robinhood_client,
        "invoke",
        Mock(side_effect=AssertionError("status must not invoke broker tools")),
    )
    server = FastMCP("venue-protection-status-test")
    broker_tools.register(server)

    result = json.loads(
        await server._tool_manager._tools["agent_list_venue_protection"].run(
            {"strategy_id": "strategy-2", "api_key": "valid"}
        )
    )

    assert result == payload
    protection_status.assert_called_once_with("strategy-2")


async def test_explicit_reconcile_tool_delegates_to_read_only_reconcile(monkeypatch):
    monkeypatch.setattr("src.mcp.tools._require", lambda _: True)
    run = Mock(return_value={"reconciled": [], "pending": 0})
    scheduled_maintenance = Mock(side_effect=AssertionError("tool must not run scheduled maintenance"))
    monkeypatch.setattr(broker_execution, "run", run)
    monkeypatch.setattr(broker_execution, "reconcile_pending", scheduled_maintenance)
    server = FastMCP("venue-reconcile-delegation-test")
    broker_tools.register(server)

    result = json.loads(
        await server._tool_manager._tools["agent_reconcile_venue_orders"].run(
            {"strategy_id": "strategy-3", "api_key": "valid"}
        )
    )

    assert result == {"reconciled": [], "pending": 0}
    run.assert_called_once_with(broker_execution.reconcile, "strategy-3")
    scheduled_maintenance.assert_not_called()
