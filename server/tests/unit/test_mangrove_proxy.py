"""MangroveAI contracts come from MCP; custody and transport remain local."""
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from mcp.types import CallToolResult, TextContent, Tool

from src.mcp import mangrove_proxy as proxy


def tool(name='server_owned_tool', field='value'):
    return Tool(name=name, description='Authoritative server description',
                inputSchema={'type': 'object', 'required': [field], 'properties': {field: {'type': 'integer'}}},
                annotations={'readOnlyHint': True}, _meta={'owner': 'MangroveAI'})


@pytest.fixture(autouse=True)
def reset_catalog(monkeypatch):
    import anyio
    monkeypatch.setattr(proxy, '_catalog_cache', None)
    monkeypatch.setattr(proxy, '_catalog_lock', anyio.Lock())


@pytest.fixture
def transport(monkeypatch):
    client = SimpleNamespace(initialize=AsyncMock(), list_tools=AsyncMock(), call_tool=AsyncMock(), send_request=AsyncMock())
    connections = []
    @asynccontextmanager
    async def session(url, key=None):
        connections.append((url, key))
        yield client
    monkeypatch.setattr(proxy, 'session', session)
    monkeypatch.setattr(proxy, 'endpoint', lambda: 'http://127.0.0.1:5002/mcp/')
    monkeypatch.setattr(proxy, '_api_key', lambda _: 'upstream-test-key')
    from src.mcp import tools
    monkeypatch.setattr(tools, '_require', lambda _: True)
    client.list_tools.return_value = SimpleNamespace(tools=[tool()], nextCursor=None)
    return client, connections


@pytest.mark.asyncio
async def test_discovery_preserves_complete_remote_contract_and_refreshes(transport):
    client, connections = transport
    first = (await proxy.catalog())[0]
    assert first.model_dump() == tool().model_dump()
    client.list_tools.return_value = SimpleNamespace(tools=[tool(field='changed')], nextCursor=None)
    assert (await proxy.catalog())[0].inputSchema == tool(field='changed').inputSchema
    assert all(key is None for _, key in connections)
    client.call_tool.assert_not_called()


@pytest.mark.asyncio
async def test_discovery_collects_pages_and_rejects_duplicates(transport):
    client, _ = transport
    client.list_tools.side_effect = [SimpleNamespace(tools=[tool('one')], nextCursor='next'), SimpleNamespace(tools=[tool('two')], nextCursor=None)]
    assert [t.name for t in await proxy.catalog()] == ['one', 'two']
    assert client.list_tools.call_args.kwargs == {'cursor': 'next'}
    client.list_tools.side_effect = None
    client.list_tools.return_value = SimpleNamespace(tools=[tool(), tool()], nextCursor=None)
    with pytest.raises(proxy.SdkError):
        await proxy.catalog()


@pytest.mark.asyncio
async def test_key_mode_forwards_arguments_and_preserves_upstream_failure(transport, monkeypatch):
    client, connections = transport
    from src.services import x402_payer
    payer = AsyncMock()
    monkeypatch.setattr(x402_payer, 'pay_remote_mcp', payer)
    response = CallToolResult(isError=True, content=[TextContent(type='text', text='not found')], structuredContent={'upstream_status': 404, 'correlation_id': 'server-correlation'})
    client.send_request.return_value = response
    actual = await proxy.call_tool('server_owned_tool', {'value': 7})
    assert actual == response
    assert client.send_request.call_args.args[0].root.params.arguments == {'value': 7}
    assert connections[-1][1] == 'upstream-test-key'
    payer.assert_not_called()


@pytest.mark.asyncio
async def test_wallet_mode_uses_existing_mcp_payer_and_keeps_result(transport, monkeypatch):
    client, _ = transport
    from src.services import x402_payer
    monkeypatch.setattr(proxy, '_api_key', lambda _: None)
    response = CallToolResult(isError=False, content=[TextContent(type='text', text='answer')], structuredContent={'answer': 42})
    payer = AsyncMock(return_value=SimpleNamespace(mcp_result=response.model_dump(by_alias=True)))
    monkeypatch.setattr(x402_payer, 'pay_remote_mcp', payer)
    assert await proxy.call_tool('server_owned_tool', {'value': 7}) == response
    assert payer.call_args.kwargs['arguments'] == {'value': 7}
    assert payer.call_args.kwargs['name'] == 'server_owned_tool'
    client.call_tool.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize('name,args', [('missing', {'value': 7}), ('server_owned_tool', {'wrong': 7})])
