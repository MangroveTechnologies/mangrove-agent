"""Real SQLite operation ownership and worker recovery; no live chain calls."""
from concurrent.futures import ThreadPoolExecutor

import pytest

from src.config import app_config
from src.services import payment_operations as operations
from src.services import payment_reconciliation_worker as worker
from src.services import spend_service
from src.shared.db import sqlite
from src.shared.errors import ValidationError, X402PaymentUncertain

from . import test_x402_uncertainty as evidence_fixtures
from .test_x402_uncertainty import NETWORK, RPC, TX, reserve

database = evidence_fixtures.database


@pytest.mark.asyncio
@pytest.mark.parametrize('url', ['https://payments.test/a', 'http://localhost/a', 'http://127.0.0.1/a', 'http://[::1]/a'])
async def test_async_operation_keeps_local_development_and_strips_caller_recovery_token(monkeypatch, url):
    from types import SimpleNamespace

    from src.services import x402_payer
    monkeypatch.setattr(x402_payer, 'resolve_payer_wallet', lambda _: 'synthetic-payer')
    monkeypatch.setattr(x402_payer, 'get_network', lambda: NETWORK)
    monkeypatch.setattr(operations, 'begin', lambda *a: ({'id': 'synthetic-operation'}, True))
    monkeypatch.setattr(operations, 'reservation_ids', lambda _: [])
    monkeypatch.setattr(operations, 'abandon_unsigned', lambda _: None)
    received = []

    @operations.tracked_payment
    async def request(url, headers=None, timeout=30):
        received.append(headers)
        return SimpleNamespace(body={})

    await request(url, headers={'X-Payment-Recovery-Token': 'SYNTHETIC_SECRET', 'Accept': 'application/json'})
    assert received == [{'accept': 'application/json'}]


def test_concurrent_duplicate_claim_has_single_owner(database):
    with ThreadPoolExecutor(max_workers=8) as pool:
        claims = list(pool.map(lambda _: operations.begin('same-request'), range(8)))
    assert sum(created for _, created in claims) == 1
    assert len({row['id'] for row, _ in claims}) == 1
    sqlite.reset_connection()
    row, created = operations.begin('same-request')
    assert not created and row['id'] == claims[0][0]['id']


def test_operation_can_reserve_only_once_but_other_operation_can_pay(database):
    first, _ = operations.begin('one')
    reserve(operation_id=first['id'])
    with pytest.raises(X402PaymentUncertain):
        reserve(operation_id=first['id'])
    other, _ = operations.begin('two')
    reserve(operation_id=other['id'])
    assert spend_service.get_status()['spent_usd'] == .002
    assert len(spend_service.get_status()['pending_operations']) == 2


def test_identity_conflict_fails_before_new_signature(database):
    row, _ = operations.begin('one')
    with pytest.raises(ValidationError):
        operations.begin('different', row['id'])
    assert spend_service.list_payments() == []


def test_unsigned_failure_can_be_retried_without_reserving_money(database):
    row, _ = operations.begin('one')
    operations.abandon_unsigned(row['id'])
    _, created = operations.begin('one', row['id'])
    assert created


def test_worker_releases_proven_nonpayment_and_restores_existing_budget(database, monkeypatch):
    monkeypatch.setattr(app_config, 'X402_SPEND_CAP_USD', .001)
    row, _ = operations.begin('one')
    rid = reserve(operation_id=row['id'])
    assert spend_service.get_status()['exhausted']
    assert worker.run_once(urls={NETWORK: 'synthetic'}, rpc_factory=lambda _: RPC(), now=1000) == 1
    assert spend_service.get_status()['spent_usd'] == 0
    assert spend_service.get_status()['cap_usd'] == .001
    assert not spend_service.get_status()['exhausted']
    assert sqlite.get_connection().execute('SELECT state FROM x402_payments WHERE id = ?', (rid,)).fetchone()[0] == 'released'
    assert worker.run_once(urls={NETWORK: 'synthetic'}, rpc_factory=lambda _: pytest.fail('already resolved'), now=2000) == 0


def test_worker_unknown_retains_amount_and_backoff_survives_restart(database):
    rid = reserve()
    def fail(*args):
        raise TimeoutError('SYNTHETIC_SECRET')
    worker.run_once(urls={NETWORK: 'synthetic'}, rpc_factory=lambda _: fail, now=1000)
    sqlite.reset_connection()
    job = sqlite.get_connection().execute('SELECT * FROM x402_reconciliation_jobs WHERE reservation_id = ?', (rid,)).fetchone()
    assert job['attempts'] == 1 and job['next_check'] > 1000
    assert job['outcome'] == 'inspection_unavailable'
    assert worker.run_once(urls={NETWORK: 'synthetic'}, rpc_factory=lambda _: pytest.fail('backoff'), now=1001) == 0
    assert spend_service.get_status()['spent_usd'] == .001
    reserve()
    assert spend_service.get_status()['spent_usd'] == .002


