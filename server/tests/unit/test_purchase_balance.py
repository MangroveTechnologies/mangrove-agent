from copy import deepcopy

import pytest

from src.services import purchase_balance as balance

WALLET = '0x' + '1' * 40
NETWORK = 'eip155:84532'
TERMS = {'network': NETWORK, 'asset': balance.TOKENS[NETWORK], 'amount': '750000'}
BLOCK = '0x' + 'a' * 64


@pytest.fixture
def rpc(monkeypatch):
    state = {'chain': hex(84532), 'block': {'hash': BLOCK}, 'balance': '0x' + format(750000, '064x'),
             'calls': [], 'closed': False}
    class RPC:
        def __init__(self, url):
            assert url == 'https://rpc.test'
        def __call__(self, method, params):
            state['calls'].append((method, params))
            value = state[{'eth_chainId': 'chain', 'eth_getBlockByNumber': 'block', 'eth_call': 'balance'}[method]]
            if isinstance(value, Exception):
                raise value
            return value
        def close(self):
            state['closed'] = True
    monkeypatch.setattr(balance, 'ReadOnlyRPC', RPC)
    monkeypatch.setattr(balance, 'configured_urls', lambda: {NETWORK: 'https://rpc.test'})
    return state


def test_exact_balance_checks_configured_chain_and_canonical_block(rpc):
    balance.require_purchase_balance(WALLET, TERMS)
    assert rpc['closed']
    assert rpc['calls'] == [('eth_chainId', []), ('eth_getBlockByNumber', ['latest', False]),
                           ('eth_call', [{'to': TERMS['asset'], 'data': '0x70a08231' + WALLET[2:].zfill(64)},
                                         {'blockHash': BLOCK, 'requireCanonical': True}])]


def test_shortfall_is_clear(rpc):
    rpc['balance'] = '0x' + format(511000, '064x')
    with pytest.raises(balance.PurchaseInsufficientFunds, match=r'has 0.511.*requires 0.75.*shortfall is 0.239'):
        balance.require_purchase_balance(WALLET, TERMS)
    assert rpc['closed']


@pytest.mark.parametrize('field,value', [('chain', hex(8453)), ('block', None), ('block', {'hash': 'bad'}),
    ('balance', '0x'), ('balance', None), ('balance', TimeoutError('private RPC details'))])
def test_unverifiable_balance_fails_closed(rpc, field, value):
    rpc[field] = value
    with pytest.raises(balance.PurchaseBalanceUnavailable) as error:
        balance.require_purchase_balance(WALLET, TERMS)
    assert 'private' not in str(error.value)
    assert rpc['closed']


@pytest.mark.parametrize('field,value', [('asset', WALLET), ('network', 'eip155:1'), ('amount', '-1'),
                                        ('amount', '1.5'), ('amount', '0')])
def test_invalid_terms_never_contact_rpc(rpc, field, value):
    terms = deepcopy(TERMS)
    terms[field] = value
    with pytest.raises(balance.PurchaseBalanceUnavailable):
        balance.require_purchase_balance(WALLET, terms)
    assert not rpc['calls']


def test_missing_rpc_fails_closed(rpc, monkeypatch):
    monkeypatch.setattr(balance, 'configured_urls', lambda: {})
    with pytest.raises(balance.PurchaseBalanceUnavailable):
        balance.require_purchase_balance(WALLET, TERMS)
    assert not rpc['calls']


@pytest.mark.anyio
async def test_submit_requires_inline_user_interaction():
    from mcp.server.fastmcp import FastMCP
    from src.mcp.marketplace_tools import register_marketplace
    server = FastMCP('test')
    register_marketplace(server)
    tools = {tool.name: tool for tool in await server.list_tools()}
    assert tools['marketplace_submit'].meta['anthropic/requiresUserInteraction'] is True
    assert 'does not send payments' not in tools['marketplace_submit'].description
    assert not (tools['marketplace_prepare'].meta or {}).get('anthropic/requiresUserInteraction')
