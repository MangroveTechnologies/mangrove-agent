"""Approved purchases through native MCP, real wallet signatures and the x402 SDK."""
from __future__ import annotations

import hashlib
import json
import time
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from eth_account import Account
from eth_account.messages import encode_defunct
from mcp.types import Tool

from src.config import app_config
from src.services import marketplace, marketplace_catalog, spend_service
from src.services.marketplace_authorization import PREFIX, canonical
from src.shared.errors import AgentError
from tests.integration import test_x402_sync_transport as payment_fixtures

database = payment_fixtures.database
wallet = payment_fixtures.wallet
TERMS = {'scheme': 'exact', 'network': 'eip155:84532', 'asset': payment_fixtures.USDC,
         'amount': '5000000', 'payTo': payment_fixtures.PAYEE}


@pytest.fixture(params=['wallet', 'api-key'])
def purchase_client(request, wallet, monkeypatch):
    api = request.param == 'api-key'
    monkeypatch.setattr(app_config, 'MANGROVE_API_KEY', 'test-key' if api else 'saved-inactive-key')
    monkeypatch.setattr(app_config, 'MANGROVE_ACCESS_MODE', 'api-key' if api else 'x402')
    settings = {'markets': 'https://receiver.test', 'authority': 'https://identity.test' if api else None,
                'audience': 'test-market', 'chain_id': 84532}
    monkeypatch.setattr(marketplace, '_settings', lambda: settings.copy())
    identity = {'version': 1, 'audience': 'mangrovemarkets', 'auth_method': 'api_key',
                'user_id': 'alice', 'org_id': 'org', 'permissions': ['execution:write'], 'cache_ttl_seconds': 0}
    monkeypatch.setattr(marketplace, '_request', lambda *args: identity.copy())
    contracts = json.loads((Path(__file__).parents[1] / 'unit/marketplace_contract_fixture.json').read_text())
    def tool(name):
        return Tool.model_validate(contracts[name])
    async def async_tool(name):
        return tool(name)
    monkeypatch.setattr(marketplace_catalog, 'get_tool_sync', tool)
    monkeypatch.setattr(marketplace_catalog, 'get_tool', async_tool)
    state = {'offers': 0, 'signed_payments': [], 'terms': deepcopy(TERMS), 'lose_response': False, 'operation': 'marketplace_make_offer'}
    from src.services import purchase_balance
    state['balance'] = 10_000_000
    state['balance_reads'] = 0
    class BalanceRPC:
        def __init__(self, url):
            pass
        def __call__(self, method, params):
            if method == 'eth_chainId':
                return hex(84532)
            if method == 'eth_getBlockByNumber':
                return {'hash': '0x' + 'a' * 64}
            assert method == 'eth_call'
            state['balance_reads'] += 1
            balance = state['balance'] if state['offers'] == 0 else state.get('balance_after_offer', state['balance'])
            return '0x' + format(balance, '064x')
        def close(self):
            pass
    monkeypatch.setattr(purchase_balance, 'ReadOnlyRPC', BalanceRPC)
    monkeypatch.setattr(purchase_balance, 'configured_urls', lambda: {TERMS['network']: 'https://rpc.test'})
    intent = {'version': 1, 'protocol': 'x402-mcp', 'tool': 'marketplace_pay_offer',
              'requirements': TERMS, 'result_bindings': {'offer_id': 'offer_id'}}

    def action(name, arguments):
        identity = marketplace._identity(settings, marketplace._wallet(wallet, settings))
        args = {k: v for k, v in arguments.items() if k != 'ownership_proof'}
        proof = arguments.get('ownership_proof')
        fields = ({k: proof[k] for k in ('nonce', 'issued_at', 'expires_at')} if proof else
                  {'nonce': 'a' * 64, 'issued_at': int(time.time()), 'expires_at': int(time.time()) + 300})
        message = {'version': 1, 'audience': 'test-market', 'user_id': identity['user_id'],
                   'org_id': identity['org_id'], 'credential_type': identity['auth_method'],
                   'operation': name, 'arguments_sha256': hashlib.sha256(canonical(args).encode()).hexdigest(),
                   'chain': 'base', 'network': TERMS['network'], 'address': wallet, **fields}
        challenge = {'code': 'OWNERSHIP_REQUIRED', 'error': True, 'chain': 'base', 'address': wallet,
                     'ownership_proof': fields, 'authorization': PREFIX + canonical(message)}
        if proof:
            assert Account.recover_message(encode_defunct(text=challenge['authorization']),
                                           signature=proof['signature']) == wallet
            return None
        return challenge

    def prepare_or_create(settings, name, args):
        assert name == state['operation']
        challenge = action(name, args)
        if challenge:
            return {**challenge, 'payment_intent': deepcopy(intent)}
        if name == 'marketplace_pay_offer':
            return {'x402Version': 2, 'accepts': [TERMS]}
        state['offers'] += 1
        return {'code': 'PAYMENT_REQUIRED', 'error': True, 'offer_id': 'offer-one'}
    monkeypatch.setattr(marketplace, '_markets_call', prepare_or_create)
    original_client = httpx.AsyncClient

    async def handle(req):
        assert req.url == 'https://receiver.test/mcp/'
        assert req.headers.get('authorization') == ('Bearer test-key' if api else None)
        if req.method != 'POST':
            return httpx.Response(405)
        message = json.loads(req.content)
        if 'id' not in message:
            return httpx.Response(202)
        if message['method'] == 'initialize':
            result = {'protocolVersion': message['params']['protocolVersion'], 'capabilities': {'tools': {}},
                      'serverInfo': {'name': 'test-markets', 'version': '1'}}
        elif message['method'] == 'tools/list':
            result = {'tools': [contracts['marketplace_pay_offer']]}
        else:
            assert message['method'] == 'tools/call'
            params = message['params']
            assert params['name'] == 'marketplace_pay_offer'
            assert params['arguments']['offer_id'] == 'offer-one'
            challenge = action(params['name'], params['arguments'])
            if challenge:
                result = {'isError': True, 'content': [{'type': 'text', 'text': json.dumps(challenge)}]}
            elif 'x402/payment' not in params.get('_meta', {}):
                quote = {'x402Version': 2, 'accepts': [{**state['terms'], 'maxTimeoutSeconds': 300,
                                                     'extra': {'name': 'USDC', 'version': '2'}}]}
                result = {'isError': True, 'content': [{'type': 'text', 'text': json.dumps(quote)}],
                          'structuredContent': quote, '_meta': {'mangrove/payment': {'idempotency': 'v1'}}}
            else:
                proof = params['_meta']['x402/payment']
                assert proof['payload']['authorization']['from'].lower() == wallet.lower()
                state['signed_payments'].append(proof)
                if state['lose_response']:
                    raise httpx.ReadError('Synthetic lost response')
                receipt = {'success': True, 'transaction': '0x' + 'ab' * 32, 'network': TERMS['network'], 'payer': wallet}
                body = {'offer_id': 'offer-one', 'status': 'accepted', 'settlement': receipt}
                result = {'isError': False, 'content': [{'type': 'text', 'text': json.dumps(body)}],
                          'structuredContent': body, '_meta': {'x402/payment-response': receipt}}
        return httpx.Response(200, json={'jsonrpc': '2.0', 'id': message['id'], 'result': result})

    def client(**kwargs):
        return original_client(transport=httpx.MockTransport(handle), **kwargs)
    monkeypatch.setattr(httpx, 'AsyncClient', client)
    preview = marketplace.prepare('marketplace_make_offer', {'listing_id': 'listing-one',
                                      'expected_revision': 'a' * 64}, wallet)
    return SimpleNamespace(preview=preview, state=state, wallet=wallet)