def test_worker_discovers_transaction_then_checks_exact_receipt(database):
    reserve()
    rpc = RPC(used=1)
    def dispatch(method, params):
        if method == 'eth_getLogs':
            assert params[0]['topics'][2].startswith('0x')
            return [{'transactionHash': TX, 'removed': False}]
        return rpc(method, params)
    worker.run_once(urls={NETWORK: 'synthetic'}, rpc_factory=lambda _: dispatch, now=1000)
    assert spend_service.list_payments()[0]['state'] == 'settled'
    assert spend_service.get_status()['spent_usd'] == .001


def test_worker_never_uses_unconfigured_public_rpc(database, monkeypatch):
    reserve()
    monkeypatch.setattr(app_config, 'X402_RECONCILIATION_RPC_URLS', None)
    assert worker.run_once(rpc_factory=lambda _: pytest.fail('unexpected RPC')) == 0


def test_worker_durable_lease_prevents_concurrent_inspections(database):
    reserve()
    def factory(_):
        assert worker.run_once(urls={NETWORK: 'synthetic'}, rpc_factory=lambda _: pytest.fail('duplicate'), now=1001) == 0
        return RPC()
    worker.run_once(urls={NETWORK: 'synthetic'}, rpc_factory=factory, now=1000)


def test_normal_retry_reconciles_expired_unused_before_new_attempt(database, monkeypatch):
    first, _ = operations.begin('same-request')
    rid = reserve(operation_id=first['id'])
    monkeypatch.setattr(worker, 'configured_urls', lambda: {NETWORK: 'synthetic'})
    run = worker.run_once
    monkeypatch.setattr(worker, 'run_once', lambda **kwargs: run(rpc_factory=lambda _: RPC(), **kwargs))
    retried, created = operations.begin('same-request')
    assert created and retried['id'] != first['id']
    assert sqlite.get_connection().execute('SELECT state FROM x402_payments WHERE id=?', (rid,)).fetchone()[0] == 'released'
    assert sqlite.get_connection().execute('SELECT state FROM x402_operations WHERE id=?', (first['id'],)).fetchone()[0] == 'unsigned_failed'
    assert spend_service.get_status()['spent_usd'] == 0
    assert len(spend_service.list_payments()) == 1


def test_recovery_does_not_sign_replacement_for_ambiguous_payment(database, monkeypatch):
    first, _ = operations.begin('same-request')
    reserve(operation_id=first['id'])
    monkeypatch.setattr(worker, 'configured_urls', lambda: {NETWORK: 'synthetic'})
    run = worker.run_once
    def unavailable(*args):
        raise TimeoutError('SECRET')
    monkeypatch.setattr(worker, 'run_once', lambda **kwargs: run(rpc_factory=lambda _: unavailable, **kwargs))
    recovered, created = operations.begin('same-request')
    assert not created and recovered['id'] == first['id']
    assert spend_service.get_status()['spent_usd'] == .001
    assert len(spend_service.list_payments()) == 1


def test_different_request_does_not_wait_for_old_recovery(database, monkeypatch):
    first, _ = operations.begin('first-tool')
    reserve(operation_id=first['id'])
    monkeypatch.setattr(worker, 'configured_urls', lambda: {NETWORK: 'synthetic'})
    monkeypatch.setattr(worker, 'run_once', lambda **kwargs: pytest.fail('Unrelated request waited for recovery'))
    other, created = operations.begin('other-tool')
    assert created and other['id'] != first['id']
    reserve(operation_id=other['id'])
    assert spend_service.get_status()['spent_usd'] == .002


def test_targeted_recovery_does_not_process_another_operation(database):
    first, _ = operations.begin('first')
    second, _ = operations.begin('second')
    rid = reserve(operation_id=first['id'])
    other = reserve(operation_id=second['id'])
    assert worker.run_once(urls={NETWORK: 'synthetic'}, rpc_factory=lambda _: RPC(), operation_id=first['id']) == 1
    rows = {r['id']: r['state'] for r in sqlite.get_connection().execute('SELECT id,state FROM x402_payments')}
    assert rows[rid] == 'released' and rows[other] == 'authorized'


def test_rpc_inspection_has_total_deadline(monkeypatch):
    rpc = worker.ReadOnlyRPC('https://rpc.test')
    try:
        monkeypatch.setattr(worker.time, 'monotonic', lambda: rpc.deadline + 1)
        with pytest.raises(TimeoutError):
            rpc('eth_chainId', [])
    finally:
        rpc.close()


