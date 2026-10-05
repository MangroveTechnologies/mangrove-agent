"""Local proof of an authorization that cannot transfer supported USDC."""
from __future__ import annotations

import base64
import hashlib
import json
import re

from eth_account import Account
from eth_account.messages import encode_typed_data
from eth_keys.exceptions import BadSignature
from x402.mechanisms.evm.eip712 import build_typed_data_for_signing
from x402.mechanisms.evm.types import ExactEIP3009Payload

from src.services.x402_inspection import TOKENS


def unspendable_evidence(row: dict, saved: dict) -> dict | None:
    """Require the original signed v2 proof to match every ledger binding."""
    try:
        if saved.get('transport') == 'mcp':
            proof = saved['meta']['x402/payment']
        else:
            headers = saved['headers']
            values = [v for k, v in headers.items() if k.lower() in {'payment-signature', 'x-payment'}]
            if len(values) != 1 or not isinstance(values[0], str) or len(values[0]) > 65536:
                return None
            proof = json.loads(base64.b64decode(values[0], validate=True))
        if proof['x402Version'] != 2:
            return None
        accepted, payload = proof['accepted'], proof['payload']
        network = row['network']
        asset = TOKENS.get(network)
        if (asset is None or accepted['scheme'] != 'exact' or accepted['network'] != network
                or accepted['asset'].lower() != asset or row['asset'].lower() != asset):
            return None
        authorization = ExactEIP3009Payload.from_dict(payload).authorization
        if (authorization.from_address.lower() != row['wallet_address'].lower()
                or authorization.to.lower() != row['payee'].lower()
                or accepted['payTo'].lower() != authorization.to.lower()
                or authorization.nonce.lower() != row['authorization_nonce'].lower()
                or int(authorization.value) != row['amount_micro_usd']
                or int(accepted['amount']) != row['amount_micro_usd']
                or int(authorization.valid_after) != row['valid_after']
                or int(authorization.valid_before) != row['valid_before']):
            return None
        quantities = (authorization.value, authorization.valid_after, authorization.valid_before)
        if any(not 0 <= int(value) < 2 ** 256 for value in quantities):
            return None
        if int(authorization.to, 16) == 0:
            reason = 'zero_recipient'
        elif int(authorization.valid_before) <= int(authorization.valid_after):
            reason = 'empty_validity_window'
        else:
            return None
        chain = int(network.split(':')[1])
        name = {8453: 'USD Coin', 84532: 'USDC'}[chain]
        if accepted.get('extra', {}).get('name') != name or accepted['extra'].get('version') != '2':
            return None
        signature = payload['signature']
        if not isinstance(signature, str) or not re.fullmatch(r'0x[0-9a-fA-F]{130}', signature):
            return None
        _, types, _, message = build_typed_data_for_signing(authorization, chain, asset, name, '2')
        signable = encode_typed_data(
            domain_data={'name': name, 'version': '2', 'chainId': chain, 'verifyingContract': asset},
            message_types=types, message_data=message,
        )
        if Account.recover_message(signable, signature=signature).lower() != row['wallet_address'].lower():
            return None
        digest = hashlib.sha256(json.dumps(proof, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        return {'reservation_id': row['id'], 'ledger_state': row['state'],
                'ledger_changed': False, 'payment_sent': False,
                'outcome': 'unspendable_authorization', 'reason': reason, 'proof_sha256': digest}
    except (KeyError, TypeError, ValueError, AttributeError, OverflowError, BadSignature):
        return None
