"""No live providers: exercise payment pauses and evidence-checked state transitions."""
from __future__ import annotations

import json
import multiprocessing
import sqlite3

import pytest
from eth_utils import keccak
from src.config import app_config
from src.services import spend_service
from src.services.x402_inspection import TOKENS
from src.services.x402_reconciliation import reconcile_authorization
from src.shared.db import sqlite
from src.shared.errors import X402PaymentUncertain

PAYER = '0x' + '11' * 20
PAYEE = '0x' + '22' * 20
NETWORK = 'eip155:84532'
NONCE = '0x' + 'ab' * 32
TX = '0x' + 'cd' * 32
BLOCK = '0x' + 'ef' * 32


@pytest.fixture
def database(tmp_path, monkeypatch):
    path = tmp_path / 'agent.db'
    monkeypatch.setattr(app_config, 'DB_PATH', str(path))
    monkeypatch.setattr(app_config, 'X402_SPEND_CAP_USD', 10)
    sqlite.reset_connection()
    sqlite.init_db()
    yield path
    sqlite.reset_connection()


def reserve(**kwargs):
    return spend_service.reserve(**dict(value=1000, wallet_address=PAYER, payee=PAYEE,
        network=NETWORK, valid_before=100, valid_after=0, authorization_nonce=NONCE,
        asset=TOKENS[NETWORK], **kwargs))


class RPC:
    def __init__(self, used=0, timestamp=101):
        self.used = used
        self.timestamp = timestamp
        self.calls = []
        self.chain = hex(84532)
        self.block_hash = BLOCK
        def topic(text):
            return '0x' + keccak(text=text).hex()
        self.receipt = {'transactionHash': TX, 'status': '0x1', 'blockNumber': '0x10',
            'blockHash': BLOCK, 'logs': [
                {'address': TOKENS[NETWORK], 'topics': [topic('AuthorizationUsed(address,bytes32)'),
                    '0x' + PAYER[2:].rjust(64, '0'), NONCE], 'data': '0x'},
                {'address': TOKENS[NETWORK], 'topics': [topic('Transfer(address,address,uint256)'),
                    '0x' + PAYER[2:].rjust(64, '0'), '0x' + PAYEE[2:].rjust(64, '0')],
                 'data': '0x' + format(1000, '064x')}]}

    def __call__(self, method, params):
        self.calls.append((method, params))
        if method == 'eth_chainId':
            return self.chain
        if method == 'eth_getBlockByNumber':
            return {'number': '0x10', 'hash': self.block_hash, 'timestamp': hex(self.timestamp)}
        if method == 'eth_call':
            assert params[1] == {'blockHash': BLOCK, 'requireCanonical': True}
            return '0x' + format(self.used, '064x')
        assert method == 'eth_getTransactionReceipt'
        assert params == [TX]
        return self.receipt


def test_pending_authorizations_do_not_block_other_requests_after_restart(database):
    rid = reserve()
    sqlite.reset_connection()
    other = reserve()
    assert rid != other
    assert spend_service.get_status()['payment_pauses'] == []
    assert spend_service.get_status()['spent_usd'] == .002


def _reserve_process(path, start, result):
    app_config.DB_PATH = path
    app_config.X402_SPEND_CAP_USD = 50
    sqlite.reset_connection()
    try:
        start.wait(timeout=15)
        reserve()
        result.put('reserved')
    except X402PaymentUncertain:
        result.put('paused')
    finally:
        sqlite.reset_connection()


def test_concurrent_independent_operations_can_authorize(database):
    ctx = multiprocessing.get_context('spawn')
    barrier, queue = ctx.Barrier(3), ctx.Queue()
    processes = [ctx.Process(target=_reserve_process, args=(str(database), barrier, queue)) for _ in range(3)]
    try:
        for p in processes:
            p.start()
        assert sorted(queue.get(timeout=25) for _ in processes) == ['reserved', 'reserved', 'reserved']
        for p in processes:
            p.join(5)
            assert p.exitcode == 0
        assert len(spend_service.list_payments()) == 3
    finally:
        for p in processes:
            if p.is_alive():
                p.terminate()
                p.join(5)
        queue.close()


