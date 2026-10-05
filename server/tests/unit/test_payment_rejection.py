"""Unusable signed authorizations close safely without trusting a server error."""
import base64
import copy
import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from cryptography.fernet import Fernet
from eth_account import Account
from eth_account.messages import encode_typed_data
from x402.mechanisms.evm.eip712 import build_typed_data_for_signing
from x402.mechanisms.evm.types import ExactEIP3009Payload

from src.config import app_config
from src.services import payment_operations as operations
from src.services import spend_service
from src.services.payment_rejection import unspendable_evidence
from src.services.x402_inspection import TOKENS
from src.services.x402_reconciliation import reconcile_unspendable
from src.shared.db import sqlite
from src.shared.errors import ValidationError

from . import test_x402_uncertainty as evidence_fixtures
from .test_x402_uncertainty import NETWORK, NONCE

database = evidence_fixtures.database

ACCOUNT = Account.from_key('0x' + '42' * 32)
ZERO = '0x' + '00' * 20
PAYEE = '0x' + '12' * 20


def authorization(recipient=ZERO, after=0, before=1900000000, network=NETWORK):
    chain = int(network.split(':')[1])
    name = 'USDC' if chain == 84532 else 'USD Coin'
    auth = ExactEIP3009Payload.from_dict({'authorization': {
        'from': ACCOUNT.address, 'to': recipient, 'value': '1000',
        'validAfter': str(after), 'validBefore': str(before), 'nonce': NONCE,
    }}).authorization
    _, types, _, message = build_typed_data_for_signing(auth, chain, TOKENS[network], name, '2')
    signature = ACCOUNT.sign_message(encode_typed_data(
        domain_data={'name': name, 'version': '2', 'chainId': chain, 'verifyingContract': TOKENS[network]},
        message_types=types, message_data=message,
    )).signature
    proof = {'x402Version': 2, 'accepted': {'scheme': 'exact', 'network': network,
             'asset': TOKENS[network], 'amount': '1000', 'payTo': recipient,
             'extra': {'name': name, 'version': '2'}},
             'payload': {'authorization': ExactEIP3009Payload(auth).to_dict()['authorization'], 'signature': '0x' + signature.hex()}}
    row = {'id': 'synthetic', 'state': 'authorized', 'network': network, 'asset': TOKENS[network],
           'wallet_address': ACCOUNT.address, 'payee': recipient, 'authorization_nonce': NONCE,
           'amount_micro_usd': 1000, 'valid_after': after, 'valid_before': before}
    return row, proof


def saved(proof, transport):
    if transport == 'mcp':
        return {'transport': 'mcp', 'meta': {'x402/payment': proof}}
    return {'headers': {'payment-signature': base64.b64encode(json.dumps(proof).encode()).decode()}}


@pytest.mark.parametrize('transport', ['mcp', 'http'])
@pytest.mark.parametrize('network', [NETWORK, 'eip155:8453'])
@pytest.mark.parametrize('recipient,after,before,reason', [
    (ZERO, 0, 1900000000, 'zero_recipient'),
    (PAYEE, 1900000000, 1900000000, 'empty_validity_window'),
    (PAYEE, 1900000001, 1900000000, 'empty_validity_window'),
])
def test_cryptographically_bound_unspendable_proof(transport, network, recipient, after, before, reason):
    row, proof = authorization(recipient, after, before, network)
    evidence = unspendable_evidence(row, saved(proof, transport))
    assert evidence['reason'] == reason
    assert evidence['payment_sent'] is False
    assert 'signature' not in json.dumps(evidence)


@pytest.mark.parametrize('field,value', [
    ('payee', PAYEE), ('wallet_address', PAYEE), ('amount_micro_usd', 2000),
    ('valid_before', 1), ('valid_after', 1), ('authorization_nonce', '0x' + 'ab' * 31 + 'cd'),
    ('asset', PAYEE), ('network', 'eip155:8453'),
])
def test_ledger_mismatch_is_not_evidence(field, value):
    row, proof = authorization()
    row[field] = value
    assert unspendable_evidence(row, saved(proof, 'mcp')) is None


def test_signature_and_protocol_changes_are_not_evidence():
    row, proof = authorization()
    for tamper in ('signature', 'domain', 'version', 'amount'):
        altered = copy.deepcopy(proof)
        if tamper == 'signature':
            altered['payload']['signature'] = '0x' + '00' * 65
        elif tamper == 'domain':
            altered['accepted']['extra']['name'] = 'untrusted'
        elif tamper == 'version':
            altered['x402Version'] = 1
        else:
            altered['accepted']['amount'] = '2000'
        assert unspendable_evidence(row, saved(altered, 'mcp')) is None


def test_decline_or_temporary_error_does_not_invalidate_valid_authorization():
    row, proof = authorization(PAYEE)
    envelope = saved(proof, 'http')
    envelope.update(status=402, error='verification_rejected', retry_payment=False)
    assert unspendable_evidence(row, envelope) is None


