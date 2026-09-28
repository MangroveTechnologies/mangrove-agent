"""Paid result persistence, cancellation and recovery with real local ledgers."""
import httpx
import pytest

from src.services import spend_service, x402_payer
from src.shared.errors import X402PaymentUncertain
from tests.unit import test_x402_payer as payer_fixtures
from tests.unit.test_mcp_payer import _RecoverableMcpSession

mock_http = payer_fixtures.mock_http
temp_db = payer_fixtures.temp_db
stub_keyring = payer_fixtures.stub_keyring
mock_sdk_create = payer_fixtures.mock_sdk_create
unbacked_wallet = payer_fixtures.unbacked_wallet
wallet = payer_fixtures.wallet
sepolia_network = payer_fixtures.sepolia_network
_mcp_receipt = payer_fixtures._mcp_receipt
_payment_required_header = payer_fixtures._payment_required_header
_settlement_header = payer_fixtures._settlement_header


async def test_result_storage_failure_retains_recoverable_payment(wallet, sepolia_network, monkeypatch):
    from src.services import payment_operations
    from src.shared.db.sqlite import get_connection

    session = _RecoverableMcpSession(receipt=_mcp_receipt())
    original_complete = payment_operations.complete

    def unavailable(*args):
        raise RuntimeError('SYNTHETIC_PRIVATE_STORAGE_DETAILS')

    monkeypatch.setattr(payment_operations, 'complete', unavailable)
    kwargs = dict(wallet_address=wallet, resource='https://receiver.test/mcp', arguments={'limit': 10})
    with pytest.raises(X402PaymentUncertain) as caught:
        await x402_payer.pay_mcp(session, **kwargs)
    assert 'SYNTHETIC_PRIVATE' not in str(caught.value)
    assert caught.value.operation_id
    row = get_connection().execute('SELECT * FROM x402_operations').fetchone()
    assert row['state'] == 'pending' and row['payment_headers'] is not None
    assert spend_service.list_payments()[0]['state'] == 'settled'
    monkeypatch.setattr(payment_operations, 'complete', original_complete)
    recovered_session = _RecoverableMcpSession(receipt=_mcp_receipt())
    result = await x402_payer.pay_mcp(recovered_session, operation_id=caught.value.operation_id, **kwargs)
    assert result.paid
    assert recovered_session.calls[0][2] == session.calls[-1][2]
    assert len(spend_service.list_payments()) == 1


async def test_cleanup_failure_cannot_replace_payment_uncertainty(wallet, sepolia_network, monkeypatch):
    from src.services import payment_operations

    def unavailable(*args):
        raise RuntimeError('SYNTHETIC_PRIVATE_STORAGE_DETAILS')

    monkeypatch.setattr(payment_operations, 'abandon_unsigned', unavailable)
    with pytest.raises(X402PaymentUncertain) as caught:
        await x402_payer.pay_mcp(_RecoverableMcpSession(failure=TimeoutError()),
                                wallet_address=wallet, resource='https://receiver.test/mcp')
    assert caught.value.operation_id
    assert len(payment_operations.pending_status()) == 1
    assert len(spend_service.list_payments()) == 1


async def test_cancelled_signed_call_remains_counted_and_recovers(wallet, sepolia_network):
    import asyncio

    from src.services import payment_operations
    from src.shared.db import sqlite

    entered = asyncio.Event()

    class Interrupted(_RecoverableMcpSession):
        async def call_tool(self, **kwargs):
            result = await super().call_tool(**kwargs)
            if kwargs.get('meta'):
                entered.set()
                await asyncio.Event().wait()
            return result

    session = Interrupted(receipt=_mcp_receipt())
    kwargs = dict(wallet_address=wallet, resource='https://receiver.test/mcp', arguments={'limit': 10})
    task = asyncio.create_task(x402_payer.pay_mcp(session, **kwargs))
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    payment, = spend_service.list_payments()
    assert payment['state'] == 'authorized'
    spent = spend_service.get_status()['spent_usd']
    assert spent > 0
    sqlite.reset_connection()
    operation, = payment_operations.pending_status()
    recovered = _RecoverableMcpSession(receipt=_mcp_receipt())
    assert (await x402_payer.pay_mcp(recovered, operation_id=operation['id'], **kwargs)).paid
    assert recovered.calls[0][2] == session.calls[-1][2]
    assert spend_service.get_status()['spent_usd'] == spent
    assert len(spend_service.list_payments()) == 1


async def test_async_result_storage_failure_recovers_without_new_payment(wallet, sepolia_network, mock_http, monkeypatch):
    from src.services import payment_operations, spend_service, x402_payer

    mock_http.install(
        httpx.Response(402, headers={'PAYMENT-REQUIRED': _payment_required_header(), 'X-Payment-Idempotency': 'v1'}),
        httpx.Response(200, json={'done': True}, headers={'payment-response': _settlement_header()}),
        httpx.Response(200, json={'done': True}, headers={'payment-response': _settlement_header()}),
    )
    def unavailable(*args):
        raise RuntimeError('SYNTHETIC_PRIVATE_STORAGE_DETAILS')
    with monkeypatch.context() as fault:
        fault.setattr(payment_operations, 'complete', unavailable)
        with pytest.raises(X402PaymentUncertain) as caught:
            await x402_payer.pay('https://agent.test/pending', wallet_address=wallet)
    assert caught.value.payment_state == 'settled'
    assert caught.value.operation_id
    assert 'SYNTHETIC_PRIVATE' not in str(caught.value)
    result = await x402_payer.pay('https://agent.test/pending', wallet_address=wallet)
    assert result.body == {'done': True}
    assert len(mock_http.requests) == 3
    assert mock_http.requests[1].headers['PAYMENT-SIGNATURE'] == mock_http.requests[2].headers['PAYMENT-SIGNATURE']
    assert len(spend_service.list_payments()) == 1
