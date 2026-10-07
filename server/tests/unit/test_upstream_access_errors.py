"""Shared denial handling must preserve status without leaking upstream content."""
import json
from types import SimpleNamespace
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
@pytest.mark.parametrize('name,args,path,register,client_name,kind', [
    ('portfolio_value', {'addresses': '0x123'}, ('portfolio', 'value'), tools._register_wallet,
     'mangrove_markets_client', MarketsAPIError),
])
async def test_registered_tools_preserve_denial_once(monkeypatch, status, name, args, path,
                                                    register, client_name, kind):
    from src.shared.clients import mangrove

    upstream = Mock(side_effect=failure(kind, status))
    client = SimpleNamespace(**{path[0]: SimpleNamespace(**{path[1]: upstream})})
    monkeypatch.setattr(mangrove, client_name, lambda: client)
    monkeypatch.setattr(tools, '_require', lambda _: True)
    server = FastMCP('access-regression')
    register(server)
    result = json.loads(await server._tool_manager._tools[name].run(args))
    assert result['upstream_status'] == status
    assert result['retryable'] is False
    assert result['retry_payment'] is False
    assert 'secret' not in json.dumps(result)
    upstream.assert_called_once()


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


@pytest.mark.parametrize('status', [401, 403, 500])
@pytest.mark.parametrize('path,method', [
    ('/ohlcv?symbol=BTC', 'get_ohlcv'), ('/data?symbol=BTC', 'get_market_data'),
    ('/trending', 'get_trending'), ('/global', 'get_global_market'),
])
def test_market_rest_denial_status_and_unknown_error_safety(monkeypatch, status, path, method):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from src.api.routes import market
    from src.shared.auth.dependency import require_api_key

    upstream = Mock(side_effect=failure(AIAPIError, status))
    sdk = SimpleNamespace(crypto_assets=SimpleNamespace(**{method: upstream}))
    monkeypatch.setattr(market, 'mangrove_ai_client', lambda: sdk)
    app = FastAPI()
    app.include_router(market.router)
    app.dependency_overrides[require_api_key] = lambda: 'test-local-identity'
    app.add_exception_handler(AgentError, agent_error_handler)
    with TestClient(app) as client:
        response = client.get('/market' + path)
    assert response.status_code == (status if status in (401, 403) else 502)
    if status in (401, 403):
        assert response.json()['upstream_status'] == status
    else:
        assert response.json()['code'] == 'SDK_ERROR'
    assert 'secret' not in response.text
    upstream.assert_called_once()


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
