"""Refund credits require exact finalized transfers; original charges survive."""
import json

import pytest

from src.config import app_config
from src.services import payment_operations, refund_service, spend_service
from src.services.x402_inspection import TOKENS
from src.shared.db import sqlite
from tests.unit import test_x402_uncertainty as fixtures
from tests.unit.test_x402_uncertainty import (
    NETWORK,
    PAYEE,
    PAYER,
    RPC,
    TX,
    reserve,
)

database = fixtures.database

REFUND = '0x' + '98' * 32


def seed():
    rid = reserve()
    spend_service.reconcile([rid], status_code=400,
        settlement={'success': True, 'transaction': TX, 'network': NETWORK, 'payer': PAYER})
    with spend_service._budget_transaction() as conn:
        conn.execute('''INSERT INTO x402_refunds
            (reservation_id, operation_id, network, amount_micro_usd, updated_at)
            VALUES (?, 'synthetic', ?, 1000, ?)''', (rid, NETWORK, spend_service._now()))
    rpc = RPC()
    rpc.receipt['transactionHash'] = REFUND
    rpc.receipt['logs'] = [rpc.receipt['logs'][1]]
    topics = rpc.receipt['logs'][0]['topics']
    topics[1], topics[2] = topics[2], topics[1]
    def call(method, params):
        if method == 'eth_getTransactionReceipt':
            assert params == [REFUND]
            return rpc.receipt
        return rpc(method, params)
    status = {'state': 'confirmed', 'original_tx': TX, 'refund_tx': REFUND, 'network': NETWORK,
              'asset': TOKENS[NETWORK], 'payer': PAYER, 'receiving_wallet': PAYEE, 'amount_atomic': 1000}
    return rid, rpc, call, status


def test_refund_restores_same_cap_once_and_preserves_charge(database, monkeypatch):
    monkeypatch.setattr(app_config, 'X402_SPEND_CAP_USD', .001)
    rid, _, rpc, status = seed()
    assert spend_service.get_status()['exhausted']
    assert refund_service.apply_confirmed_refund(rid, status, rpc)
    assert not refund_service.apply_confirmed_refund(rid, status, rpc)
    current = spend_service.get_status()
    assert current['spent_usd'] == 0 and current['remaining_usd'] == .001
    assert not current['exhausted'] and current['cap_usd'] == .001
    payment = sqlite.get_connection().execute('SELECT * FROM x402_payments WHERE id = ?', (rid,)).fetchone()
    assert payment['state'] == 'settled' and payment['transaction_hash'] == TX


@pytest.mark.parametrize('field,value', [('payer', PAYEE), ('amount_atomic', 999),
    ('network', 'eip155:8453'), ('asset', PAYER), ('original_tx', REFUND)])
def test_wrong_refund_terms_never_restore_budget(database, field, value):
    rid, _, rpc, status = seed()
    status[field] = value
    with pytest.raises(ValueError):
        refund_service.apply_confirmed_refund(rid, status, rpc)
    assert spend_service.get_status()['spent_usd'] == .001


def test_reorg_or_unconfirmed_transfer_keeps_charge(database):
    rid, fixture, rpc, status = seed()
    fixture.receipt['blockHash'] = '0x' + '77' * 32
    assert not refund_service.apply_confirmed_refund(rid, status, rpc)
    assert spend_service.get_status()['spent_usd'] == .001


def test_old_period_refund_does_not_add_current_budget(database):
    rid, _, rpc, status = seed()
    with spend_service._budget_transaction() as conn:
        conn.execute('UPDATE x402_spend_state SET period_id = period_id + 1')
    reserve()
    assert refund_service.apply_confirmed_refund(rid, status, rpc)
    assert spend_service.get_status()['spent_usd'] == .001


