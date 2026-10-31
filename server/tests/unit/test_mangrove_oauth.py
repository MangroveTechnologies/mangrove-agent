"""Offline OAuth contract tests; no real credentials, browser or payments."""
from __future__ import annotations

import json
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.testclient import TestClient
from src.api.routes import oauth as routes
from src.services import mangrove_oauth as oauth


@pytest.fixture
def env(tmp_path, monkeypatch):
    for key, value in dict(DB_PATH=str(tmp_path / 'agent.db'), MANGROVE_ACCESS_MODE='oauth',
                           MANGROVE_OAUTH_ISSUER='https://issuer.test',
                           LOCAL_AGENT_URL='http://127.0.0.1:9080').items():
        monkeypatch.setattr(oauth.app_config, key, value, raising=False)
    cipher = Fernet(Fernet.generate_key())
    monkeypatch.setattr(oauth, 'encrypt', cipher.encrypt)
    monkeypatch.setattr(oauth, 'decrypt', cipher.decrypt)
    monkeypatch.setattr(oauth, '_pending', None)
    return tmp_path


def reply(**over):
    return dict(access_token='mgo_at_synthetic', refresh_token='mgo_rt_synthetic',
                token_type='Bearer', expires_in=3600, scope=oauth.SCOPES, **over)


def connect(monkeypatch, token_reply=None):
    calls = []
    def post(path, data=None, **kwargs):
        calls.append((path, data, kwargs))
        return {'client_id': 'client'} if path == '/oauth/register' else (token_reply if token_reply is not None else reply())
    monkeypatch.setattr(oauth, 'post', post)
    started = oauth.start()
    query = parse_qs(urlsplit(started['authorization_url']).query)
    oauth.complete({'state': query['state'][0], 'iss': oauth.issuer(), 'code': 'synthetic-code'})
    return query, calls


def test_pkce_callback_storage_and_status(env, monkeypatch):
    query, calls = connect(monkeypatch)
    assert query['code_challenge_method'] == ['S256']
    import base64
    import hashlib
    verifier = calls[1][1]['code_verifier']
    assert query['code_challenge'] == [base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()]
    assert oauth.access_token() == 'mgo_at_synthetic'
    assert oauth.status()['connected']
    assert 'synthetic' not in json.dumps(oauth.status())
    raw = (env / 'oauth.db').read_bytes()
    assert b'mgo_at_' not in raw and b'mgo_rt_' not in raw
    assert (env / 'oauth.db').stat().st_mode & 0o777 == 0o600
    with pytest.raises(oauth.OAuthError):
        oauth.complete({'state': query['state'][0], 'iss': oauth.issuer(), 'code': 'replay'})


@pytest.mark.parametrize('change', [{'state': 'wrong'}, {'iss': 'https://evil.test'}])
def test_callback_binding(env, monkeypatch, change):
    monkeypatch.setattr(oauth, 'post', lambda *a, **k: {'client_id': 'client'})
    state = parse_qs(urlsplit(oauth.start()['authorization_url']).query)['state'][0]
    with pytest.raises(oauth.OAuthError):
        oauth.complete(dict(state=state, iss=oauth.issuer(), code='code') | change)
    assert not oauth.status()['connected']


def test_cancel_preserves_existing_connection(env, monkeypatch):
    connect(monkeypatch)
    oauth.start()
    oauth.cancel()
    assert oauth.access_token() == 'mgo_at_synthetic'
    assert not oauth.status()['pending']


def expired():
    with oauth.store() as conn:
        record = oauth.read(conn)
        record['expires_at'] = time.time() - 1
        oauth.save(conn, record)


def test_refresh_is_serialized(env, monkeypatch):
    connect(monkeypatch)
    expired()
    calls = []
    def rotate(path, data):
        calls.append(data)
        return dict(reply(), access_token='mgo_at_rotated', refresh_token='mgo_rt_rotated')
    monkeypatch.setattr(oauth, 'post', rotate)
    with ThreadPoolExecutor(max_workers=8) as pool:
        values = list(pool.map(lambda _: oauth.access_token(), range(8)))
    assert values == ['mgo_at_rotated'] * 8
    assert len(calls) == 1
    with oauth.store() as conn:
        assert oauth.read(conn)['refresh_token'] == 'mgo_rt_rotated'


