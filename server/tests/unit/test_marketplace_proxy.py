"""Server-owned discovery and local custody boundaries."""
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from mcp.types import CallToolResult, Tool

from src.config import app_config
from src.mcp import marketplace_proxy as proxy
from src.services import marketplace_catalog as catalog
from src.services.marketplace import MarketplaceError
from src.services.x402_payer import PaymentResult


@pytest.fixture
def remote():
    return Tool(name='marketplace_search', description='Fresh server description',
                inputSchema={'type': 'object', 'properties': {'limit': {'type': 'integer'}}},
                **{'_meta': {catalog.CONTRACT_META: {'version': 1, 'mode': 'read'}}})


@pytest.fixture
def receiver(monkeypatch, remote):
    monkeypatch.setattr(app_config, 'MANGROVEMARKETS_BASE_URL', 'https://markets.test')
    monkeypatch.setattr(app_config, 'MANGROVE_API_KEY', 'upstream-secret')
    state = {'tools': [remote.model_dump(by_alias=True, exclude_none=True)], 'requests': [],
             'cursor': None, 'status': 200}
    original = httpx.AsyncClient

    def handle(request):
        import json
        state['requests'].append(request)
        assert request.url == 'https://markets.test/mcp/'
        assert request.headers['authorization'] == 'Bearer upstream-secret'
        if state['status'] != 200:
            return httpx.Response(state['status'], headers={'Location': 'https://other.test'})
        if request.method == 'GET':
            return httpx.Response(405)
        data = json.loads(request.content)
        if data['method'].startswith('notifications/'):
            return httpx.Response(202)
        result = ({'protocolVersion': '2025-03-26', 'capabilities': {'tools': {}},
                   'serverInfo': {'name': 'markets', 'version': '1'}}
                  if data['method'] == 'initialize' else
                  {'tools': state['tools'], 'nextCursor': state['cursor']})
        return httpx.Response(200, json={'jsonrpc': '2.0', 'id': data['id'], 'result': result})

    def client(**kwargs):
        assert kwargs['trust_env'] is False
        assert kwargs['follow_redirects'] is False
        return original(transport=httpx.MockTransport(handle), **kwargs)
    monkeypatch.setattr(httpx, 'AsyncClient', client)
    return state


@pytest.mark.asyncio
async def test_catalog_tracks_server_contract_without_local_registration(receiver):
    first = await catalog.catalog()
    assert first[0].description == 'Fresh server description'
    receiver['tools'][0]['description'] = 'Changed on server'
    receiver['tools'][0]['inputSchema']['properties']['new_filter'] = {'type': 'boolean'}
    second = await proxy.list_tools()
    assert second[0].description.startswith('Changed on server')
    assert 'new_filter' in second[0].inputSchema['properties']
    assert '_agent' not in receiver['tools'][0]['inputSchema']['properties']


@pytest.mark.asyncio
async def test_catalog_ignores_unmarked_tools(receiver):
    receiver['tools'].append({'name': 'unrelated', 'inputSchema': {}})
    assert len(await catalog.catalog()) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('malformation', ['duplicate', 'cursor', 'version', 'reserved'])
async def test_catalog_fails_closed(receiver, malformation):
    if malformation == 'duplicate':
        receiver['tools'] *= 2
    elif malformation == 'cursor':
        receiver['cursor'] = 'repeated'
    elif malformation == 'version':
        receiver['tools'][0]['_meta'][catalog.CONTRACT_META]['version'] = 2
    else:
        receiver['tools'][0]['name'] = 'marketplace_submit'
    with pytest.raises(MarketplaceError):
        await catalog.catalog()


@pytest.mark.asyncio
@pytest.mark.parametrize('status', [302, 401, 403, 503])
async def test_discovery_failure_never_follows_redirect(receiver, status):
    from src.shared.errors import AgentError
    receiver['status'] = status
    with pytest.raises(AgentError):
        await catalog.catalog()
    assert all(str(r.url) == 'https://markets.test/mcp/' for r in receiver['requests'])


