"""Shared denial handling must preserve status without leaking upstream content."""
import json
from unittest.mock import Mock

import httpx
import pytest
from mangrove_ai.exceptions import APIError as AIAPIError
from mangrove_markets.exceptions import APIError as MarketsAPIError
from mcp.server.fastmcp import FastMCP

from src.mcp import tools
from src.shared.errors import (
    AgentError,
    SdkError,
    X402PaymentUncertain,
    X402SpendCapExceeded,
    agent_error_handler,
    upstream_access_error,
)


@pytest.fixture(autouse=True)
def block_network(monkeypatch):
    def reject(*args, **kwargs):
        raise AssertionError("Access regression tests must not use the network")
    monkeypatch.setattr(httpx.HTTPTransport, 'handle_request', reject)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, 'handle_async_request', reject)


def failure(kind, status):
    if kind == 'http':
        response = httpx.Response(status, request=httpx.Request('GET', 'https://example.test'))
        return httpx.HTTPStatusError('secret-body', request=response.request, response=response)
    return kind(status, 'denial', 'secret-body', 'PRIVATE_PROVIDER_CODE')


@pytest.mark.parametrize('kind', [AIAPIError, MarketsAPIError, 'http'])
@pytest.mark.parametrize('status', [401, 403])
@pytest.mark.parametrize('wrapped', [False, True])
def test_shared_handler_preserves_only_access_status(kind, status, wrapped):
    error = failure(kind, status)
    if wrapped:
        wrapper = SdkError('secret-wrapper', correlation_id='local-correlation')
        wrapper.__cause__ = error
        error = wrapper
    result = json.loads(tools._handle_upstream_error('TOOL_FAILED', error))
    assert result['upstream_status'] == status
    assert result['code'] == ('UPSTREAM_ACCESS_DENIED' if status == 403 else 'UPSTREAM_AUTHENTICATION_FAILED')
    assert result['retryable'] is False
    assert result['retry_payment'] is False
    assert 'secret' not in json.dumps(result)
    assert 'signal:read' not in json.dumps(result)
    if wrapped:
        assert result['correlation_id'] == 'local-correlation'


@pytest.mark.parametrize('status', [400, 402, 404, 429, 500, 503])
@pytest.mark.parametrize('kind', [AIAPIError, MarketsAPIError, 'http'])
def test_other_statuses_are_not_access_denials(kind, status):
    error = failure(kind, status)
    assert upstream_access_error(error) is None
    result = json.loads(tools._handle_upstream_error('TOOL_FAILED', error))
    assert result['code'] == 'TOOL_FAILED'
    assert 'secret' not in json.dumps(result)


@pytest.mark.parametrize('error', [X402SpendCapExceeded('budget reached'),
                                  X402PaymentUncertain(upstream_status=403)])
def test_payment_semantics_take_precedence_over_caused_access_errors(error):
    error.__cause__ = failure(AIAPIError, 403)
    assert upstream_access_error(error) is None
    assert json.loads(tools._handle_agent_error(error)) == error.to_dict()


def test_no_message_inference_or_cause_cycle():
    error = RuntimeError('HTTP 403 forbidden')
    error.status_code = 403
    assert upstream_access_error(error) is None
    wrapper = SdkError('wrapper')
    wrapper.__cause__ = wrapper
    assert upstream_access_error(wrapper) is None


@pytest.mark.asyncio
@pytest.mark.parametrize('status', [401, 403])
async def test_markets_proxy_preserves_denial_once(monkeypatch, status):
    from unittest.mock import AsyncMock

    from mcp.types import Tool

    from src.mcp import marketplace_proxy
    from src.services import marketplace_catalog, marketplace_reads

    remote = Tool(name='oneinch_portfolio_value', inputSchema={
        'type': 'object', 'properties': {'addresses': {'type': 'string'}},
    }, **{'_meta': {marketplace_catalog.READ_CONTRACT_META: {
        'version': 1, 'mode': 'read', 'access': 'api_key',
    }}})
    monkeypatch.setattr(tools, '_require', lambda _: True)
    monkeypatch.setattr(marketplace_catalog, 'get_tool', AsyncMock(return_value=remote))
    upstream = AsyncMock(side_effect=failure(MarketsAPIError, status))
    monkeypatch.setattr(marketplace_reads, 'read', upstream)
    response = await marketplace_proxy.call_tool(remote.name, {'addresses': '0x123'})
    result = response.structuredContent
    assert response.isError
    assert result['upstream_status'] == status
    assert result['retryable'] is False
    assert result['retry_payment'] is False
    assert 'secret' not in json.dumps(result)
    upstream.assert_awaited_once()


