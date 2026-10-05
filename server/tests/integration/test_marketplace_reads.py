"""Official MCP HTTP client and real signing against a controlled receiver."""
import asyncio
import json
import uuid

import httpx
import pytest

from src.config import app_config
from src.services import marketplace_reads, spend_service, x402_payer
from src.shared.errors import UpstreamAccessError, X402PaymentUncertain
from tests.integration import test_x402_sync_transport as payment_fixtures

database = payment_fixtures.database
wallet = payment_fixtures.wallet

ORIGIN = 'https://receiver.test/mcp'


@pytest.fixture
def receiver(monkeypatch, wallet):
    monkeypatch.setattr(app_config, 'MANGROVE_API_KEY', None)
    monkeypatch.setattr(app_config, 'MANGROVEMARKETS_BASE_URL', 'https://receiver.test')
    state = {'calls': [], 'failure': None, 'closed': False, 'requests': []}
    original_client = httpx.AsyncClient

    async def handle(request):
        state['requests'].append(request)
        assert request.url.host == 'receiver.test'
        if app_config.MANGROVE_API_KEY:
            assert request.headers['authorization'] == 'Bearer ' + app_config.MANGROVE_API_KEY
        else:
            assert 'authorization' not in request.headers
        if state['failure'] in {401, 403}:
            return httpx.Response(state['failure'], text='PRIVATE_KEY_DETAILS')
        if request.method != 'POST':
            return httpx.Response(405)
        message = json.loads(request.content)
        if 'id' not in message:
            return httpx.Response(202)
        if message['method'] == 'initialize':
            result = {'protocolVersion': message['params']['protocolVersion'],
                      'capabilities': {'tools': {}}, 'serverInfo': {'name': 'test', 'version': '1'}}
        elif message['method'] == 'tools/list':
            result = {'tools': [{'name': 'marketplace_search', 'inputSchema': {'type': 'object', 'properties': {'limit': {'type': 'integer', 'default': 10}}, 'additionalProperties': False}, '_meta': {'mangrove/marketplace': {'version': 1, 'mode': 'read'}}}]}
        else:
            assert message['method'] == 'tools/call'
            params = message['params']
            state['calls'].append(params)
            assert params['name'] == 'marketplace_search'
            assert params['arguments']['limit'] == 10
            if state['failure'] == 'quota':
                result = {'isError': True, 'structuredContent': {'code': 'QUOTA_EXCEEDED'},
                          'content': [{'type': 'text', 'text': '{}'}]}
            elif app_config.MANGROVE_API_KEY and state['failure'] != 'challenge':
                assert uuid.UUID(params['_meta']['mangrove/quota']['operation_id'])
                result = {'isError': False, 'structuredContent': {'listings': ['one']},
                          'content': [{'type': 'text', 'text': '{}'}]}
            elif 'x402/payment' not in params.get('_meta', {}):
                quote = {'x402Version': 2, 'accepts': [{'scheme': 'exact', 'network': 'eip155:84532',
                    'asset': '0x036CbD53842c5426634e7929541eC2318f3dCF7e', 'amount': '1000',
                    'payTo': '0xde991861bB3e7078015826Fad749de398F6ec1f6', 'maxTimeoutSeconds': 300,
                    'extra': {'name': 'USDC', 'version': '2'}}]}
                result = {'isError': True, 'structuredContent': quote,
                          'content': [{'type': 'text', 'text': json.dumps(quote)}],
                          '_meta': {'mangrove/payment': {'idempotency': 'v1'}}}
            else:
                if state['failure'] == 'timeout':
                    await asyncio.sleep(10)
                if state['failure'] == 'lost':
                    raise httpx.ReadError('SYNTHETIC_PRIVATE')
                proof = params['_meta']['x402/payment']
                assert proof['payload']['authorization']['from'].lower() == wallet.lower()
                assert len(params['_meta']['mangrove/payment']['recovery_token']) >= 43
                result = {'isError': False, 'structuredContent': {'signals': [{'name': 'example'}]},
                    'content': [{'type': 'text', 'text': '{"signals":[{"name":"example"}]}'}],
                    '_meta': {'x402/payment-response': {'success': True, 'transaction': '0x' + 'ab' * 32,
                              'payer': wallet, 'network': 'eip155:84532'}}}
        return httpx.Response(200, json={'jsonrpc': '2.0', 'id': message['id'], 'result': result})

    class Transport(httpx.MockTransport):
        async def aclose(self):
            state['closed'] = True
            await super().aclose()

    def client(**kwargs):
        assert kwargs['trust_env'] is False and kwargs['follow_redirects'] is False
        return original_client(transport=Transport(handle), **kwargs)

    monkeypatch.setattr(httpx, 'AsyncClient', client)
    return state