async def test_unknown_tool_or_invalid_arguments_never_pay(transport, monkeypatch, name, args):
    client, _ = transport
    from src.services import x402_payer
    payer = AsyncMock()
    monkeypatch.setattr(x402_payer, 'pay_remote_mcp', payer)
    monkeypatch.setattr(proxy, '_api_key', lambda _: None)
    assert (await proxy.call_tool(name, args)).isError
    payer.assert_not_called()
    client.call_tool.assert_not_called()


@pytest.mark.asyncio
async def test_local_auth_fails_before_discovery_or_payment(transport, monkeypatch):
    from src.mcp import tools
    monkeypatch.setattr(tools, '_require', lambda _: False)
    result = await proxy.call_tool('server_owned_tool', {'value': 1})
    assert result.isError
    transport[0].list_tools.assert_not_called()


@pytest.mark.asyncio
async def test_discovery_outage_does_not_fall_back_to_sdk(transport, monkeypatch):
    from src.shared.clients import mangrove
    sdk = Mock(side_effect=AssertionError('must not use SDK'))
    monkeypatch.setattr(mangrove, 'mangrove_ai_client', sdk)
    transport[0].list_tools.side_effect = RuntimeError('private upstream error')
    result = await proxy.call_tool('server_owned_tool', {'value': 1})
    assert result.isError
    assert 'private' not in str(result)
    sdk.assert_not_called()


def test_external_schema_refs_rejected():
    with pytest.raises(ValueError):
        proxy.validate_tool(Tool(name='unsafe', inputSchema={'$ref': 'https://untrusted/schema'}))


@pytest.mark.asyncio
async def test_server_and_discovery_use_same_dynamic_catalog(monkeypatch):
    from src.mcp import server, marketplace_proxy
    from src.services import tool_pricing
    from src.api.routes import discovery
    monkeypatch.setattr(proxy, 'catalog', AsyncMock(return_value=[tool()]))
    monkeypatch.setattr(marketplace_proxy, 'list_tools', AsyncMock(return_value=[]))
    monkeypatch.setattr(tool_pricing, 'enrich_tools', AsyncMock(side_effect=lambda rows: rows))
    server.reset_mcp_server()
    mcp = server.create_mcp_server()
    assert 'get_signal' not in mcp._tool_manager._tools
    assert 'evaluate_multiple_signals_series' not in mcp._tool_manager._tools
    assert 'list_wallets' in mcp._tool_manager._tools
    assert 'agent_list_strategies' in mcp._tool_manager._tools
    assert (await mcp.list_tools())[-1] == tool()
    assert (await discovery.tools())['tools'][-1]['inputSchema'] == tool().inputSchema
    call = AsyncMock(return_value=CallToolResult(content=[]))
    monkeypatch.setattr(proxy, 'call_tool', call)
    await mcp.call_tool('server_owned_tool', {'value': 3})
    call.assert_awaited_once_with('server_owned_tool', {'value': 3})
    server.reset_mcp_server()


@pytest.mark.parametrize('base', ['https://api.example/api/v1', 'http://127.0.0.1:5002/api/v1'])
def test_endpoint_uses_configured_server_origin(monkeypatch, base):
    monkeypatch.setattr(proxy, '_api_key', lambda _: 'test-key')
    monkeypatch.setattr(proxy, '_api_key_base_url', lambda _: base)
    assert proxy.endpoint() == base[:-7] + '/mcp/'


@pytest.mark.parametrize('base', ['http://public.example/api/v1', 'https://user:secret@api.example/api/v1', 'https://api.example/api/v1?key=secret'])
def test_wallet_endpoint_rejects_unsafe_destination(monkeypatch, base):
    monkeypatch.setattr(proxy, '_api_key', lambda _: None)
    monkeypatch.setattr(proxy, '_payment_destination', lambda _: {'base_url': base})
    with pytest.raises(proxy.SdkError):
        proxy.endpoint()