@pytest.mark.parametrize('status', [401, 403])
def test_rest_normalizes_wrapped_access_failure(status):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    app = FastAPI()
    app.add_exception_handler(AgentError, agent_error_handler)

    @app.get('/example')
    def example():
        raise SdkError('secret-wrapper', correlation_id='test-id') from failure(MarketsAPIError, status)

    with TestClient(app) as client:
        response = client.get('/example')
    assert response.status_code == status
    assert response.json()['upstream_status'] == status
    assert response.json()['correlation_id'] == 'test-id'
    assert 'secret' not in response.text


@pytest.mark.asyncio
@pytest.mark.parametrize('name,arguments', [
    ('cex_balances', {}),
    ('cex_validate_order', {'pair': 'XBTUSD', 'side': 'buy', 'volume': 0.01}),
    ('cex_sync_fills', {}),
])
async def test_missing_cex_credentials_remain_a_local_failure(monkeypatch, name, arguments):
    from src.services import cex_service

    loader = Mock(return_value=None)
    monkeypatch.setattr(cex_service.cex_credentials, 'load', loader)
    monkeypatch.setattr(tools, '_require', lambda _: True)
    server = FastMCP('local-credential-regression')
    tools._register_dex(server)
    result = json.loads(await server._tool_manager._tools[name].run(arguments))
    assert result['code'] == 'CEX_CREDENTIALS_MISSING'
    assert result['message'] == 'No Kraken account is connected on this agent.'
    assert 'upstream_status' not in result
    loader.assert_called_once_with('kraken')


@pytest.mark.asyncio
async def test_unexpected_cex_error_remains_sanitized(monkeypatch):
    from src.services import cex_service

    monkeypatch.setattr(cex_service, 'get_balances', Mock(side_effect=RuntimeError('secret-body')))
    monkeypatch.setattr(tools, '_require', lambda _: True)
    server = FastMCP('cex-sanitization-regression')
    tools._register_dex(server)
    result = json.loads(await server._tool_manager._tools['cex_balances'].run({}))
    assert result['code'] == 'CEX_ERROR'
    assert 'secret-body' not in json.dumps(result)


@pytest.mark.parametrize('path,body', [
    ('/balances', None),
    ('/validate-order', {'pair': 'XBTUSD', 'side': 'buy', 'volume': 0.01}),
    ('/sync-fills', {}),
])
def test_cex_rest_preserves_missing_connection(monkeypatch, path, body):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from src.api.routes import cex
    from src.services import cex_service
    from src.shared.auth.dependency import require_api_key

    monkeypatch.setattr(cex_service.cex_credentials, 'load', Mock(return_value=None))
    app = FastAPI()
    app.include_router(cex.router)
    app.dependency_overrides[require_api_key] = lambda: 'test-local-identity'
    app.add_exception_handler(AgentError, agent_error_handler)
    with TestClient(app) as client:
        response = client.get('/cex' + path) if body is None else client.post('/cex' + path, json=body)
    assert response.status_code == 409
    assert response.json()['code'] == 'CEX_CREDENTIALS_MISSING'
    assert response.json()['message'] == 'No Kraken account is connected on this agent.'


def test_mcp_authentication_error_is_not_a_payment_failure():
    from mcp.shared.exceptions import McpError
    from mcp.types import ErrorData
    result = upstream_access_error(McpError(ErrorData(code=-32010, message='private upstream detail')))
    assert result.to_dict()['code'] == 'UPSTREAM_AUTHENTICATION_FAILED'
    assert result.to_dict()['retry_payment'] is False
    assert 'private upstream detail' not in result.message
    assert upstream_access_error(McpError(ErrorData(code=-32002, message='execution failed'))) is None


def structured_failure(data, status=403):
    response = httpx.Response(status, json={"error": {"message": "secret token", "data": data}},
                              request=httpx.Request("POST", "https://example.test/mcp/"))
    return httpx.HTTPStatusError("private message", request=response.request, response=response)


