"""Official MCP HTTP client and real signing against a controlled receiver."""
import asyncio
import json
import logging
import uuid

import httpx
import pytest
from mcp.client.streamable_http import streamable_http_client

from mcp import ClientSession
from src.services import payment_operations, spend_service, x402_payer
from src.shared.errors import X402PaymentError, X402PaymentUncertain
from tests.integration import test_x402_sync_transport as payment_fixtures

database = payment_fixtures.database
wallet = payment_fixtures.wallet

ORIGIN = 'https://receiver.test/mcp'


@pytest.fixture
def receiver(monkeypatch, wallet):
    state = {'calls': [], 'failure': None, 'closed': False}
    original_client = httpx.AsyncClient

    async def handle(request):
        if request.method != 'POST':
            return httpx.Response(405)
        message = json.loads(request.content)
        if 'id' not in message:
            return httpx.Response(202)
        if message['method'] == 'initialize':
            result = {'protocolVersion': message['params']['protocolVersion'],
                      'capabilities': {'tools': {}}, 'serverInfo': {'name': 'test', 'version': '1'}}
        elif message['method'] == 'tools/list':
            result = {'tools': [{'name': 'list_signals', 'inputSchema': {'type': 'object'}}]}
        else:
            assert message['method'] == 'tools/call'
            params = message['params']
            state['calls'].append(params)
            assert params['name'] == 'list_signals'
            assert params['arguments'] == {'limit': 10}
            assert 'authorization' not in request.headers and 'x-api-key' not in request.headers
            if 'x402/payment' not in params.get('_meta', {}):
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


async def test_official_remote_client_pays_and_closes(receiver, wallet):
    result = await x402_payer.pay_remote_mcp(ORIGIN, name='list_signals', arguments={'limit': 10}, wallet_address=wallet)
    assert result.paid and result.mcp_result['structuredContent']['signals'][0]['name'] == 'example'
    assert len(receiver['calls']) == 2 and receiver['closed']
    assert len(spend_service.list_payments()) == 1


async def test_official_client_recovers_without_new_signature(receiver, wallet):
    receiver['failure'] = 'lost'
    with pytest.raises(X402PaymentUncertain) as caught:
        await x402_payer.pay_remote_mcp(ORIGIN, name='list_signals', arguments={'limit': 10}, wallet_address=wallet)
    assert receiver['closed']
    assert 'SYNTHETIC_PRIVATE' not in str(caught.value)
    signed = receiver['calls'][-1]['_meta']
    receiver['failure'] = None
    result = await x402_payer.pay_remote_mcp(ORIGIN, name='list_signals', arguments={'limit': 10},
                                           wallet_address=wallet, operation_id=caught.value.operation_id)
    assert result.paid and len(receiver['calls']) == 3
    assert receiver['calls'][-1]['_meta'] == signed
    assert len(spend_service.list_payments()) == 1


async def test_overall_deadline_closes_connection_and_retains_payment(receiver, wallet):
    receiver['failure'] = 'timeout'
    with pytest.raises(X402PaymentError):
        await x402_payer.pay_remote_mcp(ORIGIN, name='list_signals', arguments={'limit': 10},
                                       wallet_address=wallet, timeout=0.2)
    assert receiver['closed']
    assert spend_service.list_payments()[0]['state'] == 'authorized'


async def test_sdk_debug_logs_never_disclose_signed_metadata(receiver, wallet, caplog):
    with caplog.at_level(logging.DEBUG, logger='mcp.client.streamable_http'):
        await x402_payer.pay_remote_mcp(ORIGIN, name='list_signals', arguments={'limit': 10}, wallet_address=wallet)
    meta = receiver['calls'][-1]['_meta']
    assert meta['x402/payment']['payload']['signature'] not in caplog.text
    assert meta['mangrove/payment']['recovery_token'] not in caplog.text
    assert 'Sending client message' not in caplog.text
    with caplog.at_level(logging.DEBUG, logger='mcp.client.streamable_http'):
        logging.getLogger('mcp.client.streamable_http').debug('unrelated diagnostics remain available')
    assert 'unrelated diagnostics remain available' in caplog.text