async def test_wallet_read_uses_generic_payer(receiver, wallet):
    result = await marketplace_reads.read('marketplace_search', {'limit': 10}, wallet_address=wallet)
    assert result.paid and result.mcp_result['structuredContent']
    assert len(receiver['calls']) == 2 and receiver['closed']
    assert len(spend_service.list_payments()) == 1
    assert 'payment' not in receiver['calls'][1]['arguments']


async def test_response_loss_reuses_original_authorization(receiver, wallet):
    receiver['failure'] = 'lost'
    with pytest.raises(X402PaymentUncertain) as caught:
        await marketplace_reads.read('marketplace_search', {'limit': 10}, wallet_address=wallet)
    proof = receiver['calls'][-1]['_meta']
    receiver['failure'] = None
    result = await marketplace_reads.read('marketplace_search', {'limit': 10}, wallet_address=wallet,
                                         operation_id=caught.value.operation_id)
    assert result.paid and receiver['calls'][-1]['_meta'] == proof
    assert len(receiver['calls']) == 3
    assert len(spend_service.list_payments()) == 1


@pytest.mark.parametrize('status', [401, 403])
async def test_auth_failure_never_signs(receiver, wallet, status, monkeypatch):
    monkeypatch.setattr(app_config, "MANGROVE_API_KEY", "synthetic-markets-key")
    receiver['failure'] = status
    with pytest.raises(UpstreamAccessError) as caught:
        await marketplace_reads.read('marketplace_search', {'limit': 10}, wallet_address=wallet)
    assert caught.value.http_status == status
    assert 'PRIVATE_KEY_DETAILS' not in str(caught.value)
    assert spend_service.list_payments() == [] and receiver['calls'] == []


async def test_key_mode_ignores_wallet_and_does_not_replay_wallet_cache(receiver, wallet, monkeypatch):
    oid = str(uuid.uuid4())
    assert (await marketplace_reads.read('marketplace_search', {'limit': 10}, wallet_address=wallet, operation_id=oid)).paid
    monkeypatch.setattr(app_config, 'MANGROVE_API_KEY', 'configured-key')
    result = await marketplace_reads.read('marketplace_search', {'limit': 10}, wallet_address='not-a-wallet', operation_id=oid)
    assert not result.paid and result.mcp_result['structuredContent'] == {'listings': ['one']}
    assert len(receiver['calls']) == 3 and len(spend_service.list_payments()) == 1


@pytest.mark.parametrize('endpoint', ['http://external.test', 'https://user:secret@receiver.test',
                                    'https://receiver.test?token=secret'])
async def test_unsafe_config_never_connects(receiver, wallet, monkeypatch, endpoint):
    monkeypatch.setattr(app_config, 'MANGROVEMARKETS_BASE_URL', endpoint)
    with pytest.raises(marketplace_reads.MarketplaceError):
        await marketplace_reads.read('marketplace_search', {'limit': 10}, wallet_address=wallet)
    assert receiver['requests'] == []


@pytest.mark.parametrize('operation,arguments', [('marketplace_make_offer', {}),
                                                ('marketplace_search', {'payment': 'proof'})])
async def test_unsupported_call_never_connects(receiver, wallet, operation, arguments):
    with pytest.raises(marketplace_reads.MarketplaceError):
        await marketplace_reads.read(operation, arguments, wallet_address=wallet)
    assert receiver['calls'] == []
    assert spend_service.list_payments() == []
    if operation == 'marketplace_search':
        assert receiver['requests'] == []