def test_uncertain_refresh_never_replays(env, monkeypatch):
    connect(monkeypatch)
    expired()
    calls = []
    def lost(*args):
        calls.append(True)
        raise oauth.OAuthError('timeout')
    monkeypatch.setattr(oauth, 'post', lost)
    for _ in range(2):
        with pytest.raises(oauth.OAuthError):
            oauth.access_token()
    assert calls == [True]
    assert not oauth.status()['connected']


def test_disconnect_offline_disables_and_retains_revocation(env, monkeypatch):
    connect(monkeypatch)
    def offline(*args):
        raise oauth.OAuthError('offline')
    monkeypatch.setattr(oauth, 'post', offline)
    result = oauth.disconnect()
    assert result['disconnected'] and not result['revoked']
    assert './scripts/setup.sh --oauth disconnect' in result['message']
    with pytest.raises(oauth.OAuthError):
        oauth.access_token()
    monkeypatch.setattr(oauth, 'post', lambda *args: {})
    assert oauth.disconnect()['revoked']
    with oauth.store() as conn:
        assert oauth.read(conn) is None


def test_issuer_change_never_sends_tokens(env, monkeypatch):
    connect(monkeypatch)
    monkeypatch.setattr(oauth.app_config, 'MANGROVE_OAUTH_ISSUER', 'https://other.test')
    with pytest.raises(oauth.OAuthError):
        oauth.access_token()
    assert not oauth.disconnect()['revoked']


@pytest.mark.parametrize('url', ['http://remote.test', 'https://user:password@issuer.test',
                                  'https://issuer.test/path', 'https://issuer.test?x=y'])
def test_invalid_issuer(env, monkeypatch, url):
    monkeypatch.setattr(oauth.app_config, 'MANGROVE_OAUTH_ISSUER', url)
    with pytest.raises(oauth.OAuthError):
        oauth.start()


def test_local_routes_auth_and_redacted_callback(env, monkeypatch):
    app = FastAPI()
    app.include_router(routes.router, prefix='/api/v1/agent')
    from src.shared.errors import AgentError, agent_error_handler
    app.add_exception_handler(AgentError, agent_error_handler)
    client = TestClient(app)
    assert client.post('/api/v1/agent/oauth/start', json={}).status_code == 401
    assert client.post('/api/v1/agent/oauth/disconnect').status_code == 401
    assert client.get('/api/v1/agent/oauth/status').status_code == 401
    response = client.get('/api/v1/agent/oauth/callback?state=secret&code=private')
    assert response.status_code == 400
    assert 'secret' not in response.text and 'private' not in response.text
    assert response.headers['cache-control'] == 'no-store'
    assert 'window.close' not in response.text
    assert 'script-src' not in response.headers['content-security-policy']

    monkeypatch.setattr(
        oauth, 'post',
        lambda path, data=None, **kwargs: {'client_id': 'client'} if path == '/oauth/register' else reply(),
    )
    started = oauth.start()
    state = parse_qs(urlsplit(started['authorization_url']).query)['state'][0]
    response = client.get('/api/v1/agent/oauth/callback', params={
        'state': state, 'iss': oauth.issuer(), 'code': 'synthetic-code',
    })
    assert response.status_code == 200
    assert '<h1>Mangrove is connected</h1>' in response.text
    assert 'Return to setup to finish' in response.text
    assert 'If it stays open, close it and return to setup.' in response.text
    script_nonce = re.search(r'<script nonce="([^"]+)">', response.text)
    assert script_nonce and re.fullmatch(r'[A-Za-z0-9_-]+', script_nonce.group(1))
    csp = response.headers['content-security-policy']
    assert f"script-src 'nonce-{script_nonce.group(1)}'" in csp
    assert "default-src 'none'" in csp and "frame-ancestors 'none'" in csp
    assert 'setTimeout(function () { window.close(); }, 1500)' in response.text
    assert 'synthetic-code' not in response.text and state not in response.text
    assert 'mgo_at_' not in response.text and 'mgo_rt_' not in response.text
    record = logging.LogRecord('uvicorn.access', 20, '', 0, '%s %s %s %s %s',
                               ('ip', 'GET', '/api/v1/agent/oauth/callback?code=secret', '1.1', 200), None)
    routes._CallbackLogFilter().filter(record)
    assert 'secret' not in record.getMessage()


