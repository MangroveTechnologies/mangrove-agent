"""Read-only funding checks for explicitly approved USDC purchases."""
from __future__ import annotations

import re
from contextlib import closing
from decimal import Decimal

from src.services.payment_reconciliation_worker import ReadOnlyRPC, configured_urls
from src.services.x402_inspection import TOKENS, _quantity
from src.shared.errors import AgentError


class PurchaseBalanceUnavailable(AgentError):
    code = 'PURCHASE_BALANCE_UNAVAILABLE'
    http_status = 503


class PurchaseInsufficientFunds(AgentError):
    code = 'PURCHASE_INSUFFICIENT_FUNDS'
    http_status = 409


def require_purchase_balance(wallet: str, terms: dict) -> None:
    try:
        network, asset = terms['network'], terms['asset']
        if (network not in TOKENS or asset.lower() != TOKENS[network]
                or not re.fullmatch(r'0x[0-9a-fA-F]{40}', wallet)
                or not re.fullmatch(r'[1-9][0-9]{0,77}', terms['amount'])):
            raise ValueError('Invalid approved payment')
        required = int(terms['amount'])
        url = configured_urls().get(network)
        if not url:
            raise ValueError('Missing network RPC')
        with closing(ReadOnlyRPC(url)) as rpc:
            if _quantity(rpc('eth_chainId', [])) != int(network.split(':')[1]):
                raise ValueError('Wrong RPC chain')
            block = rpc('eth_getBlockByNumber', ['latest', False])
            if not isinstance(block, dict) or not re.fullmatch(r'0x[0-9a-fA-F]{64}', str(block.get('hash', ''))):
                raise ValueError('Invalid block')
            encoded = rpc('eth_call', [{'to': asset, 'data': '0x70a08231' + wallet[2:].lower().zfill(64)},
                                      {'blockHash': block['hash'], 'requireCanonical': True}])
            if not isinstance(encoded, str) or not re.fullmatch(r'0x[0-9a-fA-F]{64}', encoded):
                raise ValueError('Invalid token balance')
            balance = int(encoded, 16)
    except Exception:
        raise PurchaseBalanceUnavailable('Could not verify the purchase wallet balance. No new payment was signed.') from None
    if balance < required:
        unit = Decimal(1_000_000)
        raise PurchaseInsufficientFunds(
            f'Insufficient USDC: this wallet has {Decimal(balance) / unit:f}, '
            f'the purchase requires {Decimal(required) / unit:f}, '
            f'and the shortfall is {Decimal(required - balance) / unit:f}. No new payment was signed.')