@pytest.mark.parametrize('failure', [None, 'challenge', 'quota'])
async def test_key_mode_never_uses_wallet_or_payment_ledger(receiver, monkeypatch, failure):
    monkeypatch.setattr(app_config, 'MANGROVE_API_KEY', 'configured-key')
    receiver['failure'] = failure

    def forbidden(*args, **kwargs):
        raise AssertionError('Key mode must not access custody or payment machinery')

    monkeypatch.setattr(x402_payer, 'pay_mcp', forbidden)
    monkeypatch.setattr(x402_payer, 'resolve_payer_wallet', forbidden)
    monkeypatch.setattr('src.shared.crypto.fernet.require_existing_master_key', forbidden)
    monkeypatch.setattr('src.services.payment_operations.reservation_ids', forbidden)
    result = await marketplace_reads.read('marketplace_search', {'limit': 10})
    assert not result.paid and result.mcp_result['isError'] == (failure is not None)
    assert len(receiver['calls']) == 1 and receiver['closed']
    assert spend_service.list_payments() == []


async def test_marketplace_tool_auth_and_read_wiring(monkeypatch):
    from types import SimpleNamespace
    from mcp.types import Tool
    import src.mcp.tools as tools
    from src.mcp import marketplace_proxy
    from src.services import marketplace_catalog

    monkeypatch.setattr(tools, '_require', lambda key: key == 'local-key')
    calls = []

    async def discover(name):
        return Tool(name=name, inputSchema={
            'type': 'object', 'properties': {'listing_id': {'type': 'string'}},
            'required': ['listing_id'], 'additionalProperties': False,
        }, _meta={'mangrove/marketplace': {'version': 1, 'mode': 'read'}})

    async def read(operation, arguments, **kwargs):
        calls.append((operation, arguments, kwargs))
        return SimpleNamespace(paid=True, transaction='receipt', mcp_result={
            'content': [{'type': 'text', 'text': '{"listing":"one"}'}],
            'structuredContent': {'listing': 'one'},
        })

    monkeypatch.setattr(marketplace_catalog, 'get_tool', discover)
    monkeypatch.setattr(marketplace_reads, 'read', read)
    denied = await marketplace_proxy.call_tool('marketplace_get_listing', {'listing_id': 'one'})
    assert denied.isError and denied.structuredContent['code'] == 'AUTH_INVALID_API_KEY'
    assert calls == []
    result = await marketplace_proxy.call_tool('marketplace_get_listing', {
        'listing_id': 'one', '_agent': {'api_key': 'local-key', 'operation_id': 'operation'},
    })
    assert result.meta['mangrove/agent']['paid']
    assert result.structuredContent['listing'] == 'one'
    assert calls[0][0:2] == ('marketplace_get_listing', {'listing_id': 'one'})
    assert calls[0][2]['wallet_address'] is None
    assert calls[0][2]['operation_id'] == 'operation'
    assert calls[0][2]['tool'].name == 'marketplace_get_listing'


async def test_normal_search_tool_key_mode_needs_no_wallet(receiver, monkeypatch):
    import src.mcp.tools as tools
    from src.mcp import marketplace_proxy

    monkeypatch.setattr(app_config, 'MANGROVE_API_KEY', 'configured-key')
    monkeypatch.setattr(tools, '_require', lambda key: key == 'local-key')

    def forbidden(*args, **kwargs):
        raise AssertionError('API-key tool must not enter the payer')

    monkeypatch.setattr(x402_payer, 'pay_mcp', forbidden)
    result = await marketplace_proxy.call_tool('marketplace_search', {
        'limit': 10, '_agent': {'api_key': 'local-key'},
    })
    assert result.meta['mangrove/agent']['paid'] is False
    assert result.structuredContent['listings'] == ['one']
    assert len(receiver['calls']) == 1