@pytest.mark.asyncio
@pytest.mark.parametrize('status', [401, 403])
@pytest.mark.parametrize('http_denial', [False, True])
@pytest.mark.parametrize('name', ['get_signal', 'search_signals', 'get_ohlcv', 'get_market_data', 'list_sweep_markets'])
async def test_official_client_access_denial_never_pays(monkeypatch, status, http_denial, name):
    import json
    import httpx
    from src.mcp import tools
    from src.services import x402_payer
    from src.shared.errors import UpstreamAccessError

    monkeypatch.setattr(tools, '_require', lambda _: True)
    monkeypatch.setattr(proxy, 'endpoint', lambda: 'https://receiver.test/mcp/')
    monkeypatch.setattr(proxy, '_api_key', lambda _: 'synthetic-upstream-key')
    payer = AsyncMock(side_effect=AssertionError('Denied keys must never pay'))
    monkeypatch.setattr(x402_payer, 'pay_remote_mcp', payer)
    calls = []
    expected = UpstreamAccessError(status).to_dict()

    def receive(request):
        if request.method != 'POST':
            return httpx.Response(405)
        message = json.loads(request.content)
        if 'id' not in message:
            return httpx.Response(202)
        if message['method'] == 'initialize':
            result = {'protocolVersion': message['params']['protocolVersion'], 'capabilities': {'tools': {}},
                      'serverInfo': {'name': 'fixture', 'version': '1'}}
        elif message['method'] == 'tools/list':
            assert 'authorization' not in request.headers
            result = {'tools': [tool(name=name).model_dump(by_alias=True)]}
        else:
            assert message['method'] == 'tools/call'
            assert request.headers['authorization'] == 'Bearer synthetic-upstream-key'
            assert message['params']['name'] == name
            assert message['params']['arguments'] == {'value': 7}
            calls.append(message)
            if http_denial:
                return httpx.Response(status, json={'message': 'SYNTHETIC_PRIVATE_BODY'})
            result = {'isError': True, 'structuredContent': expected,
                      'content': [{'type': 'text', 'text': json.dumps(expected)}]}
        return httpx.Response(200, json={'jsonrpc': '2.0', 'id': message['id'], 'result': result})

    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kw: original(transport=httpx.MockTransport(receive), **kw))
    result = await proxy.call_tool(name, {'value': 7})
    assert result.isError
    actual = dict(result.structuredContent)
    assert actual.pop('correlation_id')
    expected.pop('correlation_id')
    assert actual == expected
    assert 'SYNTHETIC_PRIVATE_BODY' not in result.model_dump_json()
    assert len(calls) == 1
    payer.assert_not_called()


@pytest.mark.asyncio
async def test_six_calls_share_catalog_and_do_not_rediscover_after_execution(transport):
    import asyncio
    client, connections = transport
    client.send_request.return_value = CallToolResult(content=[])
    results = await asyncio.gather(*(proxy.call_tool('server_owned_tool', {'value': n}) for n in range(6)))
    assert all(not r.isError for r in results)
    assert client.list_tools.await_count == 1
    assert client.send_request.await_count == 6
    assert len(connections) == 7


@pytest.mark.asyncio
async def test_expired_catalog_failure_never_uses_stale_contract(transport, monkeypatch):
    await proxy.catalog()
    target, _, rows = proxy._catalog_cache
    monkeypatch.setattr(proxy, '_catalog_cache', (target, 0, rows))
    transport[0].list_tools.side_effect = RuntimeError('unavailable')
    result = await proxy.call_tool('server_owned_tool', {'value': 1})
    assert result.isError
    transport[0].send_request.assert_not_called()


@pytest.mark.asyncio
async def test_catalog_copies_cannot_mutate_cached_schema(transport):
    rows = await proxy.catalog()
    rows[0].inputSchema.clear()
    assert (await proxy.catalog(refresh=False))[0].inputSchema == tool().inputSchema


@pytest.mark.asyncio
@pytest.mark.parametrize('phase', ['discovery', 'call'])
async def test_rate_limit_is_preserved_without_retry(transport, phase):
    import httpx
    client, _ = transport
    error = httpx.HTTPStatusError('SECRET', request=httpx.Request('POST', 'https://example.test'),
                                  response=httpx.Response(429, text='SECRET'))
    failing = client.list_tools if phase == 'discovery' else client.send_request
    failing.side_effect = ExceptionGroup('transport', [error])
    result = await proxy.call_tool('server_owned_tool', {'value': 1})
    assert result.structuredContent['code'] == 'UPSTREAM_RATE_LIMITED'
    assert result.structuredContent['upstream_status'] == 429
    assert 'SECRET' not in result.model_dump_json()
    assert failing.await_count == 1


