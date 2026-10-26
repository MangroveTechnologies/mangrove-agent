"""Markets read discovery must not expand payment or custody authority."""
import json
from unittest.mock import AsyncMock

import httpx
import pytest
from mcp.types import CallToolResult, TextContent, Tool

from src.config import app_config
from src.mcp import marketplace_proxy, tools
from src.mcp.server import PricedFastMCP
from src.services import marketplace_catalog as catalog
from src.services import marketplace_reads as reads
from src.services.marketplace import MarketplaceError
from src.services.x402_payer import PaymentResult


@pytest.fixture
def remote(monkeypatch):
    monkeypatch.setattr(app_config, 'MANGROVEMARKETS_BASE_URL', 'https://markets.test')
    return Tool(name='oneinch_balances', description='Server balance contract', inputSchema={
        'type': 'object', 'properties': {
            'wallet': {'type': 'string'}, 'chain_id': {'type': 'integer'},
        }, 'required': ['wallet', 'chain_id'],
    }, **{'_meta': {catalog.READ_CONTRACT_META: {
        'version': 1, 'mode': 'read', 'access': 'api_key',
    }}})


def test_read_contract_only_exposes_local_auth(remote):
    exposed = marketplace_proxy.exposed_tool(remote)
    assert set(exposed.inputSchema['properties']['_agent']['properties']) == {'api_key'}
    assert exposed.description.startswith(remote.description)
    assert '_agent' not in remote.inputSchema['properties']


@pytest.mark.parametrize('field', ['private_key', 'seed', 'mnemonic', 'api_key', 'signed_tx', 'payment'])
def test_read_contract_rejects_secrets_and_signing(remote, field):
    remote.inputSchema['properties'][field] = {'type': 'string'}
    with pytest.raises(MarketplaceError):
        catalog.validate_tool(remote)


@pytest.mark.parametrize('change', [
    {'mode': 'ownership', 'protocol': 'ownership-v1'}, {'access': 'x402'}, {'version': 2},
])
def test_read_contract_cannot_grant_payment_or_ownership(remote, change):
    remote.meta[catalog.READ_CONTRACT_META].update(change)
    with pytest.raises(MarketplaceError):
        catalog.validate_tool(remote)


@pytest.mark.asyncio
async def test_wallet_only_read_never_enters_payment_system(monkeypatch, remote):
    monkeypatch.setattr(reads, '_api_key', lambda _: None)
    payer = AsyncMock(side_effect=AssertionError('must not pay'))
    monkeypatch.setattr(reads.x402_payer, 'pay_mcp', payer)
    with pytest.raises(MarketplaceError, match='requires an API key'):
        await reads.read(remote.name, {'wallet': 'public', 'chain_id': 8453}, tool=remote)
    payer.assert_not_called()


@pytest.mark.asyncio
async def test_authenticated_read_reuses_transport_without_quota_or_payer(monkeypatch, remote):
    monkeypatch.setattr(reads, '_api_key', lambda _: 'upstream-secret')
    result = PaymentResult(status_code=200, body=[], paid=False,
                           mcp_result=CallToolResult(content=[]).model_dump(by_alias=True))
    transport = AsyncMock(return_value=result)
    monkeypatch.setattr(reads, '_key_read', transport)
    payer = AsyncMock(side_effect=AssertionError('must not pay'))
    monkeypatch.setattr(reads.x402_payer, 'pay_mcp', payer)
    args = {'wallet': 'public', 'chain_id': 8453}
    assert await reads.read(remote.name, args, tool=remote) is result
    assert transport.call_args.args[1:] == ('upstream-secret', remote.name, args, None)
    payer.assert_not_called()


@pytest.mark.asyncio
async def test_native_read_dispatch_preserves_results_and_metadata(monkeypatch, remote):
    monkeypatch.setattr(tools, '_require', lambda key: key == 'local-secret')
    monkeypatch.setattr(catalog, 'catalog', AsyncMock(return_value=[remote]))
    native = CallToolResult(content=[TextContent(type='text', text='provider unavailable')],
                           structuredContent={'error': True, 'code': 'BALANCE_FETCH_FAILED'},
                           isError=True, **{'_meta': {'trace': 'safe-correlation'}})
    transport = AsyncMock(return_value=PaymentResult(status_code=502, body=[], paid=False,
                          mcp_result=native.model_dump(by_alias=True)))
    monkeypatch.setattr(reads, 'read', transport)
    server = PricedFastMCP('read-dispatch')
    response = await server.call_tool(remote.name, {
        'wallet': 'public', 'chain_id': 8453, '_agent': {'api_key': 'local-secret'},
    })
    assert response.isError
    assert response.content == native.content
    assert response.structuredContent == native.structuredContent
    assert response.meta['trace'] == 'safe-correlation'
    assert transport.call_args.args == (remote.name, {'wallet': 'public', 'chain_id': 8453})


@pytest.mark.asyncio
@pytest.mark.parametrize('options', [{'wallet_address': 'wallet'}, {'operation_id': 'old-operation'}])
async def test_payment_options_rejected_before_read(monkeypatch, remote, options):
    monkeypatch.setattr(tools, '_require', lambda _: True)
    monkeypatch.setattr(catalog, 'get_tool', AsyncMock(return_value=remote))
    transport = AsyncMock()
    monkeypatch.setattr(reads, 'read', transport)
    response = await marketplace_proxy.call_tool(remote.name, {
        'wallet': 'public', 'chain_id': 8453, '_agent': options,
    })
    assert response.isError
    transport.assert_not_called()


