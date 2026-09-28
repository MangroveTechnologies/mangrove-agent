"""Remote MCP arguments, complete results and durable payment recovery."""
import pytest

from src.services import spend_service, x402_payer
from src.shared.errors import ValidationError, X402PaymentError, X402PaymentUncertain
from tests.unit import test_x402_payer as payer_fixtures

temp_db = payer_fixtures.temp_db
stub_keyring = payer_fixtures.stub_keyring
mock_sdk_create = payer_fixtures.mock_sdk_create
unbacked_wallet = payer_fixtures.unbacked_wallet
wallet = payer_fixtures.wallet
sepolia_network = payer_fixtures.sepolia_network
_McpPaymentSession = payer_fixtures._McpPaymentSession
_mcp_receipt = payer_fixtures._mcp_receipt


class _RecoverableMcpSession(_McpPaymentSession):
    async def call_tool(self, **kwargs):
        result = await super().call_tool(**kwargs)
        result.meta = {**(result.meta or {}), 'mangrove/payment': {'idempotency': 'v1'}}
        if kwargs.get('meta'):
            result.structuredContent = {'signals': [{'name': 'example'}], 'has_more': True, 'next_offset': 10}
        return result


async def test_mcp_arguments_and_complete_result_survive(wallet, sepolia_network):
    session = _RecoverableMcpSession(receipt=_mcp_receipt())
    args = {'limit': 10, 'category': 'trend'}
    result = await x402_payer.pay_mcp(session, name='list_signals', arguments=args,
                                    wallet_address=wallet, resource='https://receiver.test/mcp')
    assert all(call[0:2] == ('list_signals', args) for call in session.calls)
    assert result.mcp_result['structuredContent']['next_offset'] == 10
    assert result.mcp_result['_meta']['x402/payment-response'] == _mcp_receipt()
    assert result.mcp_result['isError'] is False
    assert len(result.mcp_result['content']) == 1
    assert set(session.calls[-1][2]['mangrove/payment']) == {'operation_id', 'recovery_token'}


async def test_mcp_lost_response_recovers_exact_payment(wallet, sepolia_network):
    from src.services import payment_operations, spend_service

    first = _RecoverableMcpSession(failure=TimeoutError('private'))
    kwargs = dict(name='list_signals', arguments={'limit': 10}, wallet_address=wallet,
                  resource='https://receiver.test/mcp')
    with pytest.raises(X402PaymentUncertain) as caught:
        await x402_payer.pay_mcp(first, **kwargs)
    second = _RecoverableMcpSession(receipt=_mcp_receipt())
    result = await x402_payer.pay_mcp(second, operation_id=caught.value.operation_id, **kwargs)
    assert result.paid
    assert len(second.calls) == 1
    assert second.calls[0][2] == first.calls[-1][2]
    assert len(spend_service.list_payments()) == 1
    assert payment_operations.pending_status() == []
    cached_session = _RecoverableMcpSession(failure=AssertionError('must use cache'))
    cached = await x402_payer.pay_mcp(cached_session, operation_id=caught.value.operation_id, **kwargs)
    assert cached.mcp_result == result.mcp_result and not cached_session.calls
    assert cached.body == result.body
    assert cached.body[0].text == result.body[0].text


async def test_mcp_changed_arguments_cannot_recover_original(wallet, sepolia_network):
    first = _RecoverableMcpSession(failure=TimeoutError())
    with pytest.raises(X402PaymentUncertain) as caught:
        await x402_payer.pay_mcp(first, arguments={'limit': 10}, wallet_address=wallet, resource='https://receiver.test/mcp')
    second = _RecoverableMcpSession()
    with pytest.raises(ValidationError):
        await x402_payer.pay_mcp(second, arguments={'limit': 11}, operation_id=caught.value.operation_id,
                                 wallet_address=wallet, resource='https://receiver.test/mcp')
    assert not second.calls


async def test_mcp_different_arguments_are_independent(wallet, sepolia_network):
    from src.services import spend_service

    first = _RecoverableMcpSession(failure=TimeoutError())
    with pytest.raises(X402PaymentUncertain):
        await x402_payer.pay_mcp(first, arguments={'limit': 10}, wallet_address=wallet, resource='https://receiver.test/mcp')
    second = _RecoverableMcpSession(receipt=_mcp_receipt())
    assert (await x402_payer.pay_mcp(second, arguments={'limit': 11}, wallet_address=wallet,
                                    resource='https://receiver.test/mcp')).paid
    assert len(spend_service.list_payments()) == 2


async def test_mcp_no_recovery_capability_never_reauthorizes(wallet, sepolia_network):
    first = _McpPaymentSession(failure=TimeoutError())
    with pytest.raises(X402PaymentUncertain):
        await x402_payer.pay_mcp(first, wallet_address=wallet, resource='https://receiver.test/mcp')
    second = _McpPaymentSession(receipt=_mcp_receipt())
    with pytest.raises(X402PaymentUncertain):
        await x402_payer.pay_mcp(second, wallet_address=wallet, resource='https://receiver.test/mcp')
    assert not second.calls


