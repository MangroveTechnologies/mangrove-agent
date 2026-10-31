"""MangroveAI contracts come from MCP; custody and transport remain local."""
import json
import socket
from contextlib import asynccontextmanager
from pathlib import Path
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
    monkeypatch.setattr(proxy, '_sessions', {})
    monkeypatch.setattr(proxy, '_sessions_lock', anyio.Lock())


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
    monkeypatch.setattr(x402_payer, 'pay_on_open_mcp_session', payer)
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
    monkeypatch.setattr(x402_payer, 'pay_on_open_mcp_session', payer)
    assert await proxy.call_tool('server_owned_tool', {'value': 7}) == response
    assert payer.call_args.kwargs['arguments'] == {'value': 7}
    assert payer.call_args.kwargs['name'] == 'server_owned_tool'
    assert payer.call_args.args[0] is client, 'the payment rides the shared session'
    client.call_tool.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize('name,args', [('missing', {'value': 7}), ('server_owned_tool', {'wrong': 7})])
async def test_unknown_tool_or_invalid_arguments_never_pay(transport, monkeypatch, name, args):
    client, _ = transport
    from src.services import x402_payer
    payer = AsyncMock()
    monkeypatch.setattr(x402_payer, 'pay_on_open_mcp_session', payer)
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
    from src.api.routes import discovery
    from src.mcp import marketplace_proxy, server
    from src.services import tool_pricing
    monkeypatch.setattr(proxy, 'catalog', AsyncMock(return_value=[tool()]))
    monkeypatch.setattr(marketplace_proxy, 'list_tools', AsyncMock(return_value=[]))
    monkeypatch.setattr(tool_pricing, 'enrich_tools', AsyncMock(side_effect=lambda rows: rows))
    server.reset_mcp_server()
    mcp = server.create_mcp_server()
    assert 'get_signal' not in mcp._tool_manager._tools
    assert 'evaluate_multiple_signals_series' not in mcp._tool_manager._tools
    assert 'list_wallets' in mcp._tool_manager._tools
    assert 'agent_list_strategies' not in mcp._tool_manager._tools
    assert 'agent_get_strategy' not in mcp._tool_manager._tools
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


@pytest.fixture
def endpoint_config(monkeypatch):
    catalog = json.loads((Path(__file__).parents[2] / "src/config/mangrove-endpoints.json").read_text())
    config = SimpleNamespace(
        MANGROVE_ACCESS_MODE="api-key", MANGROVE_API_KEY="dev_fixture",
        MANGROVEAI_BASE_URL=None, MANGROVE_ENDPOINTS=catalog, ENVIRONMENT="local",
        MANGROVE_SDK_TIMEOUT_SECONDS=1,
    )
    monkeypatch.setattr(proxy, "app_config", config)
    monkeypatch.setattr(proxy.oauth, "app_config", config)

    def blocked(*args, **kwargs):
        pytest.fail("Runtime destination checks must not open sockets or resolve DNS")

    for name in ("connect", "connect_ex"):
        monkeypatch.setattr(socket.socket, name, blocked)
    monkeypatch.setattr(socket, "getaddrinfo", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)
    return config


@pytest.mark.asyncio
@pytest.mark.parametrize("environment", ["local", "prod", "dev", "test"])
@pytest.mark.parametrize("base", [None, ""])
async def test_dev_key_runtime_discovery_requires_an_explicit_origin(monkeypatch, endpoint_config, environment, base):
    from src.shared.errors import ValidationError

    endpoint_config.ENVIRONMENT = environment
    endpoint_config.MANGROVEAI_BASE_URL = base
    transport = Mock(side_effect=AssertionError("HTTP transport must not be constructed"))
    monkeypatch.setattr(proxy, "_http_client", transport)
    with pytest.raises(ValidationError, match="Development API keys require"):
        await proxy.catalog()
    transport.assert_not_called()


@pytest.mark.parametrize("base", ["https://dev.example/api/v1", "http://127.0.0.1:5002/api/v1"])
def test_dev_key_runtime_explicit_origin_is_preserved(endpoint_config, base):
    endpoint_config.MANGROVEAI_BASE_URL = base
    assert proxy.endpoint() == base[:-7] + "/mcp/"