def test_structured_denial_preserves_only_safe_fields():
    error = structured_failure({"code": "insufficient_scope", "tool": "secret-tool",
                                "required_scopes": ["strategy:create", "backtest:create"],
                                "missing_scopes": ["strategy:create"], "user_id": "secret-user"})
    result = upstream_access_error(error).to_dict()
    assert result["reason"] == "insufficient_scope"
    assert result["missing_scopes"] == ["strategy:create"]
    assert "strategy:create" in result["message"]
    assert "secret" not in json.dumps(result)
    assert result["retryable"] is result["retry_payment"] is False


@pytest.mark.parametrize("data", [None, [], {"code": []}, {"code": "secret-reason"},
                                   {"code": "insufficient_scope", "missing_scopes": ["secret-token"]},
                                   {"code": "insufficient_scope", "missing_scopes": [None]},
                                   {"code": "insufficient_scope", "missing_scopes": ["strategy:read"] * 65}])
def test_malformed_or_unrecognized_denial_details_are_not_exposed(data):
    result = upstream_access_error(structured_failure(data)).to_dict()
    assert "missing_scopes" not in result
    assert "secret" not in json.dumps(result)


@pytest.mark.parametrize("reason", ["insufficient_role", "not_org_member", "unmapped_tool"])
def test_role_and_availability_reasons_remain_distinct(reason):
    result = upstream_access_error(structured_failure({"code": reason})).to_dict()
    assert result["reason"] == reason
    assert "lacks permissions" not in result["message"]


def test_structured_details_do_not_reinterpret_authentication_failure():
    result = upstream_access_error(structured_failure({"code": "insufficient_scope", "missing_scopes": ["strategy:create"]}, 401)).to_dict()
    assert "reason" not in result
    assert result["upstream_status"] == 401


def test_mcp_permission_denial_retains_safe_details():
    from mcp.shared.exceptions import McpError
    from mcp.types import ErrorData
    error = McpError(ErrorData(code=-32012, message="secret", data={
        "code": "insufficient_scope", "missing_scopes": ["strategies:write"]}))
    assert upstream_access_error(error).to_dict()["missing_scopes"] == ["strategies:write"]


def test_oauth_proxy_preserves_structured_denial_without_payment(monkeypatch):
    from src.mcp import mangrove_proxy
    monkeypatch.setattr(mangrove_proxy.oauth, "enabled", lambda: True)
    monkeypatch.setattr(mangrove_proxy.oauth, "endpoint", lambda: "https://example.test/mcp/")
    result = mangrove_proxy.transport_error(structured_failure({
        "code": "insufficient_scope", "missing_scopes": ["backtests:run"]})).to_dict()
    assert result["missing_scopes"] == ["backtests:run"]
    assert "--oauth login" in result["suggestion"]
    assert result["retry_payment"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("status,size", [(403, 0), (403, 17000), (402, 0)])
async def test_streamed_denial_is_bounded_and_does_not_consume_payment_quotes(status, size):
    from src.mcp.mangrove_proxy import _capture_access_denial
    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield json.dumps({"error": {"data": {"code": "insufficient_scope",
                "missing_scopes": ["strategy:create"]}, "private": "x" * size}}).encode()
    response = httpx.Response(status, stream=Stream(), request=httpx.Request("POST", "https://example.test"))
    await _capture_access_denial(response)
    if status == 402:
        assert not response.is_stream_consumed
        assert "mangrove_denial" not in response.extensions
    else:
        error = httpx.HTTPStatusError("private", request=response.request, response=response)
        result = upstream_access_error(error).to_dict()
        if size:
            assert "missing_scopes" not in result
        else:
            assert result["missing_scopes"] == ["strategy:create"]
    await response.aclose()


@pytest.mark.parametrize("kind", [AIAPIError, MarketsAPIError])
def test_sdk_known_reason_is_preserved_without_parsing_private_message(kind):
    error = kind(403, "secret", "secret", "INSUFFICIENT_ROLE")
    result = upstream_access_error(error).to_dict()
    assert result["reason"] == "insufficient_role"
    assert "secret" not in json.dumps(result)
