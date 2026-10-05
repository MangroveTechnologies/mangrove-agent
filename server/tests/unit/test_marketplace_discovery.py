"""Trusted discovery, optional configuration pins and server-owned networks."""
from copy import deepcopy

import httpx
import pytest

from src.config import app_config
from src.services import marketplace as svc
from src.shared.errors import SigningError
from tests.unit import test_marketplace as fixtures

arguments = fixtures.arguments
env = fixtures.env
discovered_contracts = fixtures.discovered_contracts

DOCUMENT = {'version': 1, 'ownership': {'version': 1, 'audience': 'test-market',
                                      'xrpl_network': 'testnet', 'chain_id': 84532, 'evm_network': 'eip155:84532'}}


@pytest.fixture
def discovery(monkeypatch):
    for key, value in {'MANGROVE_API_KEY': 'private-key',
                       'MANGROVEMARKETS_BASE_URL': 'https://markets.test',
                       'MANGROVEAI_BASE_URL': 'https://identity.test/api/v1',
                       'MARKETPLACE_OWNERSHIP_AUDIENCE': None,
                       'MARKETPLACE_CHAIN_ID': None,
                       'MARKETPLACE_XRPL_NETWORK': None}.items():
        monkeypatch.setattr(app_config, key, value)
    state = {'document': deepcopy(DOCUMENT), 'status': 200, 'requests': [], 'headers': {}}
    original = httpx.Client

    def handle(request):
        state['requests'].append(request)
        assert request.url == 'https://markets.test/.well-known/mangrove-marketplace'
        assert 'authorization' not in request.headers
        return httpx.Response(state['status'], json=state['document'], headers=state['headers'])

    def client(**kwargs):
        assert kwargs['follow_redirects'] is False
        assert kwargs['trust_env'] is False
        return original(transport=httpx.MockTransport(handle), **kwargs)
    monkeypatch.setattr(svc.httpx, 'Client', client)
    return state


def test_discovery_requires_no_manual_audience_or_network(discovery):
    assert svc._settings() == dict(markets='https://markets.test',
                                  authority='https://identity.test/api/v1',
                                  audience='test-market', chain_id=84532, xrpl_network='testnet')
    assert svc.readiness()['ownership_discovery'] == 'ready'


@pytest.mark.parametrize('document', [
    {}, [], {'version': True, 'ownership': DOCUMENT['ownership']},
    {'version': 2, 'ownership': DOCUMENT['ownership']},
    {'version': 1, 'ownership': {'version': True, 'audience': 'test-market'}},
    {'version': 1, 'ownership': {'version': 1, 'audience': ''}},
    {'version': 1, 'ownership': {'version': 1, 'audience': 'a\nb'}},
    {'version': 1, 'ownership': {'version': 1, 'audience': 'a' * 201}},
    {'version': 1, 'ownership': {**DOCUMENT['ownership'], 'xrpl_network': []}},
    {'version': 1, 'ownership': {**DOCUMENT['ownership'], 'xrpl_network': 'wrong'}},
    {'version': 1, 'padding': 'x' * 8192, 'ownership': DOCUMENT['ownership']},
])
def test_invalid_document_fails_closed(discovery, document):
    discovery['document'] = document
    with pytest.raises(svc.MarketplaceError, match='discovery'):
        svc._settings()


@pytest.mark.parametrize('status', [302, 401, 404, 503])
def test_unavailable_or_redirect_never_followed(discovery, status):
    discovery['status'] = status
    discovery['headers'] = {'location': 'https://untrusted.test'}
    with pytest.raises(svc.MarketplaceError):
        svc._settings()
    assert len(discovery['requests']) == 1


@pytest.mark.parametrize('key,value', [('MARKETPLACE_OWNERSHIP_AUDIENCE', 'other'),
                                     ('MARKETPLACE_XRPL_NETWORK', 'mainnet'),
                                     ('MARKETPLACE_CHAIN_ID', 8453)])
def test_explicit_pins_must_match(discovery, monkeypatch, key, value):
    monkeypatch.setattr(app_config, key, value)
    with pytest.raises(svc.MarketplaceError, match='does not match'):
        svc._settings()