@pytest.mark.asyncio
async def test_cached_output_schema_is_still_validated(transport):
    client, _ = transport
    contract = tool()
    contract.outputSchema = {'type': 'object', 'required': ['answer']}
    client.list_tools.return_value.tools = [contract]
    client.send_request.return_value = CallToolResult(content=[], structuredContent={'wrong': 1})
    assert (await proxy.call_tool('server_owned_tool', {'value': 1})).isError
    assert client.list_tools.await_count == 1


@pytest.mark.asyncio
async def test_real_mcp_client_six_reads_fetch_catalog_once(monkeypatch):
    import json
    import httpx
    from collections import Counter
    from src.mcp import tools
    monkeypatch.setattr(tools, '_require', lambda _: True)
    monkeypatch.setattr(proxy, 'endpoint', lambda: 'https://receiver.test/mcp/')
    monkeypatch.setattr(proxy, '_api_key', lambda _: 'synthetic-key')
    methods = Counter()

    def receive(request):
        if request.method != 'POST':
            return httpx.Response(405)
        message = json.loads(request.content)
        if 'id' not in message:
            return httpx.Response(202)
        methods[message['method']] += 1
        if message['method'] == 'initialize':
            result = {'protocolVersion': message['params']['protocolVersion'], 'capabilities': {'tools': {}},
                      'serverInfo': {'name': 'fixture', 'version': '1'}}
        elif message['method'] == 'tools/list':
            result = {'tools': [tool().model_dump(by_alias=True)]}
        else:
            assert message['method'] == 'tools/call'
            assert request.headers['authorization'] == 'Bearer synthetic-key'
            result = {'content': [], 'isError': False}
        return httpx.Response(200, json={'jsonrpc': '2.0', 'id': message['id'], 'result': result})

    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kw: original(transport=httpx.MockTransport(receive), **kw))
    for n in range(6):
        assert not (await proxy.call_tool('server_owned_tool', {'value': n})).isError
    assert methods == {'initialize': 7, 'tools/list': 1, 'tools/call': 6}


@pytest.mark.asyncio
@pytest.mark.parametrize('has_key', [True, False])
@pytest.mark.parametrize('upstream_error', [False, True])
async def test_public_free_read_never_enters_payment_client(transport, monkeypatch, has_key, upstream_error):
    from src.services import x402_payer
    client, connections = transport
    contract = tool()
    contract = Tool.model_validate({**contract.model_dump(), 'annotations': {
        'mangrove/access_level': 'public', 'mangrove/side_effect': 'read'}})
    client.list_tools.return_value.tools = [contract]
    monkeypatch.setattr(proxy, '_api_key', lambda _: 'test-key' if has_key else None)
    payer = AsyncMock(side_effect=AssertionError('Free reads must never authorize payment'))
    monkeypatch.setattr(x402_payer, 'pay_remote_mcp', payer)
    response = CallToolResult(content=[], isError=upstream_error,
                              structuredContent={'upstream_status': 402} if upstream_error else {'ok': True})
    client.send_request.return_value = response
    assert await proxy.call_tool('server_owned_tool', {'value': 1}) == response
    assert connections[-1][1] == ('test-key' if has_key else None)
    payer.assert_not_called()


@pytest.mark.asyncio
async def test_priced_public_read_keeps_payment_path(transport, monkeypatch):
    from src.services import x402_payer
    client, _ = transport
    client.list_tools.return_value.tools = [Tool(name='server_owned_tool', inputSchema=tool().inputSchema,
        annotations={'mangrove/access_level': 'public', 'mangrove/side_effect': 'read', 'mangrove/x402_price_usd': '0.01'})]
    monkeypatch.setattr(proxy, '_api_key', lambda _: None)
    payer = AsyncMock(return_value=SimpleNamespace(mcp_result={'content': []}))
    monkeypatch.setattr(x402_payer, 'pay_remote_mcp', payer)
    assert not (await proxy.call_tool('server_owned_tool', {'value': 1})).isError
    payer.assert_awaited_once()
    client.send_request.assert_not_called()
