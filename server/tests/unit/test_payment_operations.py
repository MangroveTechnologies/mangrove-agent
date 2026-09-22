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