@pytest.mark.parametrize("key,mode,environment,expected", [
    ("prod_fixture", "api-key", "prod", "https://api.mangrovedeveloper.ai/mcp/"),
    ("local_fixture", "api-key", "test", "http://localhost:5001/mcp/"),
    ("test_fixture", "api-key", "test", "http://localhost:5001/mcp/"),
    (None, "x402", "local", "https://api.mangrovedeveloper.ai/mcp/"),
    ("dev_saved", "x402", "local", "https://api.mangrovedeveloper.ai/mcp/"),
])
def test_other_runtime_destination_modes_are_unchanged(endpoint_config, key, mode, environment, expected):
    endpoint_config.MANGROVE_API_KEY = key
    endpoint_config.MANGROVE_ACCESS_MODE = mode
    endpoint_config.ENVIRONMENT = environment
    assert proxy.endpoint() == expected


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
    monkeypatch.setattr(x402_payer, 'pay_on_open_mcp_session', payer)
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
    from collections import Counter

    import httpx
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
    # One session for discovery (keyless) and one for the keyed calls, each opened once
    # and reused -- not one per call, which made every call three extra requests.
    assert methods == {'initialize': 2, 'tools/list': 1, 'tools/call': 6}


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
    monkeypatch.setattr(x402_payer, 'pay_on_open_mcp_session', payer)
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
    monkeypatch.setattr(x402_payer, 'pay_on_open_mcp_session', payer)
    assert not (await proxy.call_tool('server_owned_tool', {'value': 1})).isError
    payer.assert_awaited_once()
    client.send_request.assert_not_called()


@pytest.mark.asyncio
async def test_a_call_waiting_on_a_dead_session_fails_at_once_with_its_error():
    """Live with a shared session, an HTTP 401 raised in the session's own task left the
    waiting caller hanging for its whole timeout; it now gets that error immediately."""
    import asyncio
    import time
    shared = proxy._SharedSession('https://receiver.test/mcp/', None)
    never = asyncio.Event()
    started = time.monotonic()

    async def die_soon():
        await asyncio.sleep(0.05)
        shared._error = RuntimeError('upstream 401')
        shared._lost.set()
    asyncio.ensure_future(die_soon())
    with pytest.raises(RuntimeError, match='upstream 401'):
        await shared.race(never.wait())
    assert time.monotonic() - started < 2


@pytest.mark.asyncio
async def test_a_live_session_returns_the_calls_result():
    shared = proxy._SharedSession('https://receiver.test/mcp/', None)

    async def answer():
        return 42
    assert await shared.race(answer()) == 42


# --- one shared session: one caller's failure must not end anyone else's call ----------

class _FakeShared:
    """Stands in for _SharedSession: opens instantly (or fails), and can be killed."""
    opened = 0
    fail_with = None
    open_delay = 0.0

    def __init__(self, endpoint_url, key):
        self.endpoint_url, self.key = endpoint_url, key
        self.client = SimpleNamespace(name='client')
        self._dead = False
        self.closed = False

    @property
    def alive(self):
        return not self._dead

    async def open(self):
        import asyncio
        type(self).opened += 1
        if type(self).open_delay:
            await asyncio.sleep(type(self).open_delay)
        if type(self).fail_with is not None:
            raise type(self).fail_with
        return self.client

    async def close(self):
        self.closed = True
        self._dead = True


@pytest.fixture
def fake_shared(monkeypatch):
    _FakeShared.opened, _FakeShared.fail_with, _FakeShared.open_delay = 0, None, 0.0
    monkeypatch.setattr(proxy, '_SharedSession', _FakeShared)
    return _FakeShared


@pytest.mark.asyncio
@pytest.mark.parametrize('error', [ValueError('malformed catalogue entry'),
                                   proxy.SdkError('output failed its schema'),
                                   TimeoutError('caller deadline')])
async def test_one_callers_error_keeps_the_live_session_for_everyone(fake_shared, error):
    """Audit H3: a malformed catalogue entry used to hang up the shared session, ending
    every concurrent call on it -- paid ones then reported as uncertain."""
    with pytest.raises(type(error)):
        async with proxy.session('https://receiver.test/mcp/') as client:
            raise error
    async with proxy.session('https://receiver.test/mcp/') as again:
        assert again._shared is client._shared, 'the same live session is still in use'
    assert fake_shared.opened == 1 and not client._shared.closed


@pytest.mark.asyncio
async def test_a_dead_session_is_dropped_and_the_next_call_opens_a_fresh_one(fake_shared):
    with pytest.raises(RuntimeError):
        async with proxy.session('https://receiver.test/mcp/') as client:
            client._shared._dead = True
            raise RuntimeError('transport closed')
    async with proxy.session('https://receiver.test/mcp/') as again:
        assert again._shared is not client._shared
    assert fake_shared.opened == 2


@pytest.mark.asyncio
async def test_a_late_failure_report_does_not_close_a_newer_session(fake_shared):
    async with proxy.session('https://receiver.test/mcp/') as first:
        pass
    first._shared._dead = True
    async with proxy.session('https://receiver.test/mcp/') as second:
        pass
    await proxy.drop_session('https://receiver.test/mcp/', None, first._shared)
    assert not second._shared.closed


@pytest.mark.asyncio
async def test_callers_behind_a_failed_open_get_that_failure_at_once(fake_shared):
    """Audit M8: each queued caller used to wait out its own open timeout in turn."""
    fake_shared.fail_with = proxy.SdkError('MangroveAI MCP did not open a session in time.')
    for _ in range(3):
        with pytest.raises(proxy.SdkError, match='did not open'):
            async with proxy.session('https://receiver.test/mcp/'):
                pass
    assert fake_shared.opened == 1, 'one open attempt; the rest answered from it'