def test_same_transfer_cannot_credit_two_charges(database):
    first, _, rpc, status = seed()
    assert refund_service.apply_confirmed_refund(first, status, rpc)
    second = reserve()
    spend_service.reconcile([second], status_code=400,
        settlement={'success': True, 'transaction': '0x' + '75' * 32, 'network': NETWORK, 'payer': PAYER})
    with spend_service._budget_transaction() as conn:
        conn.execute('''INSERT INTO x402_refunds
            (reservation_id, operation_id, network, amount_micro_usd, updated_at)
            VALUES (?, 'second', ?, 1000, ?)''', (second, NETWORK, spend_service._now()))
    import sqlite3
    with pytest.raises(sqlite3.IntegrityError):
        refund_service.apply_confirmed_refund(second, {**status, 'original_tx': '0x' + '75' * 32}, rpc)
    assert spend_service.get_status()['spent_usd'] == .001


def test_failed_completion_retains_encrypted_recovery_for_refund_watch(database, monkeypatch):
    op, _ = payment_operations.begin('refund-synthetic')
    rid = reserve(operation_id=op['id'])
    spend_service.reconcile([rid], status_code=400, settlement={'success': True, 'transaction': TX, 'network': NETWORK, 'payer': PAYER})
    monkeypatch.setattr(payment_operations, 'seal', lambda value: json.dumps(value).encode())
    payment_operations.save_headers(op['id'], {'headers': {'PAYMENT-SIGNATURE': 'synthetic'}})
    payment_operations.complete(op['id'], {'status': 400, 'body': 'invalid-strategy'})
    row = sqlite.get_connection().execute('SELECT * FROM x402_refunds').fetchone()
    assert row['reservation_id'] == rid and row['recovery_headers'] is not None
    assert spend_service.get_status()['spent_usd'] == .001


def test_status_proof_never_sent_to_different_origin(monkeypatch):
    monkeypatch.setattr(app_config, 'X402_MANGROVE_BASE_URL', 'https://receiver.example')
    assert refund_service.status_url('https://receiver.example/api/v1/strategies/') == 'https://receiver.example/api/v1/x402/refund-status'
    with pytest.raises(ValueError):
        refund_service.status_url('https://other.example/api/v1/strategies/')


def test_background_observer_uses_private_proof_and_verifies_credit(database, monkeypatch):
    import httpx

    from src.services import payment_reconciliation_worker
    rid, _, rpc, status = seed()
    with spend_service._budget_transaction() as conn:
        conn.execute('UPDATE x402_payments SET resource = ? WHERE id = ?',
                     ('https://receiver.example/api/v1/strategies/', rid))
        conn.execute('UPDATE x402_refunds SET recovery_headers = ? WHERE reservation_id = ?', (b'encrypted', rid))
    monkeypatch.setattr(app_config, 'X402_MANGROVE_BASE_URL', 'https://receiver.example')
    monkeypatch.setattr(payment_reconciliation_worker, 'configured_urls', lambda: {NETWORK: 'synthetic-rpc'})
    monkeypatch.setattr(payment_operations, 'unseal', lambda _: {'headers': {
        'PAYMENT-SIGNATURE': 'synthetic-proof', 'X-Payment-Recovery-Token': 'r' * 43,
        'Authorization': 'must-not-forward'}})
    calls = []
    def respond(request):
        calls.append(request)
        assert str(request.url) == 'https://receiver.example/api/v1/x402/refund-status'
        assert request.headers['PAYMENT-SIGNATURE'] == 'synthetic-proof'
        assert 'Authorization' not in request.headers
        return httpx.Response(200, json=status)
    real_client = httpx.Client
    monkeypatch.setattr(refund_service.httpx, 'Client', lambda **kw: real_client(transport=httpx.MockTransport(respond), **kw))
    class Reader:
        def __call__(self, method, params):
            return rpc(method, params)
        def close(self):
            pass
    monkeypatch.setattr(payment_reconciliation_worker, 'ReadOnlyRPC', lambda _: Reader())
    refund_service.run_once()
    refund_service.run_once()
    assert len(calls) == 1
    assert spend_service.get_status()['spent_usd'] == 0