@pytest.mark.asyncio
async def test_proxy_uses_bearer_only_and_fresh_token(env, monkeypatch):
    from src.mcp import mangrove_proxy as proxy
    monkeypatch.setattr(oauth, 'access_token', lambda: 'mgo_at_current')
    client = proxy._http_client(oauth.endpoint(), None)
    req = httpx.Request('POST', oauth.endpoint())
    await client.event_hooks['request'][0](req)
    assert req.headers['Authorization'] == 'Bearer mgo_at_current'
    assert 'X-API-Key' not in req.headers
    assert not any('payment' in key.lower() for key in req.headers)
    with pytest.raises(oauth.OAuthError):
        await client.event_hooks['request'][0](httpx.Request('POST', 'https://other.test'))
    await client.aclose()


def test_sdk_cannot_fall_back_to_wallet_or_saved_key(env):
    from src.shared.clients.mangrove import _api_key
    from src.shared.errors import ValidationError
    with pytest.raises(ValidationError, match='No wallet fallback'):
        _api_key(oauth.app_config)


def test_http_token_exchange_never_follows_redirect_or_leaks_body(env, monkeypatch):
    real = httpx.Client
    requests = []
    def handle(request):
        requests.append(request)
        return httpx.Response(302, headers={'location': 'https://evil.test'}, text='mgo_rt_private')
    monkeypatch.setattr(oauth.httpx, 'Client', lambda **kw: real(transport=httpx.MockTransport(handle), **kw))
    with pytest.raises(oauth.OAuthError) as error:
        oauth.post('/oauth/token', {'refresh_token': 'mgo_rt_private'})
    assert 'private' not in str(error.value)
    assert len(requests) == 1
    assert requests[0].url.host == 'issuer.test'


@pytest.mark.parametrize('field,value', [('scope', None), ('token_type', None), ('access_token', 'mgo_at_x\r\n'),
                                         ('expires_in', float('nan')), ('refresh_token', None),
                                         ('scope', oauth.SCOPES + ' admin:manage')])
def test_invalid_tokens_rejected(env, field, value):
    with pytest.raises(oauth.OAuthError):
        oauth.token_record(dict(reply(), **{field: value}), client_id='c', scopes=oauth.SCOPES)


def test_oauth_management_requires_auth_even_when_local_auth_disabled(env, monkeypatch):
    monkeypatch.setattr(oauth.app_config, 'AUTH_ENABLED', False)
    app = FastAPI()
    app.include_router(routes.router, prefix='/api/v1/agent')
    from src.shared.errors import AgentError, agent_error_handler
    app.add_exception_handler(AgentError, agent_error_handler)
    client = TestClient(app)
    assert client.post('/api/v1/agent/oauth/start', json={}).status_code == 401
    assert client.get('/api/v1/agent/oauth/status').status_code == 401


def test_scope_error_gives_explicit_reconnect_without_retry(env):
    from src.mcp import mangrove_proxy
    request = httpx.Request('POST', oauth.endpoint())
    response = httpx.Response(403, request=request, headers={
        'www-authenticate': 'Bearer error="insufficient_scope", scope="signals:read signals:evaluate"'})
    error = mangrove_proxy.transport_error(httpx.HTTPStatusError('denied', request=request, response=response))
    assert './scripts/setup.sh --auth oauth --oauth login --oauth-scope signals:evaluate' in error.suggestion
    assert 'setup_support.py' not in error.suggestion
    assert 'No tool was retried' in error.message


def test_callback_denial_and_expiry_keep_prior_grant(env, monkeypatch):
    connect(monkeypatch)
    state = parse_qs(urlsplit(oauth.start()['authorization_url']).query)['state'][0]
    with pytest.raises(oauth.OAuthError, match='declined'):
        oauth.complete({'state': state, 'iss': oauth.issuer(), 'error': 'access_denied'})
    assert oauth.access_token() == 'mgo_at_synthetic'
    oauth.start()
    oauth._pending['expires_at'] = 0
    with pytest.raises(oauth.OAuthError):
        oauth.complete({'state': oauth._pending['state'], 'iss': oauth.issuer(), 'code': 'expired'})
    assert oauth.access_token() == 'mgo_at_synthetic'