async def test_mcp_pending_receipt_records_charge_without_completing(wallet, sepolia_network):
    from src.services import payment_operations, spend_service

    class Pending(_RecoverableMcpSession):
        async def call_tool(self, **kwargs):
            result = await super().call_tool(**kwargs)
            if kwargs.get('meta'):
                result.meta['mangrove/payment']['state'] = 'pending'
            return result

    session = Pending(receipt=_mcp_receipt())
    with pytest.raises(X402PaymentUncertain) as caught:
        await x402_payer.pay_mcp(session, wallet_address=wallet, resource='https://receiver.test/mcp')
    assert caught.value.payment_state == 'settled'
    assert spend_service.list_payments()[0]['state'] == 'settled'
    assert len(payment_operations.pending_status()) == 1


async def test_mcp_no_retry_marker_wins_over_payment_quote(wallet, sepolia_network):
    class Refusal(_RecoverableMcpSession):
        async def call_tool(self, **kwargs):
            result = await super().call_tool(**kwargs)
            result.structuredContent['retry_payment'] = False
            return result

    session = Refusal()
    with pytest.raises(X402PaymentError):
        await x402_payer.pay_mcp(session, wallet_address=wallet, resource='https://receiver.test/mcp')
    assert len(session.calls) == 1
    assert spend_service.list_payments() == []


@pytest.mark.parametrize('arguments', [[], 'bad', {'limit': float('nan')}, {'limit': object()}])
async def test_mcp_invalid_arguments_never_contact_server(wallet, sepolia_network, arguments):
    session = _McpPaymentSession()
    with pytest.raises(ValidationError):
        await x402_payer.pay_mcp(session, arguments=arguments, wallet_address=wallet, resource='https://receiver.test/mcp')
    assert not session.calls


@pytest.mark.parametrize('endpoint', ['http://remote.test/mcp', 'https://user:secret@remote.test/mcp',
                                     'https://remote.test/mcp?token=secret', 'file:///tmp/mcp'])
async def test_remote_mcp_rejects_unsafe_endpoints(endpoint):
    with pytest.raises(ValidationError):
        await x402_payer.pay_remote_mcp(endpoint, name='list_signals')


async def test_legacy_empty_argument_operation_is_not_replaced(wallet, sepolia_network):
    from src.services import payment_operations

    resource = 'https://receiver.test/mcp'
    digest = payment_operations.fingerprint(wallet, x402_payer.get_network(), 'MCP:hello_mangrove', resource, b'')
    original, _ = payment_operations.begin(digest)
    session = _McpPaymentSession()
    with pytest.raises(X402PaymentUncertain) as caught:
        await x402_payer.pay_mcp(session, wallet_address=wallet, resource=resource)
    assert caught.value.operation_id == original['id']
    assert not session.calls


async def test_paid_mcp_failure_retains_refund_recovery_headers(wallet, sepolia_network):
    from src.services import payment_operations
    from src.shared.db.sqlite import get_connection

    session = _RecoverableMcpSession(receipt=_mcp_receipt(), is_error=True)
    result = await x402_payer.pay_mcp(session, wallet_address=wallet, resource='https://receiver.test/mcp')
    assert result.paid and result.mcp_result['isError']
    row = get_connection().execute('SELECT recovery_headers FROM x402_refunds').fetchone()
    assert row is not None
    saved = payment_operations.unseal(row['recovery_headers'])
    assert saved['headers']['X-Payment-Recovery-Token'] == session.calls[-1][2]['mangrove/payment']['recovery_token']
    assert saved['headers']['PAYMENT-SIGNATURE']


@pytest.mark.parametrize('legacy', [False, True])
@pytest.mark.parametrize('is_error', [False, True])
async def test_cached_mixed_content_preserves_public_body(wallet, sepolia_network, legacy, is_error):
    import uuid

    from mcp.types import ImageContent, TextContent

    from src.services import payment_operations
    from src.shared.db.sqlite import get_connection

    class MixedContent(_RecoverableMcpSession):
        async def call_tool(self, **kwargs):
            result = await super().call_tool(**kwargs)
            if kwargs.get('meta'):
                result.content = [TextContent(type='text', text='result'),
                                  ImageContent(type='image', data='c3ludGhldGlj', mimeType='image/png')]
            return result

    oid = str(uuid.uuid4())
    kwargs = dict(wallet_address=wallet, resource='https://receiver.test/mcp', operation_id=oid)
    original = await x402_payer.pay_mcp(MixedContent(receipt=_mcp_receipt(), is_error=is_error), **kwargs)
    if legacy:
        conn = get_connection()
        row = conn.execute('SELECT response FROM x402_operations WHERE id = ?', (oid,)).fetchone()
        saved = payment_operations.unseal(row['response'])
        saved.pop('mcp_result')
        conn.execute('UPDATE x402_operations SET response = ? WHERE id = ?', (payment_operations.seal(saved), oid))
        conn.commit()
    session = _RecoverableMcpSession(failure=AssertionError('must use cache'))
    cached = await x402_payer.pay_mcp(session, **kwargs)
    assert cached.body == original.body
    assert cached.body[0].text == 'result'
    assert cached.body[1].mimeType == 'image/png'
    assert cached.paid == original.paid and cached.status_code == original.status_code
    assert not session.calls
    assert len(spend_service.list_payments()) == 1