async def test_key_quota_operation_id_is_sent_and_returned(receiver, monkeypatch):
    monkeypatch.setattr(app_config, 'MANGROVE_API_KEY', 'upstream-key')
    operation_id = str(uuid.uuid4())
    for _ in range(2):
        result = await marketplace_reads.read('marketplace_search', {'limit': 10}, operation_id=operation_id)
        assert result.mcp_result['_meta']['mangrove/quota']['operation_id'] == operation_id
        assert receiver['calls'][-1]['_meta'] == {'mangrove/quota': {'operation_id': operation_id}}
    result = await marketplace_reads.read('marketplace_search', {'limit': 10})
    assert result.mcp_result['_meta']['mangrove/quota']['operation_id'] != operation_id


async def test_key_quota_invalid_operation_id_rejected_before_request(receiver, monkeypatch):
    monkeypatch.setattr(app_config, 'MANGROVE_API_KEY', 'upstream-key')
    with pytest.raises(marketplace_reads.MarketplaceError, match='UUID'):
        await marketplace_reads.read('marketplace_search', {'limit': 10}, operation_id='invalid')
    assert receiver['requests'] == []


async def test_key_quota_connection_failure_preserves_recovery_id(receiver, monkeypatch, caplog):
    monkeypatch.setattr(app_config, 'MANGROVE_API_KEY', 'upstream-key')
    async def lost(*args, **kwargs):
        raise httpx.ReadError('private details')
    monkeypatch.setattr(marketplace_reads.ClientSession, 'call_tool', lost)
    operation_id = str(uuid.uuid4())
    with pytest.raises(marketplace_reads.MarketplaceError) as caught:
        await marketplace_reads.read('marketplace_search', {'limit': 10}, operation_id=operation_id)
    assert operation_id in caught.value.suggestion
    assert 'private details' not in str(caught.value)

    assert operation_id in caplog.text
    assert "stage=call_tool error_type=ReadError" in caplog.text
    assert "private details" not in caplog.text
    assert "upstream-key" not in caplog.text


async def test_background_recovery_releases_proven_unused_authorization(receiver, wallet, monkeypatch):
    from src.services import payment_reconciliation_worker as worker
    from src.shared.db import sqlite
    from tests.unit.test_x402_uncertainty import RPC

    receiver['failure'] = 'lost'
    with pytest.raises(X402PaymentUncertain):
        await marketplace_reads.read('marketplace_search', {'limit': 10}, wallet_address=wallet)
    row = sqlite.get_connection().execute('SELECT * FROM x402_payments').fetchone()
    original = receiver['calls'][-1]['_meta']['x402/payment']
    monkeypatch.setattr(worker, 'configured_urls', lambda: {'eip155:84532': 'synthetic'})
    run = worker.run_once
    monkeypatch.setattr(worker, 'run_once', lambda **kwargs: run(
        rpc_factory=lambda _: RPC(timestamp=row['valid_before'] + 1), **kwargs))
    assert worker.run_once() == 1
    receiver['failure'] = None
    result = await marketplace_reads.read('marketplace_search', {'limit': 10}, wallet_address=wallet)
    assert result.paid
    assert receiver['calls'][-1]['_meta']['x402/payment'] != original
    assert sorted(p['state'] for p in spend_service.list_payments()) == ['released', 'settled']
    assert spend_service.get_status()['spent_usd'] == .001


async def test_fresh_search_can_pay_while_identical_old_search_is_uncertain(receiver, wallet):
    receiver['failure'] = 'lost'
    with pytest.raises(X402PaymentUncertain) as first:
        await marketplace_reads.read('marketplace_search', {'limit': 10}, wallet_address=wallet)
    old_proof = receiver['calls'][-1]['_meta']['x402/payment']
    receiver['failure'] = None
    result = await marketplace_reads.read('marketplace_search', {'limit': 10}, wallet_address=wallet)
    assert result.paid
    assert receiver['calls'][-1]['_meta']['x402/payment'] != old_proof
    assert sorted(p['state'] for p in spend_service.list_payments()) == ['authorized', 'settled']
    status = spend_service.get_status()
    assert status['settled_usd'] == .001
    assert status['reserved_usd'] == .001
    recovered = await marketplace_reads.read('marketplace_search', {'limit': 10}, wallet_address=wallet,
                                            operation_id=first.value.operation_id)
    assert recovered.paid
    assert receiver['calls'][-1]['_meta']['x402/payment'] == old_proof
    assert len(spend_service.list_payments()) == 2