def test_reconnect_revokes_previous_grant_after_saving_new(env, monkeypatch):
    connect(monkeypatch)
    _, calls = connect(monkeypatch)
    assert calls[-1][0] == '/oauth/revoke'
    assert calls[-1][1]['token'] == 'mgo_rt_synthetic'
    assert not oauth.status()['revocation_pending']


def test_failed_old_grant_revocation_is_retained_and_retried(env, monkeypatch):
    connect(monkeypatch)
    def post(path, data=None, **kwargs):
        if path == '/oauth/register':
            return {'client_id': 'new-client'}
        if path == '/oauth/revoke':
            raise oauth.OAuthError('offline')
        return dict(reply(), refresh_token='mgo_rt_new')
    monkeypatch.setattr(oauth, 'post', post)
    query = parse_qs(urlsplit(oauth.start()['authorization_url']).query)
    oauth.complete({'state': query['state'][0], 'iss': oauth.issuer(), 'code': 'new-code'})
    assert oauth.status()['connected'] and oauth.status()['revocation_pending']
    with pytest.raises(oauth.OAuthError, match='revocation'):
        oauth.start()
    revoked = []
    monkeypatch.setattr(oauth, 'post', lambda path, data: revoked.append(data['token']) or {})
    assert oauth.disconnect()['revoked']
    assert set(revoked) == {'mgo_rt_new', 'mgo_rt_synthetic'}


def temporary_reply():
    result = reply()
    result.pop('refresh_token')
    result['scope'] = 'data:read'
    return result


def test_temporary_callback_uses_full_lifetime_and_expires_without_refresh(env, monkeypatch):
    _, calls = connect(monkeypatch, temporary_reply())
    assert oauth.status()['connected']
    assert oauth.status()['scopes'] == ['data:read']
    with oauth.store() as conn:
        record = oauth.read(conn)
        assert record['refresh_token'] is None
        record['expires_at'] = 1000
        oauth.save(conn, record)
    # The final minute is usable: the refresh safety window applies only to
    # persistent connections. Neither status nor access may attempt a refresh.
    calls.clear()
    monkeypatch.setattr(oauth.time, 'time', lambda: 999)
    assert oauth.access_token() == 'mgo_at_synthetic'
    assert oauth.status()['connected']
    monkeypatch.setattr(oauth.time, 'time', lambda: 1000)
    assert not oauth.status()['connected']
    assert oauth.status()['expires_at'] is None
    for _ in range(2):
        with pytest.raises(oauth.OAuthError, match='temporary.*expired'):
            oauth.access_token()
    assert calls == []
    assert b'mgo_at_' not in (env / 'oauth.db').read_bytes()


def test_temporary_disconnect_retains_token_until_revocation_succeeds(env, monkeypatch):
    connect(monkeypatch, temporary_reply())
    expired()
    def offline(*args):
        raise oauth.OAuthError('offline')
    monkeypatch.setattr(oauth, 'post', offline)
    result = oauth.disconnect()
    assert result['disconnected'] and not result['revoked']
    assert './scripts/setup.sh --oauth disconnect' in result['message']
    with oauth.store() as conn:
        assert oauth.read(conn)['access_token'] == 'mgo_at_synthetic'
    revoked = []
    monkeypatch.setattr(oauth, 'post', lambda path, data: revoked.append((path, data)) or {})
    assert oauth.disconnect()['revoked']
    assert revoked == [('/oauth/revoke', {'token': 'mgo_at_synthetic', 'client_id': 'client',
                                         'token_type_hint': 'access_token'})]
    with oauth.store() as conn:
        assert oauth.read(conn) is None


