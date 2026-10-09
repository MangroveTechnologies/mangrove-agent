import threading
from unittest.mock import Mock

import pytest
from mangrove_ai.exceptions import APIError

from src.services import on_chain
from src.shared.errors import UpstreamAccessError, X402PaymentUncertain


@pytest.mark.asyncio
async def test_read_runs_off_event_loop_and_preserves_metadata(monkeypatch):
    event_thread = threading.get_ident()
    def call(**kwargs):
        assert threading.get_ident() != event_thread
        assert kwargs == {'symbol': 'ETH', 'label': 'whale'}
        return {'provider': 'nansen', 'label': 'whale', '_meta': {'operation_id': 'fixture'}}
    client = Mock()
    client.on_chain.get_token_flows = call
    monkeypatch.setattr(on_chain, 'mangrove_ai_client', lambda: client)
    result = await on_chain.read('get_token_flows', symbol='ETH', label='whale')
    assert result['_meta']['operation_id'] == 'fixture'
    assert result['provider'] == 'nansen'


@pytest.mark.asyncio
@pytest.mark.parametrize('status', [401, 403, 429, 501, 503])
async def test_errors_preserve_status_without_retry_or_secret_leak(monkeypatch, status):
    call = Mock(side_effect=APIError(status, 'error', 'secret-provider-key', 'PROVIDER_FAILURE', retry_after=30))
    client = Mock()
    client.on_chain.get_token_flows = call
    monkeypatch.setattr(on_chain, 'mangrove_ai_client', lambda: client)
    expected = UpstreamAccessError if status in (401, 403) else on_chain.OnChainUpstreamError
    with pytest.raises(expected) as caught:
        await on_chain.read('get_token_flows', 'ETH')
    assert caught.value.http_status == status
    assert 'secret-provider-key' not in str(caught.value.to_dict())
    call.assert_called_once()


@pytest.mark.asyncio
async def test_payment_uncertainty_is_preserved(monkeypatch):
    error = X402PaymentUncertain(operation_id='original-operation')
    client = Mock()
    client.on_chain.get_token_flows.side_effect = error
    monkeypatch.setattr(on_chain, 'mangrove_ai_client', lambda: client)
    with pytest.raises(X402PaymentUncertain) as caught:
        await on_chain.read('get_token_flows', 'ETH')
    assert caught.value is error