@pytest.mark.parametrize('field,value', [('$ref', 'https://evil.test/schema'),
                                       ('$id', 'https://evil.test/base'),
                                       ('$dynamicRef', '#/properties/limit'),
                                       ('$recursiveRef', '#')])
def test_external_or_dynamic_schema_resolution_rejected(remote, field, value):
    remote.inputSchema['properties']['limit'][field] = value
    with pytest.raises(MarketplaceError):
        catalog.validate_tool(remote)


def test_invalid_schema_rejected(remote):
    remote.inputSchema['properties']['limit']['type'] = 'imaginary'
    with pytest.raises(MarketplaceError):
        catalog.validate_tool(remote)


@pytest.mark.asyncio
async def test_proxy_strips_local_controls_and_preserves_native_result(monkeypatch, remote):
    from src.mcp import tools
    monkeypatch.setattr(tools, '_require', lambda key: key == 'local-secret')
    monkeypatch.setattr(catalog, 'get_tool', AsyncMock(return_value=remote))
    native = CallToolResult(content=[], structuredContent={'listings': []},
                            **{'_meta': {'mangrove/quota': {'operation_id': 'read-id'}}})
    read = AsyncMock(return_value=PaymentResult(status_code=200, body=[], paid=False,
                    mcp_result=native.model_dump(by_alias=True, exclude_none=True)))
    monkeypatch.setattr(proxy.marketplace_reads, 'read', read)
    result = await proxy.call_tool(remote.name, {'limit': 5, '_agent': {'api_key': 'local-secret'}})
    assert not result.isError
    assert result.structuredContent == {'listings': []}
    assert result.meta['mangrove/quota']['operation_id'] == 'read-id'
    assert result.meta['mangrove/agent']['paid'] is False
    assert read.call_args.args == ('marketplace_search', {'limit': 5})


@pytest.mark.asyncio
async def test_unauthenticated_call_never_contacts_markets(monkeypatch):
    from src.mcp import tools
    monkeypatch.setattr(tools, '_require', lambda key: False)
    discover = AsyncMock()
    monkeypatch.setattr(catalog, 'get_tool', discover)
    assert (await proxy.call_tool('marketplace_search', {})).isError
    discover.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize('denied', [False, True])
async def test_ownership_tool_only_prepares(monkeypatch, remote, denied):
    from src.mcp import tools
    remote.name = 'marketplace_new_action'
    remote.meta[catalog.CONTRACT_META] = {'version': 1, 'mode': 'ownership',
                                        'protocol': 'ownership-v1', 'actor_field': 'seller'}
    remote.inputSchema = {'type': 'object', 'properties': {'seller': {'type': 'string'},
                         'ownership_proof': {}, 'payment': {}, 'value': {'type': 'number'}},
                         'required': ['seller', 'value']}
    monkeypatch.setattr(tools, '_require', lambda key: True)
    monkeypatch.setattr(catalog, 'get_tool', AsyncMock(return_value=remote))
    payload = ({'error': True, 'code': 'OWNERSHIP_DENIED', 'message': 'Action not authorized.'}
               if denied else {'approval_id': 'preview', 'state': 'prepared'})
    prepare = Mock(return_value=payload)
    submit = Mock(side_effect=AssertionError('must not sign'))
    monkeypatch.setattr(proxy.marketplace, 'prepare', prepare)
    monkeypatch.setattr(proxy.marketplace, 'submit', submit)
    result = await proxy.call_tool(remote.name, {'value': 2, '_agent': {'wallet_address': 'wallet'}})
    assert result.structuredContent == payload
    assert result.isError is denied
    prepare.assert_called_once_with(remote.name, {'value': 2}, 'wallet')
    submit.assert_not_called()
    assert 'ownership_proof' not in proxy.exposed_tool(remote).inputSchema['properties']
    invalid = await proxy.call_tool(remote.name, {'value': 2, 'ownership_proof': 'injected',
                                                '_agent': {'wallet_address': 'wallet'}})
    assert invalid.isError
    assert prepare.call_count == 1