@pytest.mark.parametrize('old_temporary', [True, False])
def test_replacement_between_temporary_and_persistent_keeps_cleanup_receipt(env, monkeypatch, old_temporary):
    connect(monkeypatch, temporary_reply() if old_temporary else reply())
    new_reply = dict(reply() if old_temporary else temporary_reply(), access_token='mgo_at_new')
    if old_temporary:
        new_reply['refresh_token'] = 'mgo_rt_new'
    def post(path, data=None, **kwargs):
        if path == '/oauth/register':
            return {'client_id': 'new-client'}
        if path == '/oauth/revoke':
            # The replacement must be saved before contacting the revocation
            # endpoint so an offline server cannot lose either grant.
            with oauth.store() as conn:
                assert oauth.read(conn)['access_token'] == 'mgo_at_new'
            raise oauth.OAuthError('offline')
        return new_reply
    monkeypatch.setattr(oauth, 'post', post)
    query = parse_qs(urlsplit(oauth.start()['authorization_url']).query)
    oauth.complete({'state': query['state'][0], 'iss': oauth.issuer(), 'code': 'new-code'})
    assert oauth.status()['connected'] and oauth.status()['revocation_pending']
    assert oauth.access_token() == 'mgo_at_new'
    revoked = []
    monkeypatch.setattr(oauth, 'post', lambda path, data: revoked.append(data) or {})
    assert oauth.disconnect()['revoked']
    assert [(r['token'], r['token_type_hint']) for r in revoked] == (
        [('mgo_rt_new', 'refresh_token'), ('mgo_at_synthetic', 'access_token')] if old_temporary else
        [('mgo_at_new', 'access_token'), ('mgo_rt_synthetic', 'refresh_token')])
    with oauth.store() as conn:
        assert oauth.read(conn) is None


def test_refresh_token_without_persistent_consent_is_rejected(env):
    with pytest.raises(oauth.OAuthError, match='invalid OAuth token'):
        oauth.token_record(dict(reply(), scope='data:read'), client_id='c', scopes=oauth.SCOPES)


def test_cross_issuer_replacement_is_blocked_before_registration(env, monkeypatch):
    connect(monkeypatch)
    monkeypatch.setattr(oauth.app_config, 'MANGROVE_OAUTH_ISSUER', 'https://other.test')
    calls = []
    monkeypatch.setattr(oauth, 'post', lambda *a, **k: calls.append(a))
    with pytest.raises(oauth.OAuthError, match='issuer.test'):
        oauth.start()
    assert calls == []
    assert not oauth.status()['pending']
    assert not oauth.disconnect()['revoked']
    assert calls == []
    assert oauth.status()['revocation_pending']
    with oauth.store() as conn:
        assert oauth.read(conn)['refresh_token'] == 'mgo_rt_synthetic'
    # Explicitly restore the original trusted configuration to finish cleanup.
    monkeypatch.setattr(oauth.app_config, 'MANGROVE_OAUTH_ISSUER', 'https://issuer.test')
    monkeypatch.setattr(oauth, 'post', lambda *a, **k: {})
    assert oauth.disconnect()['revoked']
    monkeypatch.setattr(oauth.app_config, 'MANGROVE_OAUTH_ISSUER', 'https://other.test')
    connect(monkeypatch)
    assert oauth.status()['connected']


@pytest.mark.parametrize('first', ['https://issuer.test', 'https://other.test'])
def test_existing_mixed_issuer_grants_cleanup_in_either_order(env, monkeypatch, first):
    connect(monkeypatch)
    with oauth.store() as conn:
        old = oauth.read(conn)
        current = dict(old, issuer='https://other.test', resource='https://other.test/mcp/connect',
                       access_token='mgo_at_other', refresh_token='mgo_rt_other', retired=old)
        oauth.save(conn, current)
    monkeypatch.setattr(oauth.app_config, 'MANGROVE_OAUTH_ISSUER', first)
    calls = []
    def revoke(path, data):
        calls.append((oauth.issuer(), path, data['token']))
        return {}
    monkeypatch.setattr(oauth, 'post', revoke)
    result = oauth.disconnect()
    assert result['disconnected'] and not result['revoked']
    second = 'https://other.test' if first == 'https://issuer.test' else 'https://issuer.test'
    assert second in result['message'] and 'mgo_' not in result['message']
    assert not oauth.status()['connected'] and oauth.status()['revocation_pending']
    with pytest.raises(oauth.OAuthError, match='revocation'):
        oauth.start()
    # Repeated cleanup at the first issuer sends no further tokens.
    assert not oauth.disconnect()['revoked']
    assert len(calls) == 1
    monkeypatch.setattr(oauth.app_config, 'MANGROVE_OAUTH_ISSUER', second)
    assert oauth.disconnect() == {'disconnected': True, 'revoked': True}
    assert set(calls) == {('https://issuer.test', '/oauth/revoke', 'mgo_rt_synthetic'),
                          ('https://other.test', '/oauth/revoke', 'mgo_rt_other')}
    with oauth.store() as conn:
        assert oauth.read(conn) is None
    connect(monkeypatch)
    assert oauth.status()['connected']