def test_confirmed_settlement_keeps_original_operation_for_result_recovery(database, monkeypatch):
    first, _ = operations.begin('same-request')
    rid = reserve(operation_id=first['id'])
    with spend_service._budget_transaction() as conn:
        conn.execute('UPDATE x402_payments SET transaction_hash=? WHERE id=?', (TX, rid))
    monkeypatch.setattr(worker, 'configured_urls', lambda: {NETWORK: 'synthetic'})
    run = worker.run_once
    monkeypatch.setattr(worker, 'run_once', lambda **kwargs: run(rpc_factory=lambda _: RPC(used=1), **kwargs))
    recovered, created = operations.begin('same-request')
    assert not created and recovered['id'] == first['id']
    assert sqlite.get_connection().execute('SELECT state FROM x402_payments WHERE id=?', (rid,)).fetchone()[0] == 'settled'
    assert spend_service.get_status()['spent_usd'] == .001


def test_concurrent_retries_after_proven_nonpayment_have_one_new_owner(database, monkeypatch):
    first, _ = operations.begin('same-request')
    reserve(operation_id=first['id'])
    monkeypatch.setattr(worker, 'configured_urls', lambda: {NETWORK: 'synthetic'})
    run = worker.run_once
    monkeypatch.setattr(worker, 'run_once', lambda **kwargs: run(rpc_factory=lambda _: RPC(), **kwargs))
    with ThreadPoolExecutor(max_workers=4) as pool:
        claims = list(pool.map(lambda _: operations.begin('same-request'), range(4)))
    assert sum(created for _, created in claims) == 1
    new = [row['id'] for row, created in claims if created]
    assert new[0] != first['id']
    assert len(spend_service.list_payments()) == 1


def test_fresh_identity_does_not_inherit_identical_pending_request(database, monkeypatch):
    import uuid
    first, _ = operations.begin('identical', str(uuid.uuid4()))
    original = reserve(operation_id=first['id'])
    monkeypatch.setattr(worker, 'configured_urls', lambda: {NETWORK: 'synthetic'})
    monkeypatch.setattr(worker, 'run_once', lambda **kwargs: pytest.fail('Fresh request waited for old recovery'))
    second, created = operations.begin('identical', str(uuid.uuid4()))
    assert created and second['id'] != first['id']
    reserve(operation_id=second['id'])
    status = spend_service.get_status()
    assert status['reserved_usd'] == .002
    assert status['settled_usd'] == 0
    assert sqlite.get_connection().execute('SELECT state FROM x402_payments WHERE id=?', (original,)).fetchone()[0] == 'authorized'


def test_same_explicit_identity_still_has_one_owner_under_concurrency(database):
    import uuid
    oid = str(uuid.uuid4())
    with ThreadPoolExecutor(max_workers=8) as pool:
        claims = list(pool.map(lambda _: operations.begin('same-request', oid), range(8)))
    assert sum(created for _, created in claims) == 1
    assert {row['id'] for row, _ in claims} == {oid}


def test_recovery_selects_original_when_identical_new_request_exists(database, monkeypatch):
    import uuid
    first, _ = operations.begin('identical', str(uuid.uuid4()))
    original = reserve(operation_id=first['id'])
    second, _ = operations.begin('identical', str(uuid.uuid4()))
    reserve(operation_id=second['id'])
    monkeypatch.setattr(worker, 'configured_urls', lambda: {})
    recovered, created = operations.begin('identical', first['id'])
    assert not created and recovered['id'] == first['id']
    assert operations.reservation_ids(first['id']) == [original]


def test_expired_unused_release_does_not_remove_history_or_other_reservation(database):
    import uuid
    first, _ = operations.begin('identical', str(uuid.uuid4()))
    rid = reserve(operation_id=first['id'])
    second, _ = operations.begin('identical', str(uuid.uuid4()))
    reserve(operation_id=second['id'])
    assert worker.run_once(urls={NETWORK: 'synthetic'}, rpc_factory=lambda _: RPC(), operation_id=first['id']) == 1
    status = spend_service.get_status()
    assert status['reserved_usd'] == .001
    assert status['settled_usd'] == 0
    assert len(spend_service.list_payments()) == 2
    assert sqlite.get_connection().execute('SELECT state FROM x402_payments WHERE id=?', (rid,)).fetchone()[0] == 'released'


def test_operation_index_upgrade_preserves_existing_reservation(database):
    import uuid
    from pathlib import Path
    first, _ = operations.begin('identical', str(uuid.uuid4()))
    rid = reserve(operation_id=first['id'])
    conn = sqlite.get_connection()
    conn.executescript("DROP INDEX idx_x402_active_operation; CREATE UNIQUE INDEX idx_x402_active_operation "
                       "ON x402_operations(fingerprint) WHERE state = 'pending';")
    migration = Path(operations.__file__).parents[1] / 'shared/db/migrations/015_independent_payment_operations.sql'
    conn.executescript(migration.read_text())
    second, created = operations.begin('identical', str(uuid.uuid4()))
    assert created and second['id'] != first['id']
    row = conn.execute('SELECT state,operation_id FROM x402_payments WHERE id=?', (rid,)).fetchone()
    assert row['state'] == 'authorized' and row['operation_id'] == first['id']
