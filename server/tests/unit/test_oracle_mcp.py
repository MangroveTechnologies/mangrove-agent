"""Read-only Oracle discovery MCP tools delegate to the shared service."""
from __future__ import annotations

import json
from unittest.mock import Mock

import pytest
from mcp.server.fastmcp import FastMCP
from src.mcp.tools import _register_oracle_reads


@pytest.fixture
def server():
    value = FastMCP("oracle-read-tests")
    _register_oracle_reads(value)
    return value


async def invoke(server, name, args):
    return json.loads(await server._tool_manager._tools[name].run(args))


async def test_experiment_discovery_requires_auth_and_uses_shared_service(monkeypatch, server):
    list_experiments = Mock(return_value=[{"experiment_id": "exp-1", "status": "complete"}])
    monkeypatch.setattr("src.services.oracle.list_experiments", list_experiments)

    denied = await invoke(server, "agent_list_oracle_experiments", {})
    assert denied["code"] == "AUTH_INVALID_API_KEY"
    list_experiments.assert_not_called()

    monkeypatch.setattr("src.mcp.tools._require", lambda _: True)
    result = await invoke(server, "agent_list_oracle_experiments", {})
    assert result == [{"experiment_id": "exp-1", "status": "complete"}]
    list_experiments.assert_called_once_with()


@pytest.mark.parametrize("args", [{"limit": 0}, {"limit": 501}, {"offset": -1}])
async def test_results_reject_invalid_page_before_service_call(monkeypatch, server, args):
    list_results = Mock(side_effect=AssertionError("service must not be called"))
    monkeypatch.setattr("src.services.oracle.list_results", list_results)
    monkeypatch.setattr("src.mcp.tools._require", lambda _: True)

    result = await invoke(server, "agent_list_oracle_results", args)
    assert result["code"] == "VALIDATION_ERROR"
    list_results.assert_not_called()


@pytest.mark.parametrize("args", [{"limit": True}, {"offset": False}])
async def test_results_schema_rejects_boolean_page_values(monkeypatch, server, args):
    from mcp.server.fastmcp.exceptions import ToolError

    list_results = Mock(side_effect=AssertionError("service must not be called"))
    monkeypatch.setattr("src.services.oracle.list_results", list_results)
    monkeypatch.setattr("src.mcp.tools._require", lambda _: True)

    with pytest.raises(ToolError):
        await invoke(server, "agent_list_oracle_results", args)
    list_results.assert_not_called()


async def test_results_forward_only_supported_pagination_and_scope(monkeypatch, server):
    list_results = Mock(return_value={"results": [{"id": "r1"}], "total": 51, "offset": 50})
    monkeypatch.setattr("src.services.oracle.list_results", list_results)
    monkeypatch.setattr("src.mcp.tools._require", lambda _: True)

    result = await invoke(server, "agent_list_oracle_results", {
        "experiment_id": "exp-1", "limit": 1, "offset": 50,
    })
    assert result == {"results": [{"id": "r1"}], "total": 51, "offset": 50}
    list_results.assert_called_once_with("exp-1", limit=1, offset=50)


async def test_results_preserve_upstream_errors(monkeypatch, server):
    from src.shared.errors import SdkError

    monkeypatch.setattr("src.services.oracle.list_results", Mock(side_effect=SdkError("bad request")))
    monkeypatch.setattr("src.mcp.tools._require", lambda _: True)

    result = await invoke(server, "agent_list_oracle_results", {})
    assert result["error"] is True
    assert result["code"] == "SDK_ERROR"


@pytest.mark.parametrize(("status", "code"), [
    (401, "UPSTREAM_AUTHENTICATION_FAILED"),
    (403, "UPSTREAM_ACCESS_DENIED"),
])
async def test_results_sanitize_typed_upstream_access_denials(monkeypatch, server, status, code):
    from mangrove_ai.exceptions import APIError

    monkeypatch.setattr(
        "src.services.oracle.list_results",
        Mock(side_effect=APIError(status, "private", "secret upstream detail", "private_code")),
    )
    monkeypatch.setattr("src.mcp.tools._require", lambda _: True)

    result = await invoke(server, "agent_list_oracle_results", {})
    assert result["code"] == code
    assert result["upstream_status"] == status
    assert "secret upstream detail" not in json.dumps(result)