def test_wallet_network_is_bound_to_approval_and_signed(env):
    approval = svc.prepare('marketplace_create_listing', arguments(), env.wallet.address)
    assert approval['chain_id'] == 84532
    assert env.decrypted == []
    assert svc.submit(approval['approval_id'], True)['listing_id'] == 'saved-listing'
    assert len(env.decrypted) == 1


def test_changed_wallet_network_invalidates_approval_before_signing(env):
    approval = svc.prepare('marketplace_create_listing', arguments(), env.wallet.address)
    env.row['chain_id'] = 8453
    with pytest.raises(SigningError):
        svc.submit(approval['approval_id'], True)
    assert env.decrypted == []


def test_discovered_identity_change_invalidates_approval_before_signing(env):
    approval = svc.prepare('marketplace_create_listing', arguments(), env.wallet.address)
    env.settings['audience'] = 'different-deployment'
    with pytest.raises(svc.MarketplaceError, match='configuration'):
        svc.submit(approval['approval_id'], True)
    assert env.decrypted == []


@pytest.mark.parametrize('chain_id,backup', [(1, 'yes'), (84532, None), (True, 'yes')])
def test_automatic_selection_never_bypasses_wallet_checks(env, chain_id, backup):
    env.row.update(chain_id=chain_id, backup_confirmed_at=backup)
    with pytest.raises(SigningError):
        svc.prepare('marketplace_create_listing', arguments(), env.wallet.address)
    assert env.decrypted == []


def test_explicit_network_pin_is_preserved(env):
    env.settings['chain_id'] = 8453
    with pytest.raises(SigningError):
        svc.prepare('marketplace_create_listing', arguments(), env.wallet.address)


def test_wallet_mode_uses_public_discovery_without_api_key(discovery, monkeypatch):
    monkeypatch.setattr(app_config, 'MANGROVE_API_KEY', None)
    assert svc._settings()['authority'] is None
    assert len(discovery['requests']) == 1


def test_readiness_endpoint_requires_local_auth_before_discovery(monkeypatch):
    from unittest.mock import Mock

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from src.api.routes.marketplace import router
    from src.shared.errors import AuthMissingApiKey

    monkeypatch.setattr(app_config, 'AUTH_ENABLED', True)
    monkeypatch.setattr(app_config, 'API_KEYS', 'local-readiness-secret')
    ready = Mock(return_value={'ownership_discovery': 'ready', 'wallet_check': 'on_selection'})
    monkeypatch.setattr(svc, 'readiness', ready)
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as client:
        with pytest.raises(AuthMissingApiKey):
            client.get('/marketplace/readiness')
        ready.assert_not_called()
        response = client.get('/marketplace/readiness', headers={'X-API-Key': 'local-readiness-secret'})
        assert response.status_code == 200
        assert response.json()['ownership_discovery'] == 'ready'
        ready.assert_called_once_with()


@pytest.mark.parametrize('fields', [
    {'chain_id': True, 'evm_network': 'eip155:84532'},
    {'chain_id': 1, 'evm_network': 'eip155:1'},
    {'chain_id': 84532, 'evm_network': 'eip155:8453'},
    {'chain_id': None, 'evm_network': None},
])
def test_invalid_server_network_never_reaches_wallet(discovery, fields):
    discovery['document']['ownership'].update(fields)
    with pytest.raises(svc.MarketplaceError, match='discovery'):
        svc._settings()


def test_missing_server_network_cannot_be_inferred_from_wallet(env):
    env.settings['chain_id'] = None
    with pytest.raises(SigningError):
        svc.prepare('marketplace_create_listing', arguments(), env.wallet.address)
    assert not env.decrypted


def test_changed_server_network_invalidates_existing_approval(env):
    approval = svc.prepare('marketplace_create_listing', arguments(), env.wallet.address)
    env.settings['chain_id'] = 8453
    with pytest.raises((svc.MarketplaceError, SigningError)):
        svc.submit(approval['approval_id'], True)
    assert not env.decrypted


@pytest.mark.parametrize('key', [None, '', 'null', 'none'])
def test_explicit_api_mode_never_falls_back_when_key_is_missing(key):
    from types import SimpleNamespace
    from src.shared.clients.mangrove import _api_key
    from src.shared.errors import ValidationError
    with pytest.raises(ValidationError):
        _api_key(SimpleNamespace(MANGROVE_ACCESS_MODE='api-key', MANGROVE_API_KEY=key))