def test_expired_unused_reconciliation_is_atomic_audited_and_idempotent(database):
    rid = reserve()
    result = reconcile_authorization(rid, RPC())
    assert result['ledger_changed'] and result['ledger_state'] == 'released'
    assert result['payment_sent'] is False
    assert spend_service.get_status()['spent_usd'] == 0
    assert spend_service.get_status()['payment_pauses'] == []
    again = reconcile_authorization(rid, lambda *args: pytest.fail('already resolved'))
    assert not again['ledger_changed']
    evidence, = sqlite.get_connection().execute('SELECT evidence_json FROM x402_reconciliation_evidence').fetchall()
    record = json.loads(evidence[0])
    assert record['authorization']['authorization_nonce'] == NONCE
    assert record['evidence']['finalized_block_hash'] == BLOCK
    reserve()  # resumes only after evidence, with the existing budget


def test_proven_nonpayment_restores_capacity_without_new_budget(database, monkeypatch):
    monkeypatch.setattr(app_config, 'X402_SPEND_CAP_USD', .001)
    rid = reserve()
    before = spend_service._get_state()
    reconcile_authorization(rid, RPC())
    assert spend_service._get_state()['period_id'] == before['period_id']
    assert spend_service.get_status()['cap_usd'] == .001
    assert spend_service.get_status()['exhausted'] is False
    reserve()


@pytest.mark.parametrize('used,timestamp,outcome', [(0, 100, 'unused_not_expired'), (1, 101, 'used_or_cancelled')])
def test_unknown_and_live_authorizations_stay_reserved(database, used, timestamp, outcome):
    rid = reserve()
    result = reconcile_authorization(rid, RPC(used, timestamp))
    assert result['outcome'] == outcome
    assert not result['ledger_changed']
    reserve()
    assert spend_service.get_status()['spent_usd'] == .002
    assert sqlite.get_connection().execute('SELECT COUNT(*) FROM x402_reconciliation_evidence').fetchone()[0] == 0


def test_missing_metadata_and_rpc_failures_do_not_release(database):
    rid = spend_service.reserve(value=1000, wallet_address=PAYER, network=NETWORK)
    result = reconcile_authorization(rid, lambda *args: pytest.fail('missing metadata'))
    assert result['outcome'] == 'legacy_metadata_missing'
    spend_service.release_unsigned(rid)  # synthetic reservation was never signed
    rid = reserve()
    def unavailable(*args):
        raise TimeoutError('SYNTHETIC_SECRET')
    with pytest.raises(TimeoutError):
        reconcile_authorization(rid, unavailable)
    assert spend_service.list_payments()[0]['state'] == 'authorized'


@pytest.mark.parametrize('outcome', ['settled', 'cancelled'])
def test_exact_finalized_transaction_resolves(database, outcome):
    rid = reserve()
    rpc = RPC(used=1)
    if outcome == 'cancelled':
        rpc.receipt['logs'] = [rpc.receipt['logs'][0]]
        rpc.receipt['logs'][0]['topics'][0] = '0x' + keccak(text='AuthorizationCanceled(address,bytes32)').hex()
    result = reconcile_authorization(rid, rpc, transaction=TX)
    assert result['ledger_changed']
    assert result['ledger_state'] == ('settled' if outcome == 'settled' else 'released')
    assert spend_service.get_status()['spent_usd'] == (.001 if outcome == 'settled' else 0)
    assert spend_service.get_status()['payment_pauses'] == []