@pytest.mark.parametrize('mutation', ['foreign', 'retired', 'disconnected'])
def test_callback_rechecks_cleanup_before_token_exchange(env, monkeypatch, mutation):
    connect(monkeypatch)
    state = parse_qs(urlsplit(oauth.start()['authorization_url']).query)['state'][0]
    with oauth.store() as conn:
        record = oauth.read(conn)
        if mutation == 'foreign':
            record['issuer'] = 'https://other.test'
        elif mutation == 'retired':
            record['retired'] = dict(record)
        else:
            record['state'] = 'disconnected'
        oauth.save(conn, record)
    calls = []
    monkeypatch.setattr(oauth, 'post', lambda *a, **k: calls.append(a))
    with pytest.raises(oauth.OAuthError, match='revocation'):
        oauth.complete({'state': state, 'iss': oauth.issuer(), 'code': 'synthetic'})
    assert calls == []
    with oauth.store() as conn:
        assert oauth.read(conn) == record


def test_disconnected_offline_receipt_blocks_replacement(env, monkeypatch):
    connect(monkeypatch)
    def offline(*args, **kwargs):
        raise oauth.OAuthError('offline')
    monkeypatch.setattr(oauth, 'post', offline)
    assert not oauth.disconnect()['revoked']
    calls = []
    monkeypatch.setattr(oauth, 'post', lambda *a, **k: calls.append(a))
    with pytest.raises(oauth.OAuthError, match='revocation'):
        oauth.start()
    assert calls == []


def test_cleanup_guidance_never_echoes_token_or_unsafe_origin():
    message = oauth.cleanup_message({'issuer': 'https://user:mgo_rt_private@issuer.test',
                                    'retired': {'issuer': 'https://issuer.test\nsecret'}})
    assert 'mgo_rt_private' not in message and 'secret' not in message
    assert 'original configured issuer' in message


def test_reconnect_cleanup_does_not_restore_concurrently_disconnected_grant(env, monkeypatch):
    connect(monkeypatch)
    def post(path, data=None, **kwargs):
        if path == '/oauth/register':
            return {'client_id': 'new-client'}
        if path == '/oauth/token':
            return dict(reply(), access_token='mgo_at_new', refresh_token='mgo_rt_new')
        # Simulate another worker disconnecting after replacement is durable.
        with oauth.store() as conn:
            conn.execute('DELETE FROM connection WHERE id=1')
        return {}
    monkeypatch.setattr(oauth, 'post', post)
    state = parse_qs(urlsplit(oauth.start()['authorization_url']).query)['state'][0]
    oauth.complete({'state': state, 'iss': oauth.issuer(), 'code': 'synthetic'})
    assert not oauth.status()['connected']
    with oauth.store() as conn:
        assert oauth.read(conn) is None


def test_reconnect_cleanup_preserves_concurrently_rotated_tokens(env, monkeypatch):
    connect(monkeypatch)
    def post(path, data=None, **kwargs):
        if path == '/oauth/register':
            return {'client_id': 'new-client'}
        if path == '/oauth/token':
            return dict(reply(), access_token='mgo_at_new', refresh_token='mgo_rt_new')
        # A second worker may refresh the durable replacement while this worker
        # waits for the old grant's revocation response. Never restore old tokens.
        with oauth.store() as conn:
            record = oauth.read(conn)
            record.update(access_token='mgo_at_rotated', refresh_token='mgo_rt_rotated')
            oauth.save(conn, record)
        return {}
    monkeypatch.setattr(oauth, 'post', post)
    state = parse_qs(urlsplit(oauth.start()['authorization_url']).query)['state'][0]
    oauth.complete({'state': state, 'iss': oauth.issuer(), 'code': 'synthetic'})
    assert oauth.access_token() == 'mgo_at_rotated'
    with oauth.store() as conn:
        record = oauth.read(conn)
        assert record['refresh_token'] == 'mgo_rt_rotated'
        assert record['retired']['refresh_token'] == 'mgo_rt_synthetic'