@pytest.mark.parametrize('recover', [False, True])
async def test_direct_session_protects_payment_and_recovery_logs(receiver, wallet, caplog, recover):
    kwargs = dict(name='list_signals', arguments={'limit': 10}, wallet_address=wallet, resource=ORIGIN)
    if recover:
        receiver['failure'] = 'lost'
        with pytest.raises(X402PaymentUncertain) as first:
            await x402_payer.pay_remote_mcp(ORIGIN, name='list_signals', arguments={'limit': 10}, wallet_address=wallet)
        kwargs['operation_id'] = first.value.operation_id
        receiver['failure'] = None
    with caplog.at_level(logging.DEBUG, logger='mcp.client.streamable_http'):
        async with httpx.AsyncClient(timeout=5, trust_env=False, follow_redirects=False) as http:
            async with streamable_http_client(ORIGIN, http_client=http) as (read, write, _):
                async with ClientSession(read, write) as session:
                    result = await x402_payer.pay_mcp(session, **kwargs)
        logger = logging.getLogger('mcp.client.streamable_http')
        logger.debug('unrelated caller-owned session diagnostics')
    assert result.paid
    meta = receiver['calls'][-1]['_meta']
    assert meta['x402/payment']['payload']['signature'] not in caplog.text
    assert meta['mangrove/payment']['recovery_token'] not in caplog.text
    assert 'unrelated caller-owned session diagnostics' in caplog.text
    assert len(spend_service.list_payments()) == 1


@pytest.mark.parametrize('identity', ['original', 'different', 'omitted'])
async def test_recovery_deadline_preserves_resolved_operation(receiver, wallet, identity):
    receiver['failure'] = 'lost'
    kwargs = dict(name='list_signals', arguments={'limit': 10}, wallet_address=wallet)
    with pytest.raises(X402PaymentUncertain) as first:
        await x402_payer.pay_remote_mcp(ORIGIN, **kwargs)
    original = first.value.operation_id
    signed = receiver['calls'][-1]['_meta']
    if identity != 'omitted':
        kwargs['operation_id'] = original if identity == 'original' else str(uuid.uuid4())
    receiver['failure'] = 'timeout'
    with pytest.raises(X402PaymentUncertain) as retry:
        await x402_payer.pay_remote_mcp(ORIGIN, timeout=0.15, **kwargs)
    if identity == 'original':
        assert retry.value.operation_id == original
        assert receiver['calls'][-1]['_meta'] == signed
        assert len(spend_service.list_payments()) == 1
    else:
        assert retry.value.operation_id != original
        assert receiver['calls'][-1]['_meta'] != signed
        assert len(spend_service.list_payments()) == 2
    assert retry.value.reservation_ids == payment_operations.reservation_ids(retry.value.operation_id)
    assert payment_operations.current_attempt.get() is None
    receiver['failure'] = None
    kwargs['operation_id'] = original
    assert (await x402_payer.pay_remote_mcp(ORIGIN, **kwargs)).paid


async def test_concurrent_recovery_deadlines_keep_separate_identities(receiver, wallet):
    endpoints = [ORIGIN, ORIGIN + '/second']
    kwargs = dict(name='list_signals', arguments={'limit': 10}, wallet_address=wallet)
    receiver['failure'] = 'lost'
    original = await asyncio.gather(*(x402_payer.pay_remote_mcp(url, **kwargs) for url in endpoints), return_exceptions=True)
    assert all(isinstance(error, X402PaymentUncertain) for error in original)
    assert original[0].operation_id != original[1].operation_id
    receiver['failure'] = 'timeout'
    recovered = await asyncio.gather(*(x402_payer.pay_remote_mcp(url, timeout=0.15, operation_id=error.operation_id, **kwargs)
                                       for url, error in zip(endpoints, original)), return_exceptions=True)
    assert all(isinstance(error, X402PaymentUncertain) for error in recovered)
    assert [error.operation_id for error in recovered] == [error.operation_id for error in original]
    assert len(spend_service.list_payments()) == 2
    assert payment_operations.current_attempt.get() is None


async def test_completed_remote_result_retains_content_types(receiver, wallet):
    kwargs = dict(name='list_signals', arguments={'limit': 10}, wallet_address=wallet, operation_id=str(uuid.uuid4()))
    first = await x402_payer.pay_remote_mcp(ORIGIN, **kwargs)
    before = len(receiver['calls'])
    cached = await x402_payer.pay_remote_mcp(ORIGIN, **kwargs)
    assert cached.body == first.body
    assert cached.body[0].text == first.body[0].text
    assert cached.mcp_result == first.mcp_result
    assert len(receiver['calls']) == before
    assert len(spend_service.list_payments()) == 1