@pytest.mark.asyncio
async def test_local_tools_survive_unavailable_markets(monkeypatch):
    from src.mcp.server import PricedFastMCP
    monkeypatch.setattr(proxy, 'list_tools', AsyncMock(side_effect=MarketplaceError('unavailable')))
    server = PricedFastMCP('test')
    @server.tool()
    def local_wallet() -> str:
        return 'local'
    assert [t.name for t in await server.list_tools()] == ['local_wallet']


@pytest.mark.asyncio
async def test_official_mcp_session_discovers_and_calls_dynamic_tool(monkeypatch, remote):
    from mcp.shared.memory import create_connected_server_and_client_session

    from src.mcp import tools
    from src.mcp.server import PricedFastMCP

    monkeypatch.setattr(tools, '_require', lambda key: key == 'local-secret')
    monkeypatch.setattr(catalog, 'catalog', AsyncMock(return_value=[remote]))
    result = CallToolResult(content=[], structuredContent={'listings': [{'id': 'example'}]})
    read = AsyncMock(return_value=PaymentResult(status_code=200, body=[], paid=False,
                    mcp_result=result.model_dump(by_alias=True, exclude_none=True)))
    monkeypatch.setattr(proxy.marketplace_reads, 'read', read)
    server = PricedFastMCP('test')
    async with create_connected_server_and_client_session(server) as session:
        discovered = await session.list_tools()
        assert [tool.name for tool in discovered.tools] == ['marketplace_search']
        assert '_agent' in discovered.tools[0].inputSchema['properties']
        response = await session.call_tool('marketplace_search',
                    {'limit': 5, '_agent': {'api_key': 'local-secret'}})
        assert not response.isError
        assert response.structuredContent == {'listings': [{'id': 'example'}]}
        assert response.meta['mangrove/agent']['paid'] is False
    assert 'marketplace_search' not in server._tool_manager._tools
    assert read.call_args.args == ('marketplace_search', {'limit': 5})


@pytest.mark.parametrize('metadata', [
    {'prepare_constraints': []}, {'wallet_defaults': []},
    {'wallet_defaults': {'unknown': {}}}, {'wallet_defaults': {'base': []}},
    {'prepare_constraints': {'ownership_proof': ''}},
    {'wallet_defaults': {'base': {'seller': 'different-wallet'}}},
])
def test_malformed_ownership_metadata_fails_closed(remote, metadata):
    remote.inputSchema['properties']['seller'] = {'type': 'string'}
    remote.meta[catalog.CONTRACT_META] = {'version': 1, 'mode': 'ownership',
        'protocol': 'ownership-v1', 'actor_field': 'seller', **metadata}
    with pytest.raises(MarketplaceError):
        proxy.exposed_tool(remote)


def test_wallet_dependent_defaults_are_not_advertised_as_universal(remote):
    remote.inputSchema['properties'].update(seller={'type': 'string'},
        chain={'type': 'string', 'default': 'base'}, currency={'type': 'string', 'default': 'USDC'})
    remote.meta[catalog.CONTRACT_META] = {'version': 1, 'mode': 'ownership',
        'protocol': 'ownership-v1', 'actor_field': 'seller', 'wallet_defaults': {
            'base': {'chain': 'base', 'currency': 'USDC'},
            'xrpl': {'chain': 'xrpl', 'currency': 'XRP'}}}
    exposed = proxy.exposed_tool(remote)
    assert 'default' not in exposed.inputSchema['properties']['chain']
    assert 'default' not in exposed.inputSchema['properties']['currency']
    assert remote.inputSchema['properties']['chain']['default'] == 'base'