@pytest.mark.asyncio
async def test_demo_is_not_in_business_discovery(monkeypatch):
    monkeypatch.setattr(marketplace_proxy, 'list_tools', AsyncMock(return_value=[]))
    monkeypatch.setattr('src.mcp.mangrove_proxy.catalog', AsyncMock(return_value=[]))
    server = PricedFastMCP('diagnostics')

    @server.tool()
    def hello_mangrove() -> str:
        return 'diagnostic'

    assert await server.list_tools() == []
    assert 'hello_mangrove' in server._tool_manager._tools


def test_removed_wrappers_are_not_registered():
    from mcp.server.fastmcp import FastMCP

    server = FastMCP('local-boundaries')
    tools._register_wallet(server)
    tools._register_dex(server)
    names = set(server._tool_manager._tools)
    assert {'create_wallet', 'import_wallet', 'list_wallets', 'execute_swap',
            'cex_status', 'cex_connect_kraken', 'cex_balances'} <= names
    assert not names & {'get_balances', 'get_swap_quote', 'portfolio_value', 'portfolio_pnl',
                        'portfolio_tokens', 'portfolio_defi', 'portfolio_history',
                        'get_dex_chart', 'get_token_info', 'list_dex_venues', 'get_tx_status',
                        'get_allowances', 'get_token_search', 'get_spot_price', 'get_gas_price'}


@pytest.mark.asyncio
async def test_retired_names_never_fall_through_to_another_server(monkeypatch):
    upstream = AsyncMock(side_effect=AssertionError('retired names must not dispatch'))
    monkeypatch.setattr('src.mcp.mangrove_proxy.call_tool', upstream)
    monkeypatch.setattr(marketplace_proxy, 'call_tool', upstream)
    server = PricedFastMCP('retirement')
    for name in catalog.RETIRED_READ_TOOLS:
        response = await server.call_tool(name, {})
        assert response.isError
        assert response.structuredContent['code'] == 'RETIRED_TOOL'
    upstream.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize('denial', [None, 401, 403, 'challenge'])
async def test_http_read_never_signs_or_invents_quota_metadata(monkeypatch, remote, denial):
    monkeypatch.setattr(reads, '_api_key', lambda _: 'upstream-secret')
    payer = AsyncMock(side_effect=AssertionError('must not sign'))
    monkeypatch.setattr(reads.x402_payer, 'pay_mcp', payer)
    original = httpx.AsyncClient
    calls = []
    challenge = {'x402Version': 2, 'accepts': []}

    def handle(request):
        assert str(request.url) == 'https://markets.test/mcp/'
        assert request.headers['authorization'] == 'Bearer upstream-secret'
        if denial in {401, 403}:
            return httpx.Response(denial, text='PRIVATE_PROVIDER_BODY')
        if request.method != 'POST':
            return httpx.Response(405)
        message = json.loads(request.content)
        if 'id' not in message:
            return httpx.Response(202)
        if message['method'] == 'initialize':
            result = {'protocolVersion': message['params']['protocolVersion'],
                      'capabilities': {'tools': {}}, 'serverInfo': {'name': 'markets', 'version': '1'}}
        elif message['method'] == 'tools/list':
            result = {'tools': [remote.model_dump(by_alias=True, exclude_none=True)]}
        else:
            assert message['method'] == 'tools/call'
            calls.append(message['params'])
            assert message['params']['name'] == remote.name
            assert not message['params'].get('_meta')
            result = {'content': [{'type': 'text', 'text': 'native response'}],
                      'structuredContent': challenge if denial else {'balances': {'token': '12'}},
                      'isError': denial == 'challenge', '_meta': {'trace': 'safe-trace'}}
        return httpx.Response(200, json={'jsonrpc': '2.0', 'id': message['id'], 'result': result})

    def client(**kwargs):
        assert kwargs['trust_env'] is False and kwargs['follow_redirects'] is False
        return original(transport=httpx.MockTransport(handle), **kwargs)

    monkeypatch.setattr(httpx, 'AsyncClient', client)
    args = {'wallet': 'public', 'chain_id': 8453}
    if denial in {401, 403}:
        from src.shared.errors import UpstreamAccessError

        with pytest.raises(UpstreamAccessError):
            await reads.read(remote.name, args, tool=remote)
        assert not calls
    else:
        result = await reads.read(remote.name, args, tool=remote)
        assert len(calls) == 1
        assert result.paid is False
        assert result.mcp_result['isError'] is (denial == 'challenge')
        assert result.mcp_result['_meta'] == {'trace': 'safe-trace'}
        assert result.mcp_result['structuredContent'] == (challenge if denial else {'balances': {'token': '12'}})
    payer.assert_not_called()


def test_paid_contract_exposes_existing_wallet_and_recovery_controls(remote):
    remote.meta[catalog.READ_CONTRACT_META]['access'] = 'api_key_or_x402'
    exposed = marketplace_proxy.exposed_tool(remote)
    assert set(exposed.inputSchema['properties']['_agent']['properties']) == {
        'api_key', 'wallet_address', 'operation_id',
    }
    assert exposed.inputSchema['properties']['wallet'] == remote.inputSchema['properties']['wallet']


@pytest.mark.asyncio
async def test_dual_access_contract_api_key_never_pays(monkeypatch, remote):
    remote.meta[catalog.READ_CONTRACT_META]['access'] = 'api_key_or_x402'
    monkeypatch.setattr(reads, '_api_key', lambda _: 'configured-key')
    transport = AsyncMock(return_value=PaymentResult(status_code=200, body=[], paid=False))
    monkeypatch.setattr(reads, '_key_read', transport)
    payer = AsyncMock(side_effect=AssertionError('must not pay'))
    monkeypatch.setattr(reads.x402_payer, 'pay_mcp', payer)
    await reads.read(remote.name, {'wallet': 'public', 'chain_id': 8453}, tool=remote)
    assert transport.call_args.args[-1] is None
    payer.assert_not_called()