def test_confirmed_purchase_automatically_pays_once(purchase_client):
    env = purchase_client
    assert env.preview['payment']['requirements'] == TERMS
    assert not env.state['signed_payments'] and env.state['offers'] == 0
    with pytest.raises(marketplace.MarketplaceError):
        marketplace.submit(env.preview['approval_id'])
    result = marketplace.submit(env.preview['approval_id'], confirm=True)
    assert result['status'] == 'accepted'
    assert marketplace.submit(env.preview['approval_id'], confirm=True) == result
    assert env.state['offers'] == 1
    assert len(env.state['signed_payments']) == 1
    assert len(spend_service.list_payments()) == 1


@pytest.mark.parametrize('field,value', [('amount', '6000000'), ('amount', '4000000'), ('payTo', '0x' + '3' * 40),
    ('asset', '0x' + '4' * 40), ('network', 'eip155:8453')])
def test_changed_payment_terms_never_signed(purchase_client, field, value):
    env = purchase_client
    env.state['terms'][field] = value
    with pytest.raises(AgentError):
        marketplace.submit(env.preview['approval_id'], confirm=True)
    assert not env.state['signed_payments']
    assert not spend_service.list_payments()


def test_lost_response_reuses_original_payment_and_offer(purchase_client):
    env = purchase_client
    env.state['lose_response'] = True
    with pytest.raises(AgentError):
        marketplace.submit(env.preview['approval_id'], confirm=True)
    assert len(spend_service.list_payments()) == 1
    env.state['lose_response'] = False
    reads = env.state['balance_reads']
    env.state['balance'] = 0
    assert marketplace.submit(env.preview['approval_id'], confirm=True)['status'] == 'accepted'
    assert env.state['offers'] == 1
    assert env.state['balance_reads'] == reads
    assert env.state['signed_payments'][0] == env.state['signed_payments'][-1]
    assert len(spend_service.list_payments()) == 1