@pytest.mark.parametrize('bad', ['nonce', 'payer', 'payee', 'token', 'amount', 'status', 'hash', 'future', 'chain', 'canonical', 'duplicate_transfer'])
def test_transaction_must_match_every_payment_field(database, bad):
    rid = reserve()
    rpc = RPC(used=1)
    if bad == 'nonce':
        rpc.receipt['logs'][0]['topics'][2] = '0x' + '00' * 32
    elif bad == 'payer':
        rpc.receipt['logs'][0]['topics'][1] = '0x' + '00' * 32
    elif bad == 'payee':
        rpc.receipt['logs'][1]['topics'][2] = '0x' + '00' * 32
    elif bad == 'token':
        rpc.receipt['logs'][1]['address'] = PAYEE
    elif bad == 'amount':
        rpc.receipt['logs'][1]['data'] = '0x' + format(999, '064x')
    elif bad == 'status':
        rpc.receipt['status'] = '0x0'
    elif bad == 'hash':
        rpc.receipt['transactionHash'] = BLOCK
    elif bad == 'future':
        rpc.receipt['blockNumber'] = '0x11'
    elif bad == 'chain':
        rpc.chain = '0x1'
    elif bad == 'canonical':
        rpc.receipt['blockHash'] = TX
    elif bad == 'duplicate_transfer':
        rpc.receipt['logs'].append(rpc.receipt['logs'][1])
    try:
        result = reconcile_authorization(rid, rpc, transaction=TX)
        assert not result['ledger_changed']
    except ValueError:
        assert bad in {'chain', 'canonical', 'hash'}
    assert spend_service.list_payments()[0]['state'] == 'authorized'


def test_audit_write_failure_rolls_back_ledger_transition(database):
    rid = reserve()
    conn = sqlite.get_connection()
    conn.executescript("CREATE TRIGGER fail_audit BEFORE INSERT ON x402_reconciliation_evidence BEGIN SELECT RAISE(ABORT, 'audit unavailable'); END;")
    with pytest.raises(sqlite3.IntegrityError):
        reconcile_authorization(rid, RPC())
    assert spend_service.list_payments()[0]['state'] == 'authorized'
    reserve()
    assert spend_service.get_status()['spent_usd'] == .002


def test_concurrent_receipt_wins_over_stale_inspection(database):
    rid = reserve()
    rpc = RPC()
    def race(method, params):
        if method == 'eth_call':
            spend_service.settle(rid, transaction=TX)
        return rpc(method, params)
    result = reconcile_authorization(rid, race)
    assert result['outcome'] == 'ledger_changed_during_inspection'
    assert spend_service.list_payments()[0]['state'] == 'settled'
    assert sqlite.get_connection().execute('SELECT COUNT(*) FROM x402_reconciliation_evidence').fetchone()[0] == 0


@pytest.fixture
def clock(monkeypatch):
    from datetime import datetime, timezone
    state = [datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)]
    monkeypatch.setattr(spend_service, '_now', lambda: state[0].isoformat())
    return state


@pytest.mark.parametrize('elapsed', [0, 1, 299, 300, 301, 86400])
def test_new_requests_never_wait_for_a_timer(database, clock, elapsed):
    from datetime import timedelta
    old = reserve()
    clock[0] += timedelta(seconds=elapsed)
    new = reserve()
    assert old != new
    assert spend_service.get_status()['payment_pauses'] == []
    assert spend_service.get_status()['spent_usd'] == .002
    assert all(row['state'] == 'authorized' for row in spend_service.list_payments())


def test_expiry_never_replenishes_spending_capacity(database, clock, monkeypatch):
    from datetime import timedelta

    from src.shared.errors import X402SpendCapExceeded
    monkeypatch.setattr(app_config, 'X402_SPEND_CAP_USD', .001)
    reserve()
    clock[0] += timedelta(seconds=300)
    with pytest.raises(X402SpendCapExceeded):
        reserve()
    assert spend_service.get_status()['spent_usd'] == .001
    assert spend_service.get_status()['exhausted'] is True


@pytest.mark.parametrize('created', ['not-a-date', 'now', '2027-01-01T00:00:00+00:00'])
def test_invalid_legacy_clock_cannot_pause_indefinitely(database, clock, created):
    rid = reserve()
    conn = sqlite.get_connection()
    conn.execute('UPDATE x402_payments SET created_at = ? WHERE id = ?', (created, rid))
    conn.commit()
    assert spend_service.get_status()['payment_pauses'] == []
    assert spend_service.get_status()['unresolved_payments'][0]['reserved_usd'] == .001
    reserve()
    assert spend_service.get_status()['spent_usd'] == .002
