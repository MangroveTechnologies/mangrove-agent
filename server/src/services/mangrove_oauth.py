"""Local OAuth connection: PKCE, encrypted durable tokens and serialized rotation.

Tokens are valid only for MangroveAI's /mcp/connect resource. No REST, Markets,
API-key or wallet fallback is permitted. The local server runs one worker.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlencode, urlsplit

import httpx

from src.config import app_config
from src.shared.crypto.fernet import decrypt, encrypt
from src.shared.errors import AgentError

DEFAULT_ISSUER = 'https://api.mangrovedeveloper.ai'
SCOPES = 'data:read strategies:read backtests:read signals:read positions:read offline_access'
ALLOWED_SCOPES = frozenset((SCOPES + ' backtests:run signals:evaluate').split())
_lock = threading.RLock()
_pending: dict | None = None


class OAuthError(AgentError):
    code = 'OAUTH_CONNECTION_REQUIRED'
    http_status = 401


def enabled() -> bool:
    return getattr(app_config, 'MANGROVE_ACCESS_MODE', None) == 'oauth'


def issuer() -> str:
    return validate_issuer(getattr(app_config, 'MANGROVE_OAUTH_ISSUER', None) or DEFAULT_ISSUER)


def validate_issuer(value: str) -> str:
    if not isinstance(value, str) or any(ord(c) < 33 or ord(c) == 127 for c in value):
        raise OAuthError('Invalid Mangrove OAuth issuer configuration.')
    u = urlsplit(value)
    if (not u.hostname or u.username or u.password or u.query or u.fragment or u.path not in {'', '/'}
            or not (u.scheme == 'https' or (u.scheme == 'http' and u.hostname in {'127.0.0.1', 'localhost', '::1'}))):
        raise OAuthError('Invalid Mangrove OAuth issuer configuration.')
    return value.rstrip('/')


def endpoint() -> str:
    return issuer() + '/mcp/connect'


def callback_url() -> str:
    u = urlsplit(app_config.LOCAL_AGENT_URL)
    if (u.scheme != 'http' or u.hostname not in {'127.0.0.1', 'localhost', '::1'}
            or u.username or u.password or u.query or u.fragment or u.path not in {'', '/'}):
        raise OAuthError('OAuth requires a loopback local agent URL.')
    return app_config.LOCAL_AGENT_URL.rstrip('/') + '/api/v1/agent/oauth/callback'


@contextmanager
def store():
    """A dedicated database; IMMEDIATE serializes refresh across processes too."""
    path = Path(app_config.DB_PATH).parent / 'oauth.db'
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.is_symlink():
        raise OAuthError('OAuth credential store must not be a symlink.')
    fd = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    os.close(fd)
    os.chmod(path, 0o600)
    conn = sqlite3.connect(path, timeout=20)
    try:
        conn.execute('CREATE TABLE IF NOT EXISTS connection (id INTEGER PRIMARY KEY CHECK(id=1), payload BLOB NOT NULL)')
        conn.commit()
        conn.execute('BEGIN IMMEDIATE')
        yield conn
        conn.commit()
    finally:
        conn.close()


def read(conn):
    row = conn.execute('SELECT payload FROM connection WHERE id=1').fetchone()
    if row is None:
        return None
    try:
        return json.loads(decrypt(row[0]))
    except Exception:
        raise OAuthError('Cannot read the encrypted OAuth connection. Restore its encryption key or reconnect.') from None


def save(conn, data):
    conn.execute('INSERT OR REPLACE INTO connection VALUES (1, ?)', (encrypt(json.dumps(data).encode()),))


def post(path, data=None, *, json_body=None):
    """Pinned same-origin endpoints, bounded replies, no redirects or retries."""
    try:
        with httpx.Client(timeout=15, trust_env=False, follow_redirects=False) as client:
            with client.stream('POST', issuer() + path, data=data, json=json_body) as response:
                body = bytearray()
                for chunk in response.iter_bytes():
                    body.extend(chunk)
                    if len(body) > 65536:
                        raise OAuthError('OAuth server response exceeded the size limit.')
                if response.status_code >= 300:
                    raise OAuthError(f'Mangrove sign-in returned HTTP {response.status_code}. Reconnect; no payment fallback was attempted.')
                result = json.loads(body) if body else {}
                if not isinstance(result, dict):
                    raise ValueError
                return result
    except OAuthError:
        raise
    except Exception:
        raise OAuthError('Mangrove sign-in could not complete. Reconnect if the token exchange outcome is uncertain.') from None


def token_record(reply, *, client_id, scopes):
    access, refresh = reply.get('access_token'), reply.get('refresh_token')
    scope_value = reply.get('scope')
    if not isinstance(scope_value, str) or len(scope_value) > 2048:
        raise OAuthError('Mangrove returned an invalid OAuth scope response. Reconnect.')
    granted = scope_value.split()
    ttl = reply.get('expires_in')
    persistent = 'offline_access' in granted
    valid_refresh = (isinstance(refresh, str) and refresh.startswith('mgo_rt_')
                     and len(refresh) <= 4096 and all(33 <= ord(c) <= 126 for c in refresh))
    if (not isinstance(reply.get('token_type'), str) or reply['token_type'].lower() != 'bearer'
            or not isinstance(access, str) or not access.startswith('mgo_at_') or len(access) > 4096
            or (persistent and not valid_refresh) or (not persistent and refresh is not None)
            or not isinstance(ttl, (int, float)) or isinstance(ttl, bool) or not 0 < ttl <= 86400
            or not set(granted) <= set(scopes.split()) or not (set(granted) - {'offline_access'})
            or any(ord(c) < 33 or ord(c) > 126 for c in access)):
        raise OAuthError('Mangrove returned an invalid OAuth token response. Reconnect.')
    return dict(access_token=access, refresh_token=refresh, expires_at=time.time() + ttl,
                client_id=client_id, scope=' '.join(granted), issuer=issuer(), resource=endpoint(), state='connected')


def cleanup_message(record):
    """Guide cleanup without treating stored issuer metadata as network authority."""
    origins = []
    for grant in (record, record.get('retired') or {}):
        try:
            origin = validate_issuer(grant.get('issuer', ''))
            if len(origin) <= 512 and origin not in origins:
                origins.append(origin)
        except (OAuthError, ValueError, TypeError):
            continue
    destinations = ', '.join(origins) or 'the original configured issuer'
    return (f'OAuth grant revocation is pending for {destinations}. Configure MANGROVE_OAUTH_ISSUER to the '
            'original issuer for each remaining grant, restart the agent, and run '
            './scripts/setup.sh --oauth disconnect before changing issuers or signing in. '
            'Portal revocation alone does not clear the local cleanup receipt.')


def require_replaceable(record):
    if record and (record.get('issuer') != issuer() or record.get('retired')
                   or record.get('state') == 'disconnected'):
        raise OAuthError(cleanup_message(record))


def start(scopes: str = SCOPES):
    global _pending
    if not enabled():
        raise OAuthError('Select OAuth mode in setup before signing in.')
    wanted = set(scopes.split()) | {'offline_access'}
    if not wanted <= ALLOWED_SCOPES:
        raise OAuthError('Unsupported OAuth permission requested.')
    with _lock:
        with store() as conn:
            previous = read(conn)
            require_replaceable(previous)
        if _pending and _pending['expires_at'] > time.time():
            raise OAuthError('A sign-in is already pending. Cancel it before starting another.')
        redirect = callback_url()
        registration = post('/oauth/register', json_body={
            'client_name': 'Mangrove Agent', 'redirect_uris': [redirect],
            'grant_types': ['authorization_code', 'refresh_token'], 'response_types': ['code'],
            'token_endpoint_auth_method': 'none',
        })
        client_id = registration.get('client_id')
        if not isinstance(client_id, str) or not 0 < len(client_id) <= 2048:
            raise OAuthError('Invalid OAuth client registration response.')
        verifier, state = secrets.token_urlsafe(48), secrets.token_urlsafe(32)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
        scope = ' '.join(sorted(wanted))
        _pending = dict(state=state, verifier=verifier, redirect_uri=redirect, client_id=client_id,
                        scope=scope, expires_at=time.time() + 600, issuer=issuer())
        url = issuer() + '/oauth/authorize?' + urlencode(dict(
            client_id=client_id, redirect_uri=redirect, response_type='code', scope=scope,
            state=state, code_challenge=challenge, code_challenge_method='S256', resource=endpoint()))
        return {'authorization_url': url, 'expires_in': 600}


def complete(params):
    global _pending
    with _lock:
        pending = _pending
        state = params.get('state', '')
        if not isinstance(state, str) or not state.isascii() or len(state) > 256:
            raise OAuthError('Invalid sign-in callback state.')
        if (not pending or pending['expires_at'] <= time.time()
                or not secrets.compare_digest(state, pending['state'])
                or pending['issuer'] != issuer() or params.get('iss') != issuer()):
            raise OAuthError('Invalid or expired sign-in callback. Start sign-in again.')
        _pending = None  # single use even when exchange fails or approval is denied
        if params.get('error'):
            raise OAuthError('Sign-in was declined or cancelled. Your existing connection was not changed.')
        code = params.get('code', '')
        if not code or len(code) > 4096:
            raise OAuthError('Sign-in callback has no valid authorization code.')
        # Recheck under the database lock: another process may have changed the
        # connection while consent was open. Do not mint a replacement that would
        # overwrite an outstanding revocation receipt.
        with store() as conn:
            previous = read(conn)
            require_replaceable(previous)
            reply = post('/oauth/token', dict(grant_type='authorization_code', code=code,
                         code_verifier=pending['verifier'], redirect_uri=pending['redirect_uri'],
                         client_id=pending['client_id'], resource=endpoint()))
            record = token_record(reply, client_id=pending['client_id'], scopes=pending['scope'])
            if previous:
                record['retired'] = {k: previous[k] for k in ('issuer', 'client_id', 'refresh_token', 'access_token')
                                     if k in previous}
            save(conn, record)
        # New connection is durable before revoking the old one. Keep an encrypted
        # revocation receipt if offline rather than losing the previous grant.
        if previous and revoke_record(previous):
            with store() as conn:
                if read(conn) == record:
                    record.pop('retired', None)
                    save(conn, record)


def access_token() -> str:
    if not enabled():
        raise OAuthError('OAuth access is not selected.')
    with _lock, store() as conn:
        record = read(conn)
        if (not record or record.get('state') != 'connected' or record.get('issuer') != issuer()
                or record.get('resource') != endpoint()):
            raise OAuthError('Sign in to Mangrove again. No API-key or wallet fallback was attempted.')
        # Temporary consent lasts until the actual access-token expiry. It must
        # never enter refresh rotation or silently request longer-lived access.
        if not record.get('refresh_token'):
            if record['expires_at'] > time.time():
                return record['access_token']
            raise OAuthError('Your temporary Mangrove connection expired. Sign in again. No API-key or wallet fallback was attempted.')
        if record['expires_at'] > time.time() + 60:
            return record['access_token']
        # Persist before sending: a crash/timeout must never replay a rotated refresh token.
        record['state'] = 'refresh_pending'
        save(conn, record)
        conn.commit()
        conn.execute('BEGIN IMMEDIATE')
        # Process lock plus DB state prevent a second process from rotating the same token.
        current = read(conn)
        if current != record:
            raise OAuthError('OAuth connection changed during refresh. Reconnect.')
        reply = post('/oauth/token', dict(grant_type='refresh_token', refresh_token=record['refresh_token'],
                                         client_id=record['client_id']))
        updated = token_record(reply, client_id=record['client_id'], scopes=record['scope'])
        if record.get('retired'):
            updated['retired'] = record['retired']
        save(conn, updated)
        return updated['access_token']


def status():
    with _lock, store() as conn:
        record = read(conn)
        connected = bool(enabled() and record and record.get('state') == 'connected'
                         and record.get('issuer') == issuer() and record.get('resource') == endpoint()
                         and (record.get('refresh_token') or record['expires_at'] > time.time()))
        return {'connected': connected, 'revocation_pending': bool(record and (record.get('retired') or record.get('state') == 'disconnected')), 'pending': bool(_pending and _pending['expires_at'] > time.time()),
                'scopes': record['scope'].split() if connected else [],
                'expires_at': record['expires_at'] if connected else None}


def cancel():
    global _pending
    with _lock:
        _pending = None
    return {'cancelled': True}


def revoke_record(record):
    if record.get('issuer') != issuer():
        return False
    token_type = 'refresh_token' if record.get('refresh_token') else 'access_token'
    token = record.get(token_type)
    if not token:
        return False
    try:
        post('/oauth/revoke', dict(token=token, client_id=record['client_id'],
                                   token_type_hint=token_type))
        return True
    except OAuthError:
        return False


def disconnect():
    global _pending
    with _lock, store() as conn:
        _pending = None
        record = read(conn)
        # Stop local use first, retaining encrypted revocation evidence if offline.
        if not record:
            return {'disconnected': True, 'revoked': True}
        record['state'] = 'disconnected'
        save(conn, record)
        conn.commit()
        conn.execute('BEGIN IMMEDIATE')
        # Keep cleanup serialized with sign-in and refresh across processes.
        record = read(conn)
        if not record:
            return {'disconnected': True, 'revoked': True}
        revoked = revoke_record(record)
        retired = record.get('retired')
        retired_revoked = not retired or revoke_record(retired)
        if revoked and retired_revoked:
            conn.execute('DELETE FROM connection WHERE id=1')
        elif revoked:
            # The current grant is confirmed revoked; retain only the old grant.
            # It can be revoked after the operator explicitly selects its issuer.
            save(conn, dict(retired, state='disconnected'))
        elif retired_revoked and retired:
            record.pop('retired', None)
            save(conn, record)
        result = {'disconnected': True, 'revoked': bool(revoked and retired_revoked)}
        if not result['revoked']:
            result['message'] = cleanup_message(read(conn))
        return result