def test_budget_cannot_be_overridden_by_purchase_approval(purchase_client):
    env = purchase_client
    spend_service.reset(cap_usd=1)
    with pytest.raises(AgentError):
        marketplace.submit(env.preview['approval_id'], confirm=True)
    assert not env.state['signed_payments']


def test_existing_offer_can_be_reviewed_and_paid_without_recreating_it(purchase_client):
    env = purchase_client
    env.state['operation'] = 'marketplace_pay_offer'
    preview = marketplace.prepare('marketplace_pay_offer', {'offer_id': 'offer-one'}, env.wallet)
    result = marketplace.submit(preview['approval_id'], confirm=True)
    assert result['status'] == 'accepted'
    assert env.state['offers'] == 0
    assert len(env.state['signed_payments']) == 1


def test_insufficient_balance_creates_no_offer_or_payment(purchase_client):
    from src.services.purchase_balance import PurchaseInsufficientFunds
    env = purchase_client
    env.state['balance'] = 511000
    with pytest.raises(PurchaseInsufficientFunds, match='Insufficient USDC'):
        marketplace.submit(env.preview['approval_id'], confirm=True)
    assert env.state['offers'] == 0
    assert not env.state['signed_payments']
    assert not spend_service.list_payments()
    env.state['balance'] = 5_000_000
    assert marketplace.submit(env.preview['approval_id'], confirm=True)['status'] == 'accepted'
    assert env.state['offers'] == 1


def test_balance_drop_after_offer_retries_same_offer(purchase_client):
    from src.services.purchase_balance import PurchaseInsufficientFunds
    env = purchase_client
    env.state['balance_after_offer'] = 0
    with pytest.raises(PurchaseInsufficientFunds):
        marketplace.submit(env.preview['approval_id'], confirm=True)
    assert env.state['offers'] == 1
    assert not env.state['signed_payments']
    assert not spend_service.list_payments()
    env.state['balance_after_offer'] = 5_000_000
    assert marketplace.submit(env.preview['approval_id'], confirm=True)['status'] == 'accepted'
    assert env.state['offers'] == 1
    assert len(env.state['signed_payments']) == 1