@pytest.mark.asyncio
async def test_a_slow_destination_does_not_hold_up_another(fake_shared):
    import asyncio
    import time
    fake_shared.open_delay = 0.5
    slow = asyncio.ensure_future(proxy.shared_session('https://slow.test/mcp/'))
    await asyncio.sleep(0.05)
    fake_shared.open_delay = 0.0
    started = time.monotonic()
    await proxy.shared_session('https://fast.test/mcp/')
    assert time.monotonic() - started < 0.3, 'no global lock held across the slow open'
    await slow


@pytest.mark.asyncio
async def test_a_cancelled_session_gives_waiting_callers_an_ordinary_error():
    """Audit M8: a CancelledError recorded by the transport task was re-raised into
    unrelated callers, skipping their payment-uncertainty handling."""
    import asyncio
    shared = proxy._SharedSession('https://receiver.test/mcp/', None)
    shared._loop = asyncio.get_running_loop()

    async def run_then_get_cancelled():
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            shared._error = proxy.SdkError('MangroveAI MCP session was closed.')
            shared._lost.set()
            raise
    task = asyncio.ensure_future(run_then_get_cancelled())
    waiting = asyncio.ensure_future(shared.race(asyncio.Event().wait()))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(proxy.SdkError, match='session was closed'):
        await waiting


@pytest.mark.asyncio
async def test_the_transport_records_cancellation_as_an_ordinary_error(monkeypatch):
    import asyncio

    @asynccontextmanager
    async def hang(*a, **k):
        await asyncio.sleep(10)
        yield None
    monkeypatch.setattr(proxy, '_http_client', hang)
    shared = proxy._SharedSession('https://receiver.test/mcp/', None)
    shared._loop = asyncio.get_running_loop()
    task = asyncio.ensure_future(shared._run())
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert isinstance(shared._error, proxy.SdkError) and shared._lost.is_set()


@pytest.mark.asyncio
async def test_a_cancelled_opener_does_not_leak_its_transport_task(monkeypatch):
    import asyncio

    @asynccontextmanager
    async def hang(*a, **k):
        await asyncio.sleep(10)
        yield None
    monkeypatch.setattr(proxy, '_http_client', hang)
    shared = proxy._SharedSession('https://receiver.test/mcp/', None)
    opener = asyncio.ensure_future(shared.open())
    await asyncio.sleep(0.05)
    opener.cancel()
    with pytest.raises(asyncio.CancelledError):
        await opener
    await asyncio.sleep(0)
    assert shared._task.done() or shared._task.cancelled()


@pytest.mark.asyncio
@pytest.mark.parametrize('name,arguments', [
    ('list_strategies', {}), ('get_strategy', {'strategy_id': 'server-strategy-id'}),
    ('evaluate_strategy', {'strategy_id': 'server-strategy-id'}),
    ('update_strategy_status', {'strategy_id': 'server-strategy-id', 'new_status': 'inactive'}),
    ('save_strategy', {'strategy_name': 'Server-owned strategy'}),
])
async def test_strategy_operations_use_remote_mcp_without_local_storage(transport, monkeypatch, name, arguments):
    from src.mcp.server import create_mcp_server, reset_mcp_server

    client, _ = transport
    remote = Tool(name=name, description='Server saved strategies', inputSchema={
        'type': 'object', 'properties': {'strategy_id': {'type': 'string'}},
    })
    client.list_tools.return_value = SimpleNamespace(tools=[remote], nextCursor=None)
    expected = CallToolResult(content=[], structuredContent={'source': 'server'})
    client.send_request.return_value = expected
    local_db = Mock(side_effect=AssertionError('Remote reads must not open local SQLite'))
    monkeypatch.setattr('src.shared.db.sqlite.get_connection', local_db)
    reset_mcp_server()
    server = create_mcp_server()
    assert name not in server._tool_manager._tools
    assert await server.call_tool(name, arguments) == expected
    params = client.send_request.call_args.args[0].root.params
    assert params.name == name
    assert params.arguments == arguments
    local_db.assert_not_called()


@pytest.mark.asyncio
async def test_oauth_never_uses_saved_api_key_or_wallet(transport, monkeypatch):
    client, connections = transport
    from src.services import x402_payer
    monkeypatch.setattr(proxy.oauth, 'enabled', lambda: True)
    key = Mock(side_effect=AssertionError('OAuth must not read an API key'))
    payer = AsyncMock(side_effect=AssertionError('OAuth must not pay'))
    monkeypatch.setattr(proxy, '_api_key', key)
    monkeypatch.setattr(x402_payer, 'pay_on_open_mcp_session', payer)
    response = CallToolResult(isError=False, content=[TextContent(type='text', text='answer')])
    client.send_request.return_value = response
    assert await proxy.call_tool('server_owned_tool', {'value': 7}) == response
    assert connections[-1][1] is None
    key.assert_not_called()
    payer.assert_not_called()