@pytest.fixture
def encrypted_database(database, tmp_path, monkeypatch):
    key = tmp_path / 'master.key'
    key.write_bytes(Fernet.generate_key())
    key.chmod(0o600)
    monkeypatch.setattr(app_config, 'MASTER_KEY_PATH', str(key))
    monkeypatch.setattr(app_config, 'X402_RECONCILIATION_RPC_URLS', {})
    return database


def store_attempt(transport='mcp', recipient=ZERO):
    row, proof = authorization(recipient)
    op, _ = operations.begin('same-request')
    rid = spend_service.reserve(value=1000, wallet_address=row['wallet_address'], payee=recipient,
        network=NETWORK, asset=row['asset'], valid_before=row['valid_before'], valid_after=0,
        authorization_nonce=NONCE, operation_id=op['id'])
    operations.save_headers(op['id'], saved(proof, transport))
    return op, rid


@pytest.mark.parametrize('transport', ['mcp', 'http'])
def test_next_request_closes_unspendable_attempt_without_rpc(encrypted_database, transport):
    op, rid = store_attempt(transport)
    retry, created = operations.begin('same-request')
    assert created and retry['id'] != op['id']
    payment = sqlite.get_connection().execute('SELECT * FROM x402_payments WHERE id=?', (rid,)).fetchone()
    assert payment['state'] == 'released'
    assert payment['release_reason'] == 'unspendable_authorization'
    assert spend_service.get_status()['spent_usd'] == 0
    assert sqlite.get_connection().execute('SELECT COUNT(*) FROM x402_reconciliation_evidence').fetchone()[0] == 1
    with pytest.raises(ValidationError, match='closed'):
        operations.begin('same-request', op['id'])


def test_concurrent_retries_create_one_new_attempt(encrypted_database):
    original, _ = store_attempt()
    with ThreadPoolExecutor(max_workers=6) as pool:
        attempts = list(pool.map(lambda _: operations.begin('same-request'), range(6)))
    assert sum(created for _, created in attempts) == 1
    assert len({op['id'] for op, _ in attempts}) == 1
    assert attempts[0][0]['id'] != original['id']
    assert sqlite.get_connection().execute('SELECT COUNT(*) FROM x402_reconciliation_evidence').fetchone()[0] == 1


def test_valid_pending_proof_is_preserved_and_other_request_can_proceed(encrypted_database):
    original, rid = store_attempt(recipient=PAYEE)
    assert reconcile_unspendable(rid) is None
    retry, created = operations.begin('same-request')
    assert not created and retry['id'] == original['id']
    other, created = operations.begin('different-request')
    assert created and other['id'] != original['id']
    assert spend_service.get_status()['spent_usd'] == .001


def test_missing_saved_proof_never_releases_from_metadata_alone(encrypted_database):
    original, rid = store_attempt()
    with spend_service._budget_transaction() as conn:
        conn.execute('UPDATE x402_operations SET payment_headers=NULL WHERE id=?', (original['id'],))
    assert reconcile_unspendable(rid) is None
    assert spend_service.get_status()['spent_usd'] == .001


def test_background_worker_closes_unspendable_without_contacting_rpc(encrypted_database):
    from src.services import payment_reconciliation_worker as worker
    _, rid = store_attempt()
    assert worker.run_once(urls={NETWORK: 'synthetic'},
        rpc_factory=lambda _: pytest.fail('Unspendable proof should not need RPC')) == 1
    assert spend_service.get_status()['spent_usd'] == 0
    assert sqlite.get_connection().execute(
        'SELECT release_reason FROM x402_payments WHERE id=?', (rid,)).fetchone()[0] == 'unspendable_authorization'


def test_existing_transaction_evidence_is_never_overridden(encrypted_database):
    from .test_x402_uncertainty import TX
    _, rid = store_attempt()
    with spend_service._budget_transaction() as conn:
        conn.execute('UPDATE x402_payments SET transaction_hash=? WHERE id=?', (TX, rid))
    assert reconcile_unspendable(rid) is None
    assert spend_service.get_status()['spent_usd'] == .001


def test_released_attempt_stays_closed_after_restart(encrypted_database):
    original, rid = store_attempt()
    assert reconcile_unspendable(rid)['ledger_changed'] is True
    sqlite.reset_connection()
    assert reconcile_unspendable(rid) is None
    following, created = operations.begin('same-request')
    assert created and following['id'] != original['id']
    assert len(spend_service.list_payments()) == 1


def test_unreadable_saved_proof_keeps_chain_recovery_available(encrypted_database, monkeypatch):
    _, rid = store_attempt()
    def unavailable(*args):
        raise ValueError('SYNTHETIC_PRIVATE_DATA')
    monkeypatch.setattr(operations, 'unseal', unavailable)
    assert reconcile_unspendable(rid) is None
    assert spend_service.get_status()['spent_usd'] == .001
